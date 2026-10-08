import argparse
import logging
import os
import re
import time
from notion_client import Client
import requests
from datetime import datetime, timedelta, timezone
import hashlib
from dotenv import load_dotenv
from notion_client.errors import APIResponseError
from retrying import retry
from .blocks import (
    get_callout,
    get_date,
    get_heading,
    get_icon,
    get_multi_select,
    get_number,
    get_quote,
    get_rich_text,
    get_select,
    get_status,
    get_title,
    get_url,
)

client = None
data_source_id = None
data_source_property_types = {}
title_property_name = None
skipped_property_names = set()
weread = None

load_dotenv()
WEREAD_URL = "https://weread.qq.com/"
WEREAD_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.4"
# Notion 官方限流约 3 次/秒，所有写操作统一节流，避免 429。
# 网络较差时可调大：export NOTION_WRITE_INTERVAL=0.5
def _parse_write_interval():
    try:
        return float(os.getenv("NOTION_WRITE_INTERVAL") or "0.3")
    except (TypeError, ValueError):
        return 0.3


NOTION_WRITE_INTERVAL = _parse_write_interval()
NOTION_VERSION = os.getenv("NOTION_API_VERSION") or "2026-03-11"
BOOKMARK_CALLOUT_ICON = "〰️"
NOTE_CALLOUT_ICON = "✍️"
# 刷新划线时只覆盖「本程序生成的块」，避免删掉用户在页面里手写的内容：
# 章节标题 + 带 〰️/✍️ 图标的 callout。其余块类型（段落/图片/分栏/表格/待办…）全部保留。
SYNC_GENERATED_HEADING_TYPES = {"heading_1", "heading_2", "heading_3"}
SYNC_CALLOUT_ICONS = {BOOKMARK_CALLOUT_ICON, NOTE_CALLOUT_ICON}
# 微信读书时间戳按北京时间展示，避免写入的时间整体偏移 8 小时
CN_TZ = timezone(timedelta(hours=8))
# 微信读书同步过来的记录统一标记为「书籍」
WEREAD_ITEM_TYPE = "书籍"
NOTION_TOKEN_PATTERN = re.compile(r"^(secret|ntn)_[A-Za-z0-9_-]{20,}$")
WEREAD_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{10,}$")
NOTION_ID_PATTERN = re.compile(
    r"^[a-f0-9]{32}$|^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
NOTION_ID_IN_TEXT_PATTERN = re.compile(
    r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.IGNORECASE,
)

# 接口字段名在不同版本间有差异，按顺序兜底取值。
# 首选项已于 2026-09-02 实测确认（/book/getprogress 真实返回）：
#   ['appId', 'bookVersion', 'chapterIdx', 'chapterOffset', 'chapterUid',
#    'installId', 'isStartReading', 'progress', 'readingTime',
#    'recordReadingTime', 'repairOffsetTime', 'reviewId', 'startReadingTime',
#    'summary', 'synckey', 'ttsTime', 'updateTime']
# 注：finishTime 只在书籍读完后才出现，未读完时该字段不存在（值为 0）。
START_TIME_KEYS = (
    "startReadingTime",   # 实测命中
    "beginReadingTime",
    "beginReadTime",
    "firstReadTime",
    "readUpdateTime",
    "createTime",
)
READING_TIME_KEYS = (
    "recordReadingTime",  # 实测命中
    "readingTime",
    "totalReadingTime",
    "readTime",
    "duration",
)


class ConfigError(Exception):
    pass


def emit_error(message):
    if os.getenv("GITHUB_ACTIONS") == "true":
        safe = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{safe}")
    else:
        print(f"配置错误: {message}")


def fail_config(message):
    emit_error(message)
    raise ConfigError(message)


def clean_secret_value(name, required=False):
    raw = os.getenv(name)
    if raw is None:
        if required:
            fail_config(f"缺少 {name}，请在 GitHub Actions Secrets 中配置")
        return None
    value = re.sub(r"\s+", "", raw)
    if value:
        os.environ[name] = value
        return value
    if required:
        fail_config(f"{name} 为空，请检查 GitHub Actions Secrets")
    os.environ.pop(name, None)
    return None


def validate_regex(name, value, pattern, hint):
    if value and not pattern.search(value):
        fail_config(f"{name} 格式不正确：{hint}")
    return value


def validate_secret_inputs():
    weread_api_key = clean_secret_value("WEREAD_API_KEY", required=True)
    notion_token = clean_secret_value("NOTION_TOKEN", required=True)
    notion_page = clean_secret_value("NOTION_PAGE")
    notion_database_id = clean_secret_value("NOTION_DATABASE_ID")
    notion_data_source_id = clean_secret_value("NOTION_DATA_SOURCE_ID")

    validate_regex(
        "WEREAD_API_KEY",
        weread_api_key,
        WEREAD_API_KEY_PATTERN,
        "应为微信读书 Gateway API Key，不能包含空格或换行",
    )
    validate_regex(
        "NOTION_TOKEN",
        notion_token,
        NOTION_TOKEN_PATTERN,
        "应以 secret_ 或 ntn_ 开头，不能包含空格或换行",
    )
    for name, value in (
        ("NOTION_DATA_SOURCE_ID", notion_data_source_id),
        ("NOTION_DATABASE_ID", notion_database_id),
    ):
        validate_regex(
            name,
            value,
            NOTION_ID_PATTERN,
            "应为 32 位 Notion ID 或带连字符的 UUID",
        )
    if notion_page and not NOTION_ID_IN_TEXT_PATTERN.search(notion_page):
        fail_config("NOTION_PAGE 格式不正确：请填写 Notion 页面链接、数据库链接或 ID")
    if not (notion_data_source_id or notion_page or notion_database_id):
        fail_config(
            "缺少 NOTION_PAGE / NOTION_DATA_SOURCE_ID / NOTION_DATABASE_ID，"
            "请至少配置其中一个"
        )
    return {
        "weread_api_key": weread_api_key,
        "notion_token": notion_token,
    }


class WeReadGatewayClient:
    def __init__(self, api_key):
        if not api_key:
            fail_config("没有找到 WEREAD_API_KEY，请在 GitHub Actions Secrets 中配置")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def request(self, api_name, **kwargs):
        payload = {
            "api_name": api_name,
            "skill_version": WEREAD_SKILL_VERSION,
            **kwargs,
        }
        response = self.session.post(WEREAD_GATEWAY_URL, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("upgrade_info"):
            raise Exception(f"微信读书 skill 需要升级: {data.get('upgrade_info')}")
        if data.get("errcode", 0) != 0:
            raise Exception(f"微信读书 Gateway 请求失败: {api_name}, errcode={data.get('errcode')}, response={data}")
        return data


def get_range_start(item):
    note_range = item.get("range") or ""
    try:
        return int(note_range.split("-")[0] or 0)
    except (ValueError, TypeError):
        return 0


def get_note_sort_key(item, chapter=None):
    chapter_uid = item.get("chapterUid", 1)
    chapter_info = None
    if chapter:
        chapter_info = chapter.get(chapter_uid) or chapter.get(str(chapter_uid))
    chapter_idx = (
        chapter_info.get("chapterIdx", 1000000)
        if chapter_info
        else chapter_uid
    )
    return (chapter_idx, get_range_start(item))


def pick_first_number(source, keys):
    """按候选字段名依次取值，取到第一个有效数字为止。"""
    for key in keys:
        value = to_number(source.get(key))
        if value:
            return value
    return 0


def timestamp_to_local(value):
    """微信读书时间戳 → 北京时间字符串；无值返回 None（不写入）。"""
    number = to_number(value)
    if not number:
        return None
    return datetime.fromtimestamp(number, tz=CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookmark_list(bookId):
    """获取我的划线"""
    data = weread.request("/book/bookmarklist", bookId=bookId)
    updated = data.get("updated") or []
    return sorted(updated, key=get_note_sort_key)


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_read_info(bookId):
    data = weread.request("/book/getprogress", bookId=bookId)
    book = data.get("book") or {}
    progress = to_number(book.get("progress")) or 0
    reading_progress = normalize_reading_progress(progress)
    finish_time = book.get("finishTime") or 0
    update_time = book.get("updateTime") or 0
    # 开始时间与阅读时长在不同接口版本里字段名不一致，逐个兜底
    start_time = pick_first_number(book, START_TIME_KEYS)
    reading_time = pick_first_number(book, READING_TIME_KEYS)
    if not start_time:
        print(
            f"  [提示] 未取到开始时间，/book/getprogress 可用字段: {sorted(book.keys())}"
        )
    if not reading_time:
        print(
            f"  [提示] 未取到阅读时长，/book/getprogress 可用字段: {sorted(book.keys())}"
        )
    if finish_time or progress >= 100:
        marked_status = 4
    elif update_time or book.get("isStartReading") or progress > 0:
        marked_status = 2
    else:
        marked_status = 1
    return {
        "markedStatus": marked_status,
        "readingTime": reading_time,
        "readingProgress": reading_progress,
        "finishedDate": finish_time,
        "startedDate": start_time,
    }


def normalize_reading_progress(value):
    value = to_number(value) or 0
    if value > 1:
        value = value / 100
    return round(min(max(value, 0), 1), 4)


def normalize_rating(value):
    value = value or 0
    if value > 100:
        return value / 1000
    if value > 10:
        return value / 10
    return value


def normalize_sort(value):
    """把「最后阅读时间 / 笔记排序」统一成和模板里一致的秒级时间戳。

    - 兼容毫秒级时间戳（>10^12 时自动除以 1000）；
    - 无有效值（如未开始阅读的书 readUpdateTime=0）时返回 None，避免写入无意义的 0。
    """
    number = to_number(value) or 0
    if number and number > 10**12:
        number = int(number // 1000)
    return number or None


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookinfo(bookId):
    """获取书的详情"""
    data = weread.request("/book/info", bookId=bookId)
    isbn = data.get("isbn", "")
    newRating = normalize_rating(data.get("newRating"))
    return (isbn, newRating)


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_review_list(bookId, max_pages=50):
    """获取笔记（分页带游标推进校验，避免接口返回脏数据时死循环）"""
    reviews_data = []
    synckey = 0
    for _ in range(max_pages):
        data = weread.request("/review/list/mine", bookid=bookId, synckey=synckey, count=100)
        batch = data.get("reviews") or []
        reviews_data.extend(batch)
        if not batch or not data.get("hasMore", 0):
            break
        new_synckey = data.get("synckey", 0)
        if not new_synckey or new_synckey == synckey:
            print(f"  [警告] {bookId} 笔记分页游标未推进，提前结束分页")
            break
        synckey = new_synckey
    else:
        print(f"  [警告] {bookId} 的笔记超过 {max_pages} 页，提前结束分页")
    summary = list(filter(lambda x: (x.get("review") or {}).get("type") == 4, reviews_data))
    reviews = list(filter(lambda x: (x.get("review") or {}).get("type") == 1, reviews_data))
    reviews = list(map(lambda x: x.get("review") or {}, reviews))
    reviews = list(
        map(
            lambda x: {
                **x,
                "markText": x.pop("content", ""),
                "_callout_icon": NOTE_CALLOUT_ICON,
            },
            reviews,
        )
    )
    return summary, reviews


def build_book_link(bookId):
    return f"https://weread.qq.com/web/reader/{calculate_book_str_id(bookId)}"


def find_existing(link):
    """用「链接」定位已同步的记录。链接由 bookId 生成，天然唯一，
    因此在模板删除 BookId 后仍可准确去重。"""
    if "链接" not in data_source_property_types:
        return []
    response = query_data_source(filter=build_equals_filter("链接", link))
    return response.get("results", [])


def delete_existing(link):
    """删除已存在的记录后重建（与原有行为一致，保证划线笔记是最新）。"""
    removed = 0
    for result in find_existing(link):
        try:
            client.blocks.delete(block_id=result["id"])
            removed += 1
        except Exception as e:
            print(f"删除块时出错: {e}")
    return removed


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_chapter_info(bookId):
    """获取章节信息"""
    data = weread.request("/book/chapterinfo", bookId=bookId)
    chapters = data.get("chapters") or []
    return {item["chapterUid"]: item for item in chapters if "chapterUid" in item}


def format_reading_time(seconds):
    """秒 → 'X时Y分' 文本（仅在「阅读时长」是文本列时使用）。"""
    seconds = to_number(seconds) or 0
    text = ""
    hour = seconds // 3600
    if hour > 0:
        text += f"{hour}时"
    minutes = seconds % 3600 // 60
    if minutes > 0:
        text += f"{minutes}分"
    return text


def reading_time_value(seconds):
    """按「阅读时长」列的实际类型返回写入值：数字列写分钟，文本列写 'X时Y分'。"""
    seconds = to_number(seconds) or 0
    if get_property_type("阅读时长") == "number":
        return max(0, round(seconds / 60))
    return format_reading_time(seconds)


def insert_to_notion(bookName, bookId, cover, sort, author, isbn, rating, categories):
    """插入到notion"""
    if not cover or not cover.startswith("http"):
        cover = "https://www.notion.so/icons/book_gray.svg"
    parent = {"type": "data_source_id", "data_source_id": data_source_id}
    raw_properties = {
        title_property_name: bookName,
        "链接": build_book_link(bookId),
        "作者": author,
        "评分": rating,
        "类型": WEREAD_ITEM_TYPE,
    }
    # BookId / Sort / ISBN 仅在模板仍保留这些列时才写入，模板删列后自动跳过
    if "BookId" in data_source_property_types:
        raw_properties["BookId"] = bookId
    if "Sort" in data_source_property_types:
        raw_properties["Sort"] = sort
    if "ISBN" in data_source_property_types:
        raw_properties["ISBN"] = isbn
    if categories != None:
        raw_properties["分类"] = categories
    read_info = (
        get_read_info(bookId=bookId)
        if has_any_property(
            ("状态", "阅读时长", "阅读进度", "开始时间", "结束时间", "时间")
        )
        else None
    )
    if read_info != None:
        markedStatus = read_info.get("markedStatus", 0)
        readingTime = read_info.get("readingTime", 0)
        readingProgress = read_info.get("readingProgress", 0)
        # 三态阅读状态：未开始 / 在读 / 读完（模板无「状态」列时自动跳过）
        status_map = {1: "未开始", 2: "在读", 4: "读完"}
        raw_properties["状态"] = status_map.get(markedStatus, "在读")
        raw_properties["阅读时长"] = reading_time_value(readingTime)
        raw_properties["阅读进度"] = readingProgress
        finished_at = timestamp_to_local(read_info.get("finishedDate"))
        if finished_at:
            # 兼容仍在使用「时间」这一旧列名的模板
            if "结束时间" in data_source_property_types:
                raw_properties["结束时间"] = finished_at
            elif "时间" in data_source_property_types:
                raw_properties["时间"] = finished_at
        started_at = timestamp_to_local(read_info.get("startedDate"))
        if started_at and "开始时间" in data_source_property_types:
            raw_properties["开始时间"] = started_at

    properties = build_notion_properties(raw_properties)
    icon = get_icon(cover)
    response = client.pages.create(parent=parent, icon=icon, cover=icon, properties=properties)
    return response["id"]


def add_children(page_id, children):
    """分批追加子块（Notion 单次上限 100）。返回写入成功的块列表，未全部成功返回 None。

    注意分批方式：只对非空分片发请求，避免 children 长度为 100 整数倍时
    多发一次 children=[] 的请求（Notion 会返回 400 并中断整轮同步）。"""
    chunks = [children[i : i + 100] for i in range(0, len(children), 100)]
    results = []
    for chunk in chunks:
        if not chunk:
            continue
        time.sleep(NOTION_WRITE_INTERVAL)
        response = client.blocks.children.append(block_id=page_id, children=chunk)
        results.extend(response.get("results") or [])
    return results if len(results) == len(children) else None


def add_grandchild(grandchild, results):
    for key, value in grandchild.items():
        time.sleep(NOTION_WRITE_INTERVAL)
        page_id = results[key].get("id")
        client.blocks.children.append(block_id=page_id, children=[value])


def get_sort():
    """获取 data source 中已同步的最新 Sort，作为增量游标。
    模板没有 Sort 列（或列为空）时返回 0，退化为全量处理。"""
    if "Sort" not in data_source_property_types:
        print("模板未保留 Sort 列，本次按全量处理")
        return 0
    filter = build_is_not_empty_filter("Sort")
    sorts = [
        {
            "property": "Sort",
            "direction": "descending",
        }
    ]
    response = query_data_source(filter=filter, sorts=sorts, page_size=1)
    if len(response.get("results")) == 1:
        return get_number_property_value(
            response.get("results")[0].get("properties").get("Sort")
        )
    return 0


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookshelf():
    """获取全量书架，覆盖「未开始阅读」的书。

    走官方 skill 网关 /shelf/sync（请求参数通常为空）。
    返回扁平化书籍列表，字段与 notebooks 模式尽量对齐，便于复用 insert_to_notion。

    注意：/shelf/sync 返回 books[] 为扁平结构（bookId/title/author/cover/category/
    finishReading/readUpdateTime 等均在项顶层），与 notebooks 包在 book 对象里不同。
    不同 skill 版本可能把 books 包一层 data，这里做了兼容兜底。
    """
    data = weread.request("/shelf/sync")
    payload = data.get("books") or (data.get("data") or {}).get("books") or []
    books = []
    for b in payload:
        books.append({
            "bookId": b.get("bookId"),
            "title": b.get("title") or "",
            "author": b.get("author") or "",
            # 缩略图转高清，与 notebooks 模式保持一致
            "cover": (b.get("cover") or "").replace("/s_", "/t7_"),
            # 系统分类，单字符串（与 notebooks 的 categories 列表语义不同）
            "category": b.get("category") or "",
            "finishReading": to_number(b.get("finishReading")) or 0,
            "readUpdateTime": to_number(b.get("readUpdateTime")) or 0,
            "isTop": to_number(b.get("isTop")) or 0,
            "secret": to_number(b.get("secret")) or 0,
        })
    # 有声书 / 专辑在独立数组 albums[]，合并进列表，分类标记为「有声书」
    for a in (data.get("albums") or []):
        info = a.get("albumInfo") or {}
        extra = a.get("albumInfoExtra") or {}
        if not info.get("albumId"):
            continue
        books.append({
            "bookId": info.get("albumId"),
            "title": info.get("name") or "",
            "author": info.get("authorName") or "",
            "cover": (info.get("cover") or "").replace("/s_", "/t7_"),
            "category": "有声书",
            "finishReading": to_number(info.get("finish")) or 0,
            "readUpdateTime": to_number(extra.get("lectureReadUpdateTime")) or 0,
            "isTop": to_number(extra.get("isTop")) or 0,
            "secret": 0,
        })
    return books


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def update_read_properties(page_id, bookId, sort=None):
    """已存在的书：只更新阅读状态相关字段（状态/阅读时长/阅读进度/开始/结束时间）
    以及 Sort，不删除重建、不拉取笔记正文。用于「全量枚举 + 增量更新」模式，
    保证页面 ID 稳定、省 API。

    Sort 修复：/shelf/sync 不返回 Sort，若这里不写，新书 / 已存在的书 Sort 会一直为空。
    这里用「微信读书笔记排序（有笔记时）」或「最后阅读时间」补齐，读完的书因此也有 Sort。
    """
    if not has_any_property(
        ("状态", "阅读时长", "阅读进度", "开始时间", "结束时间", "时间", "Sort")
    ):
        return
    raw = {}
    if sort and "Sort" in data_source_property_types:
        raw["Sort"] = normalize_sort(sort)
    if has_any_property(
        ("状态", "阅读时长", "阅读进度", "开始时间", "结束时间", "时间")
    ):
        read_info = get_read_info(bookId=bookId)
        if read_info is not None:
            markedStatus = read_info.get("markedStatus", 0)
            readingTime = read_info.get("readingTime", 0)
            readingProgress = read_info.get("readingProgress", 0)
            status_map = {1: "未开始", 2: "在读", 4: "读完"}
            raw["状态"] = status_map.get(markedStatus, "在读")
            raw["阅读时长"] = reading_time_value(readingTime)
            raw["阅读进度"] = readingProgress
            finished_at = timestamp_to_local(read_info.get("finishedDate"))
            if finished_at:
                # 兼容仍在使用「时间」这一旧列名的模板
                if "结束时间" in data_source_property_types:
                    raw["结束时间"] = finished_at
                elif "时间" in data_source_property_types:
                    raw["时间"] = finished_at
            started_at = timestamp_to_local(read_info.get("startedDate"))
            if started_at and "开始时间" in data_source_property_types:
                raw["开始时间"] = started_at
    raw = {name: value for name, value in raw.items() if value is not None}
    payload = build_notion_properties(raw)
    if payload:
        client.pages.update(page_id=page_id, properties=payload)


def is_sync_generated_block(block):
    """判断一个顶层块是否由本同步程序生成（可安全覆盖）。

    本程序只生成两种顶层块：
      - 章节标题：heading_1 / heading_2 / heading_3
      - 划线 / 笔记：icon 为 〰️ 或 ✍️ 的 callout
    其余类型（paragraph 段落、image 图片、column_list 分栏、table 表格、
    to_do 待办、divider 分割线、bookmark、toggle…）一律视为用户手写，必须保留。
    """
    block_type = block.get("type")
    if block_type in SYNC_GENERATED_HEADING_TYPES:
        return True
    if block_type == "callout":
        icon = (block.get("callout") or {}).get("icon") or {}
        emoji = icon.get("emoji")
        if not emoji:
            return False
        # 兼容带 / 不带变体选择符（U+FE0F）的 emoji
        stripped = {e.rstrip("\ufe0f") for e in SYNC_CALLOUT_ICONS}
        return emoji in SYNC_CALLOUT_ICONS or emoji.rstrip("\ufe0f") in stripped
    return False


def list_removable_block_ids(page_id):
    """列出本页「同步生成、刷新时可覆盖」的顶层块 id；其余手写块一律保留。

    返回 (待删除 id 列表, 页面顶层块总数, 手写块数)。
    """
    removable = []
    total = 0
    manual = 0
    cursor = None
    while True:
        kwargs = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        response = client.blocks.children.list(**kwargs)
        for block in response.get("results") or []:
            total += 1
            if is_sync_generated_block(block):
                removable.append(block["id"])
            else:
                manual += 1
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return removable, total, manual


def delete_blocks(block_ids, retries=3):
    """逐个删除块：带节流与重试，返回最终没删掉的块 id 列表。

    必须节流：重写一次划线要删几十上百个块，Notion 限流约 3 次/秒；
    如果删除失败被静默忽略，就会出现「旧块残留 + 新块已写入」→ 笔记重复两份。
    """
    failed = []
    for block_id in block_ids:
        for attempt in range(retries):
            try:
                client.blocks.delete(block_id=block_id)
                break
            except Exception as e:
                if attempt == retries - 1:
                    failed.append(block_id)
                    print(f"  [警告] 删除块失败 {block_id}: {e}")
                else:
                    time.sleep(NOTION_WRITE_INTERVAL * (attempt + 2))
        time.sleep(NOTION_WRITE_INTERVAL)
    return failed


def refresh_content(page_id, bookId):
    """重新抓取并写入某本书的划线 / 笔记。

    安全设计一：只覆盖「本程序生成的块」
        页面里的章节标题（heading_*）和带 〰️/✍️ 图标的 callout 会被替换；
        你手写的段落、图片、分栏、表格、待办、分割线等一律保留，不会被删。

    安全设计二：先写后删
        1. 先算出「旧同步块」的 id 列表；
        2. 把最新内容追加到页面末尾；
        3. 全部写入成功后，才按 id 删除旧同步块。
        这样即使中途限流 / 网络失败，旧内容仍完好；
        最坏情况只是暂时重复，并会在下一次同步时被自动清理。

    设环境变量 WEREAD_REFRESH_MODE=skip 可完全跳过正文刷新（只更新阅读状态）。
    """
    if (os.getenv("WEREAD_REFRESH_MODE") or "full").lower() == "skip":
        print("  [跳过正文刷新] WEREAD_REFRESH_MODE=skip")
        return
    chapter = get_chapter_info(bookId)
    bookmark_list = get_bookmark_list(bookId)
    summary, reviews = get_review_list(bookId)
    bookmark_list.extend(reviews)
    bookmark_list = sorted(
        bookmark_list,
        key=lambda x: get_note_sort_key(x, chapter),
    )
    children, grandchild = get_children(chapter, summary, bookmark_list)
    if not children:
        # 该书当前没有可写入的划线 / 笔记，保留原内容，避免误清空
        return
    old_block_ids, total_blocks, manual_blocks = list_removable_block_ids(page_id)
    if manual_blocks:
        print(f"  [保护] 页面有 {manual_blocks} 个手写块将被保留")
    skip_delete = False
    if total_blocks and not old_block_ids:
        # 页面上有内容却一个同步块都没识别出来：只追加新内容，不删除任何旧块
        #（例如这本书的页面完全是你手写的，还没有同步生成过标题/划线）
        print("  [保护] 未识别到任何同步生成的块，本次只追加新内容、不删除旧内容")
        skip_delete = True
    results = add_children(page_id, children)
    if results is None:
        # 新内容没全部写进去，此时绝不能删旧块
        raise RuntimeError("写入新内容未全部成功，已保留原有正文不动")
    if len(grandchild) > 0:
        add_grandchild(grandchild, results)
    if skip_delete:
        return
    failed = delete_blocks(old_block_ids)
    if failed:
        print(
            f"  [警告] {len(failed)} 个旧块未删除，页面可能暂时重复，下次同步会自动重试清理"
        )


def get_notebook_sort_map():
    """列出「有笔记 / 划线」的书 → Sort 值（来自 /user/notebooks）。

    同时承担两个职责：
    - 正文刷新白名单：只有真正有笔记的书才需要抓取笔记，省 API；
    - Sort 字段的权威来源：与历史数据里已有的 Sort 口径一致。
    """
    mapping = {}
    try:
        for item in get_notebooklist():
            book = item.get("book") or item
            bookId = book.get("bookId")
            if not bookId:
                continue
            mapping[bookId] = (
                to_number(item.get("sort")) or to_number(book.get("sort")) or 0
            )
    except Exception as e:
        print(f"[警告] 获取笔记列表失败，本次跳过正文刷新: {e}")
    return mapping


def _sync_shelf():
    """全量枚举 + 增量更新：每日用 /shelf/sync 扫全量书架（含未开始阅读的书）。

    - 全量枚举：每天仍扫描整个书架，因此新加入书架、以及从未打开的书都会被纳入。
    - 已存在的书：刷新阅读状态字段 + Sort；若该书「有笔记 / 划线」再刷新正文。
    - 新书 / 未开始阅读的书：新建页面；有笔记的书顺带抓取正文。

    关键修复（相对上一版）：
    1. 已存在的书也会刷新划线 / 笔记正文（之前只更新状态，导致新写的笔记永远不同步）；
    2. 补齐 Sort 字段（之前插入时传 None，导致 Sort 空白，尤其是读完后仍为空）；
    3. 每本书独立 try/except：单本失败不再中断整轮，失败清单在结尾汇总。
    """
    print("同步模式: 全量书架 (shelf) —— 全量枚举 + 增量更新")
    books = get_bookshelf()
    print(f"书架共 {len(books)} 本（含电子书与有声书）")
    # 有笔记 / 划线的书：正文只对这些书刷新，同时作为 Sort 的权威来源
    note_sort = get_notebook_sort_map()
    print(f"其中有笔记 / 划线的书 {len(note_sort)} 本，将刷新正文")
    failures = []
    for index, b in enumerate(books):
        bookId = b.get("bookId")
        title = b.get("title") or ""
        if not bookId:
            continue
        print(f"正在同步 {title} ,当前是第{index+1}本，共{len(books)}本。")
        # 单本隔离：一本书失败（限流 / 下架 / 接口偶发异常）不影响后面的书
        try:
            _sync_one_book(b, title, bookId, note_sort)
        except Exception as e:
            failures.append((title, e))
            print(f"  [失败] {title}: {e}")
    report_failures(failures, len(books))


def _sync_one_book(b, title, bookId, note_sort):
    """同步单本书：已存在则更新状态 + Sort（+ 刷新正文），否则新建。"""
    # Sort：优先微信读书笔记排序，其次最后阅读时间（保证读过的书都有 Sort）
    sort = normalize_sort(note_sort.get(bookId) or b.get("readUpdateTime"))
    book_link = build_book_link(bookId)

    # 全量枚举：先按「链接」查这本书是否已在 Notion
    existing = find_existing(book_link)
    if existing:
        if len(existing) > 1:
            print(f"  [提示] Notion 中存在 {len(existing)} 条同名链接记录，仅更新第一条")
        # 已存在 → 更新阅读状态与 Sort（不删除重建，页面 ID 稳定）
        page_id = existing[0]["id"]
        update_read_properties(page_id, bookId, sort)
        # 有笔记的书 → 刷新划线与笔记正文（页面 ID 不变）
        if bookId in note_sort:
            refresh_content(page_id, bookId)
        return

    # 新书 / 未开始阅读的书 → 完整新建
    isbn, rating = ("", None)
    if has_any_property(("ISBN", "评分")):
        isbn, rating = get_bookinfo(bookId)
    category = b.get("category")
    # 系统分类转成列表，喂给 insert_to_notion 的 categories 参数
    categories = [category] if category else None
    page_id = insert_to_notion(
        title, bookId, b.get("cover") or "", sort or None, b.get("author") or "",
        isbn, rating, categories,
    )
    # 有笔记的书 → 抓取正文；未开始阅读的书没有内容，自动跳过，节省 API
    if bookId in note_sort:
        refresh_content(page_id, bookId)


def report_failures(failures, total):
    """结尾汇总：部分失败只警告（不把 Action 标红），全失败或严格模式则退出码 1。"""
    if not failures:
        print(f"\n✅ 同步完成：{total} 本全部成功")
        return
    print(f"\n⚠️ 有 {len(failures)}/{total} 本同步失败：")
    for title, e in failures[:20]:
        print(f"   - {title}: {e}")
    if len(failures) > 20:
        print(f"   ...（其余 {len(failures) - 20} 条省略）")
    if len(failures) == total or (os.getenv("WEREAD_STRICT") or "").lower() in {
        "1",
        "true",
        "yes",
    }:
        raise SystemExit(1)


def get_notebooklist(max_pages=200):
    """获取笔记本列表（分页带游标推进校验，避免某条记录缺 sort 时死循环）"""
    books = []
    lastSort = None
    for _ in range(max_pages):
        params = {"count": 100}
        if lastSort is not None:
            params["lastSort"] = lastSort
        data = weread.request("/user/notebooks", **params)
        batch = data.get("books") or []
        books.extend(batch)
        if not batch or not data.get("hasMore", 0):
            break
        new_sort = batch[-1].get("sort")
        if new_sort is None or new_sort == lastSort:
            print("[警告] 笔记列表分页游标未推进，提前结束分页")
            break
        lastSort = new_sort
    else:
        print(f"[警告] 笔记列表超过 {max_pages} 页，提前结束分页")
    books.sort(key=lambda x: x.get("sort") or 0)
    return books


def get_children(chapter, summary, bookmark_list):
    children = []
    grandchild = {}
    all_chapters = []
    if chapter:
        for uid, info in chapter.items():
            item = dict(info)
            item["chapterUid"] = item.get("chapterUid", uid)
            all_chapters.append(item)
        all_chapters.sort(key=lambda x: x.get("chapterIdx", 0))
    # 按 chapterUid 建立位置索引：避免 all_chapters.index() 按值比对，
    # 在出现内容完全相同的同名章节时定位到错误的位置
    position_by_uid = {}
    for index, node in enumerate(all_chapters):
        position_by_uid.setdefault(node.get("chapterUid"), index)
        position_by_uid.setdefault(str(node.get("chapterUid")), index)

    def get_ancestor_chain(chapter_uid):
        if chapter_uid is None:
            return []
        current_pos = position_by_uid.get(chapter_uid)
        if current_pos is None:
            current_pos = position_by_uid.get(str(chapter_uid))
        if current_pos is None:
            return []
        current_chapter_info = all_chapters[current_pos]

        chain = []
        target_level = current_chapter_info.get("level", 1)
        for index in range(current_pos - 1, -1, -1):
            candidate = all_chapters[index]
            if candidate.get("level", 1) < target_level:
                chain.insert(0, candidate)
                target_level = candidate.get("level", 1)
                if target_level <= 1:
                    break
        chain.append(current_chapter_info)
        return chain

    if chapter:
        grouped_bookmarks = []
        last_uid = None
        current_group = None

        for data in bookmark_list:
            uid = data.get("chapterUid", 1)
            if uid != last_uid:
                if current_group:
                    grouped_bookmarks.append(current_group)
                info = chapter.get(uid) or chapter.get(str(uid))
                current_group = {
                    "chapterUid": uid,
                    "bookmarks": [],
                    "chapterInfo": info,
                }
                last_uid = uid
            current_group["bookmarks"].append(data)
        if current_group:
            grouped_bookmarks.append(current_group)

        previous_path_uids = []
        for group in grouped_bookmarks:
            info = group["chapterInfo"]
            if info:
                path = get_ancestor_chain(group["chapterUid"])

                divergence_index = 0
                min_len = min(len(path), len(previous_path_uids))
                while divergence_index < min_len:
                    path_uid = path[divergence_index].get("chapterUid")
                    if path_uid != previous_path_uids[divergence_index]:
                        break
                    divergence_index += 1

                for chapter_node in path[divergence_index:]:
                    children.append(
                        get_heading(
                            chapter_node.get("level"), chapter_node.get("title")
                        )
                    )
                previous_path_uids = [node.get("chapterUid") for node in path]
            else:
                previous_path_uids = []

            for i in group["bookmarks"]:
                markText = i.get("markText") or ""
                if not markText:
                    continue
                callout_icon = i.get("_callout_icon") or BOOKMARK_CALLOUT_ICON
                for j in range(0, len(markText) // 2000 + 1):
                    children.append(
                        get_callout(
                            markText[j * 2000 : (j + 1) * 2000],
                            icon=callout_icon,
                        )
                    )
                if i.get("abstract") != None and i.get("abstract") != "":
                    quote = get_quote(i.get("abstract"))
                    grandchild[len(children) - 1] = quote

    else:
        # 如果没有章节信息
        for data in bookmark_list:
            markText = data.get("markText") or ""
            if not markText:
                continue
            for i in range(0, len(markText) // 2000 + 1):
                children.append(
                    get_callout(
                        markText[i * 2000 : (i + 1) * 2000],
                        icon=BOOKMARK_CALLOUT_ICON,
                    )
                )
    if summary != None and len(summary) > 0:
        children.append(get_heading(1, "点评"))
        for i in summary:
            content = (i.get("review") or {}).get("content") or ""
            if not content:
                continue
            for j in range(0, len(content) // 2000 + 1):
                children.append(
                    get_callout(
                        content[j * 2000 : (j + 1) * 2000],
                        icon=NOTE_CALLOUT_ICON,
                    )
                )
    return children, grandchild


def transform_id(book_id):
    id_length = len(book_id)

    if re.match(r"^\d*$", book_id):
        ary = []
        for i in range(0, id_length, 9):
            ary.append(format(int(book_id[i : min(i + 9, id_length)]), "x"))
        return "3", ary

    result = ""
    for i in range(id_length):
        result += format(ord(book_id[i]), "x")
    return "4", [result]


def calculate_book_str_id(book_id):
    md5 = hashlib.md5()
    md5.update(book_id.encode("utf-8"))
    digest = md5.hexdigest()
    result = digest[0:3]
    code, transformed_ids = transform_id(book_id)
    result += code + "2" + digest[-2:]

    for i in range(len(transformed_ids)):
        hex_length_str = format(len(transformed_ids[i]), "x")
        if len(hex_length_str) == 1:
            hex_length_str = "0" + hex_length_str

        result += hex_length_str + transformed_ids[i]

        if i < len(transformed_ids) - 1:
            result += "g"

    if len(result) < 20:
        result += digest[0 : 20 - len(result)]

    md5 = hashlib.md5()
    md5.update(result.encode("utf-8"))
    result += md5.hexdigest()[0:3]
    return result


def extract_notion_id():
    url_or_id = (
        os.getenv("NOTION_DATA_SOURCE_ID")
        or os.getenv("NOTION_PAGE")
        or os.getenv("NOTION_DATABASE_ID")
    )
    if not url_or_id:
        fail_config("没有找到 NOTION_PAGE / NOTION_DATA_SOURCE_ID，请按照文档填写")
    match = NOTION_ID_IN_TEXT_PATTERN.search(url_or_id)
    if match:
        return match.group(0)

    fail_config("获取 Notion ID 失败，请检查 NOTION_PAGE / NOTION_DATA_SOURCE_ID")


def query_data_source(**body):
    return client.request(
        path=f"data_sources/{data_source_id}/query",
        method="POST",
        body=body,
    )


def load_data_source_schema():
    """读取当前 data source 的真实属性。
    BookId / Sort 不再强制要求，改用「链接」做去重，因此「链接」是必需的。"""
    global data_source_property_types, title_property_name, skipped_property_names
    response = client.request(path=f"data_sources/{data_source_id}", method="GET")
    properties = response.get("properties") or {}
    data_source_property_types = {
        name: (config or {}).get("type") for name, config in properties.items()
    }
    title_property_name = next(
        (
            name
            for name, prop_type in data_source_property_types.items()
            if prop_type == "title"
        ),
        None,
    )
    skipped_property_names = set()
    if not title_property_name:
        raise Exception("Notion data source 缺少标题属性，请保留一个 Title 类型属性")

    if "链接" not in data_source_property_types:
        raise Exception(
            "Notion data source 缺少「链接」属性，无法判断书籍是否已同步，请在模板中补充后重试"
        )

    print(
        f"已读取 Notion 属性 {len(data_source_property_types)} 个，"
        f"标题属性: {title_property_name}"
    )


def get_property_type(name):
    return data_source_property_types.get(name)


def has_any_property(names):
    return any(name in data_source_property_types for name in names)


def build_equals_filter(name, value):
    prop_type = get_property_type(name)
    if prop_type in {"title", "rich_text", "url", "email", "phone_number"}:
        return {"property": name, prop_type: {"equals": str(value)}}
    if prop_type == "number":
        return {"property": name, "number": {"equals": to_number(value)}}
    if prop_type == "select":
        return {"property": name, "select": {"equals": str(value)}}
    if prop_type == "status":
        return {"property": name, "status": {"equals": str(value)}}
    raise Exception(f"Notion 属性 {name} 的类型 {prop_type} 暂不支持用于查询")


def build_is_not_empty_filter(name):
    prop_type = get_property_type(name)
    if prop_type in {
        "title",
        "rich_text",
        "url",
        "email",
        "phone_number",
        "number",
        "select",
        "status",
        "date",
    }:
        return {"property": name, prop_type: {"is_not_empty": True}}
    raise Exception(f"Notion 属性 {name} 的类型 {prop_type} 暂不支持用于查询")


def to_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(to_text(item) for item in value if item is not None)
    return str(value)


def to_name_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [to_text(item) for item in value if to_text(item)]
    text = to_text(value)
    return [text] if text else []


def to_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def normalize_date_value(value):
    """时间戳按北京时间写入，避免整体偏移 8 小时。"""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return value


def build_option_property(prop_type, value):
    names = to_name_list(value)
    if not names:
        return None
    if prop_type == "status":
        return get_status(names[0])
    if prop_type == "select":
        return get_select(names[0])
    return get_multi_select(names)


def build_notion_property(name, value):
    prop_type = get_property_type(name)
    if not prop_type:
        if name not in skipped_property_names:
            print(f"属性 {name} 在 Notion 模板中不存在，自动跳过")
            skipped_property_names.add(name)
        return None
    if value is None:
        return None

    if prop_type == "title":
        return get_title(to_text(value))
    if prop_type == "rich_text":
        return get_rich_text(to_text(value))
    if prop_type == "number":
        number = to_number(value)
        return get_number(number) if number is not None else None
    if prop_type == "url":
        return get_url(to_text(value))
    if prop_type in {"multi_select", "status", "select"}:
        return build_option_property(prop_type, value)
    if prop_type == "date":
        return get_date(normalize_date_value(value))
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}

    if name not in skipped_property_names:
        print(f"属性 {name} 的类型 {prop_type} 暂不支持写入，自动跳过")
        skipped_property_names.add(name)
    return None


def build_notion_properties(raw_properties):
    return {
        name: prop
        for name, value in raw_properties.items()
        if (prop := build_notion_property(name, value)) is not None
    }


def get_number_property_value(property_value):
    if not property_value:
        return 0
    prop_type = property_value.get("type")
    value = property_value.get(prop_type)
    if prop_type == "number":
        return value or 0
    if prop_type in {"title", "rich_text"} and value:
        return to_number(value[0].get("plain_text")) or 0
    if prop_type in {"select", "status"} and value:
        return to_number(value.get("name")) or 0
    return 0


def is_property_empty(property_value):
    """判断某个属性当前是否有值（用于 backfill 只补空缺）。"""
    if not property_value:
        return True
    prop_type = property_value.get("type")
    value = property_value.get(prop_type)
    if value in (None, "", []):
        return True
    if prop_type == "number":
        return value is None
    if prop_type == "date":
        return not (value or {}).get("start")
    if prop_type in {"select", "status"}:
        return not (value or {}).get("name")
    if prop_type in {"title", "rich_text"}:
        return not "".join(x.get("plain_text", "") for x in (value or [])).strip()
    return False


def resolve_data_source_id(notion_id):
    if os.getenv("NOTION_DATA_SOURCE_ID"):
        return notion_id

    try:
        client.request(path=f"data_sources/{notion_id}", method="GET")
        return notion_id
    except APIResponseError as error:
        code = getattr(error.code, "value", error.code)
        if code not in {"object_not_found", "validation_error"}:
            raise

    database = client.request(path=f"databases/{notion_id}", method="GET")
    sources = database.get("data_sources") or []
    if not sources:
        raise Exception(f"数据库 {notion_id} 下没有可用的 data source")
    if len(sources) > 1:
        print(
            f"数据库 {notion_id} 包含 {len(sources)} 个 data sources，默认使用第一个: {sources[0].get('id')}"
        )
    return sources[0]["id"]


def setup_clients():
    """初始化微信读书与 Notion 客户端并读取模板 schema。"""
    global client, data_source_id, weread
    secrets = validate_secret_inputs()
    notion_id = extract_notion_id()
    notion_token = secrets["notion_token"]
    weread = WeReadGatewayClient(secrets["weread_api_key"])
    client = Client(
        auth=notion_token,
        log_level=logging.ERROR,
        notion_version=NOTION_VERSION,
    )
    data_source_id = resolve_data_source_id(notion_id)
    print(f"Notion API Version: {NOTION_VERSION}")
    print(f"Notion Data Source ID: {data_source_id}")
    load_data_source_schema()


def sync():
    """同步微信读书到 Notion。

    默认 shelf 模式：全量枚举 + 增量更新书架（含未开始阅读的书）——每天扫描全部书架，
    新书/未读书新建页面，已存在的书只刷新阅读状态字段，页面 ID 稳定、省 API。
    设置环境变量 WEREAD_SYNC_MODE=notebooks：切回仅同步有笔记/划线的书（原行为）。
    """
    setup_clients()
    sync_mode = (os.getenv("WEREAD_SYNC_MODE") or "shelf").lower()
    if sync_mode == "shelf":
        _sync_shelf()
        return
    latest_sort = get_sort()
    print(f"增量游标 latest_sort = {latest_sort}")
    books = get_notebooklist()
    if books == None:
        return
    failures = []
    for index, book in enumerate(books):
        sort = book.get("sort") or 0
        if latest_sort and sort <= latest_sort:
            continue
        book = book.get("book") or book
        title = book.get("title") or ""
        cover = (book.get("cover") or "").replace("/s_", "/t7_")
        bookId = book.get("bookId")
        author = book.get("author") or ""
        if not bookId:
            continue
        print(f"正在同步 {title} ,一共{len(books)}本，当前是第{index+1}本。")
        # 单本隔离：一本书失败不影响后面的书
        try:
            book_link = build_book_link(bookId)
            categories = book.get("categories")
            if categories != None:
                categories = [x["title"] for x in categories]
            delete_existing(book_link)
            if has_any_property(("ISBN", "评分")):
                isbn, rating = get_bookinfo(bookId)
            else:
                isbn, rating = "", None
            page_id = insert_to_notion(
                title, bookId, cover, sort, author, isbn, rating, categories
            )
            chapter = get_chapter_info(bookId)
            bookmark_list = get_bookmark_list(bookId)
            summary, reviews = get_review_list(bookId)
            bookmark_list.extend(reviews)
            bookmark_list = sorted(
                bookmark_list,
                key=lambda x: get_note_sort_key(x, chapter),
            )
            children, grandchild = get_children(chapter, summary, bookmark_list)
            results = add_children(page_id, children)
            if len(grandchild) > 0 and results != None:
                add_grandchild(grandchild, results)
        except Exception as e:
            failures.append((title, e))
            print(f"  [失败] {title}: {e}")
    report_failures(failures, len(books))


def backfill():
    """只补缺失字段，绝不删除或重建任何页面，也不碰正文。
    用于修复历史数据（早期同步漏掉的类型 / 开始时间 / 结束时间 / 阅读时长 / Sort）。

    数据源是 /shelf/sync 全量书架而不是「有笔记的书」：书架里绝大多数书没有笔记，
    但它们的 Sort 同样可能是空的，只遍历笔记书会漏掉绝大多数记录。
    """
    setup_clients()
    books = get_bookshelf()
    note_sort = get_notebook_sort_map()
    print(f"书架共 {len(books)} 本，其中 {len(note_sort)} 本有笔记（提供权威 Sort）")
    updated = 0
    failures = []
    for b in books:
        bookId = b.get("bookId")
        title = b.get("title") or ""
        if not bookId:
            continue
        try:
            # Sort 来源与 sync 保持一致：笔记排序优先，其次最后阅读时间
            raw_sort = note_sort.get(bookId) or b.get("readUpdateTime")
            existing = find_existing(build_book_link(bookId))
            if not existing:
                print(f"[跳过] {title}：Notion 中还没有这条记录")
                continue
            page = existing[0]
            props = page.get("properties") or {}
            missing = [
                name
                for name in ("类型", "开始时间", "结束时间", "阅读时长", "Sort")
                if name in data_source_property_types and is_property_empty(props.get(name))
            ]
            if not missing:
                print(f"[无缺失] {title}")
                continue
            read_info = get_read_info(bookId=bookId)
            raw = {}
            if "类型" in missing:
                raw["类型"] = WEREAD_ITEM_TYPE
            if "Sort" in missing and normalize_sort(raw_sort):
                raw["Sort"] = normalize_sort(raw_sort)
            if "开始时间" in missing:
                started = timestamp_to_local(read_info.get("startedDate"))
                if started:
                    raw["开始时间"] = started
            if "结束时间" in missing:
                finished = timestamp_to_local(read_info.get("finishedDate"))
                if finished:
                    raw["结束时间"] = finished
            if "阅读时长" in missing:
                seconds = to_number(read_info.get("readingTime")) or 0
                if seconds:
                    raw["阅读时长"] = reading_time_value(seconds)
            if not raw:
                hint = []
                if "结束时间" in missing:
                    hint.append(
                        "结束时间：该书尚未读完，微信读书只在读完后才返回 finishTime（正常）"
                        if read_info.get("markedStatus") != 4
                        else "结束时间：接口未返回"
                    )
                if "开始时间" in missing:
                    hint.append(
                        "开始时间：接口未返回有效的 startReadingTime（可能从未正式开始阅读）"
                    )
                if "阅读时长" in missing:
                    hint.append("阅读时长：接口未返回")
                if "Sort" in missing:
                    hint.append(
                        "Sort：微信读书未返回最后阅读时间（该书可能从未打开）"
                    )
                if "类型" in missing:
                    hint.append("类型：无法写入")
                print(f"[接口无数据] {title} -> {'；'.join(hint) or '微信读书未返回对应值'}")
                continue
            payload = build_notion_properties(raw)
            if not payload:
                print(f"[跳过] {title}：没有可写入的值")
                continue
            client.pages.update(page_id=page["id"], properties=payload)
            updated += 1
            print(f"[已补全] {title} -> {list(payload.keys())}")
        except Exception as e:
            failures.append((title, e))
            print(f"  [失败] {title}: {e}")
    print(f"\n回填完成，共更新 {updated} 条记录")
    if failures:
        report_failures(failures, len(books))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="weread2notion",
        description="Sync WeRead highlights and notes to Notion.",
        epilog=(
            "可用环境变量：WEREAD_SYNC_MODE=shelf|notebooks（默认 shelf，全量书架）、"
            "WEREAD_REFRESH_MODE=full|skip（skip 时只更新阅读状态，不重写划线正文）、"
            "WEREAD_STRICT=true（有书失败即以非 0 退出）、"
            "NOTION_WRITE_INTERVAL=0.3（Notion 写操作节流秒数）"
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="sync",
        choices=["sync", "backfill"],
        help="Command to run. Defaults to sync. backfill 只补缺失字段，不删除任何页面。",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "backfill":
            backfill()
        else:
            sync()
    except ConfigError:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
