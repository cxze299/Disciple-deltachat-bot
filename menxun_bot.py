#!/usr/bin/env python3
"""门训同行 Delta Chat bot：连接门训网站，提供任务、进度、绑定和打卡。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import threading
import time
import html as html_lib
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from deltachat2 import ChatType, MessageData, events
from deltabot_cli import BotCli

from admin_key import load_key_record, verify_key

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("MENXUN_DATA_DIR", ROOT / "data"))
STATE_FILE = DATA_DIR / "state.json"
HEALTH_FILE = DATA_DIR / "health.json"
BOT_NAME = os.getenv("BOT_NAME", "门训同行")
TIMEZONE = os.getenv("BOT_TIMEZONE", "Asia/Shanghai")
WEBSITE_URL = os.getenv("MENXUN_WEBSITE_URL", "http://127.0.0.1:3000").rstrip("/")
MORNING_REMINDER_TIME = os.getenv("MORNING_REMINDER_TIME", "08:30")
EVENING_REMINDER_TIME = os.getenv("EVENING_REMINDER_TIME", os.getenv("REMINDER_TIME", "20:30"))
CHAT_IDS = {int(x) for x in os.getenv("CHAT_IDS", "").split(",") if x.strip().isdigit()}
SITES_FILE = Path(os.getenv("MENXUN_SITES_FILE", ROOT / "sites.json"))
ADMIN_KEY_FILE = Path(os.getenv("MENXUN_ADMIN_KEY_FILE", DATA_DIR / "admin-key.json"))


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    name: str
    url: str
    chat_ids: frozenset[int]
    timezone: str = TIMEZONE
    morning_time: str = MORNING_REMINDER_TIME
    evening_time: str = EVENING_REMINDER_TIME


def parse_chat_ids(value) -> frozenset[int]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return frozenset(int(item) for item in values if str(item).strip().isdigit())


def validate_site_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if url.startswith("[") or not re.match(r"^https?://[^\s]+$", url):
        raise RuntimeError(f"门训网站地址格式错误：{url!r}；必须是纯 http/https 网址。")
    return url


def validate_reminder_time(value: str, site_id: str, field: str) -> str:
    clean = str(value or "").strip()
    if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", clean):
        raise RuntimeError(f"网站 {site_id} 的 {field} 必须是 HH:MM：{clean!r}")
    return clean


def load_sites() -> tuple[SiteConfig, ...]:
    raw_json = os.getenv("MENXUN_SITES_JSON", "").strip()
    source = None
    if raw_json:
        source = json.loads(raw_json)
    elif SITES_FILE.exists():
        source = json.loads(SITES_FILE.read_text(encoding="utf-8-sig"))

    rows = source.get("sites", []) if isinstance(source, dict) else source
    if not isinstance(rows, list) or not rows:
        rows = [{
            "id": "default",
            "name": "默认门训",
            "url": WEBSITE_URL,
            "chat_ids": sorted(CHAT_IDS),
            "timezone": TIMEZONE,
            "morning_time": MORNING_REMINDER_TIME,
            "evening_time": EVENING_REMINDER_TIME,
        }]

    sites: list[SiteConfig] = []
    seen_ids: set[str] = set()
    seen_chats: dict[int, str] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or row.get("enabled") is False:
            continue
        site_id = str(row.get("id") or f"site{index}").strip().lower()
        if not re.match(r"^[a-z0-9_-]+$", site_id):
            raise RuntimeError(f"网站 id 只能使用英文字母、数字、下划线或短横线：{site_id!r}")
        if site_id in seen_ids:
            raise RuntimeError(f"网站 id 重复：{site_id}")
        seen_ids.add(site_id)
        chat_ids = parse_chat_ids(row.get("chat_ids", row.get("chatIds", [])))
        for chat_id in chat_ids:
            if chat_id in seen_chats:
                raise RuntimeError(f"群聊 {chat_id} 同时属于网站 {seen_chats[chat_id]} 和 {site_id}")
            seen_chats[chat_id] = site_id
        timezone = str(row.get("timezone") or TIMEZONE).strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise RuntimeError(f"网站 {site_id} 的时区无效：{timezone}") from error
        morning_time = validate_reminder_time(
            row.get("morning_time") or row.get("morningTime") or MORNING_REMINDER_TIME,
            site_id,
            "morning_time",
        )
        evening_time = validate_reminder_time(
            row.get("evening_time") or row.get("eveningTime") or EVENING_REMINDER_TIME,
            site_id,
            "evening_time",
        )
        if morning_time == evening_time:
            raise RuntimeError(f"网站 {site_id} 的早晚提醒时间不能相同：{morning_time}")
        sites.append(SiteConfig(
            site_id=site_id,
            name=str(row.get("name") or site_id).strip(),
            url=validate_site_url(row.get("url")),
            chat_ids=chat_ids,
            timezone=timezone,
            morning_time=morning_time,
            evening_time=evening_time,
        ))
    if not sites:
        raise RuntimeError("没有启用任何门训网站。")
    return tuple(sites)


SITES = load_sites()
SITE_BY_ID = {site.site_id: site for site in SITES}
SITE_BY_CHAT_ID = {chat_id: site for site in SITES for chat_id in site.chat_ids}
DEFAULT_SITE = SITES[0]

cli = BotCli("menxun-bot")
state_lock = threading.Lock()
reminder_started = False
config_cache_lock = threading.Lock()
config_cache: dict[str, tuple[float, dict]] = {}
devotion_cache: dict[str, tuple[float, str]] = {}
admin_attempt_lock = threading.Lock()
admin_failed_attempts: dict[int, list[float]] = {}


@cli.on(events.RawEvent)
def on_raw_event(bot, accid: int, event) -> None:
    event_name = type(event).__name__
    if event_name not in {"EventTypeInfo", "EventTypeWarning"}:
        bot.logger.debug("Delta Chat event: account=%s type=%s", accid, event_name)


def now(site: SiteConfig | None = None) -> datetime:
    timezone = site.timezone if site else TIMEZONE
    try:
        return datetime.now(ZoneInfo(timezone))
    except ZoneInfoNotFoundError as error:
        raise RuntimeError(
            f"缺少时区数据库 {timezone}，请执行：python -m pip install tzdata；原始错误：{error}"
        ) from error


def load_state() -> dict:
    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        loaded = {}
    loaded.setdefault("checkins", {})
    loaded.setdefault("reminded", {})
    loaded.setdefault("bindings", {})
    loaded.setdefault("active_sites", {})
    loaded.setdefault("admins", {})
    loaded.setdefault("welcomed", {})
    loaded.setdefault("website_records", {})
    loaded.setdefault("recent_announcements", {})
    if not isinstance(loaded["admins"], dict):
        loaded["admins"] = {}
    if not isinstance(loaded["welcomed"], dict):
        loaded["welcomed"] = {}
    if not isinstance(loaded["website_records"], dict):
        loaded["website_records"] = {}
    if not isinstance(loaded["recent_announcements"], dict):
        loaded["recent_announcements"] = {}
    return loaded


state = load_state()


def save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def write_health(status: str = "running") -> None:
    """写入容器健康检查心跳；不包含账号、消息或网站凭据。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = HEALTH_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "status": status,
        "updated_at": datetime.now().astimezone().isoformat(),
        "sites": len(SITES),
    }, ensure_ascii=False), encoding="utf-8")
    temporary.replace(HEALTH_FILE)


def is_admin(member_id: int) -> bool:
    return str(member_id) in state["admins"]


def bind_admin(member_id: int) -> None:
    with state_lock:
        state["admins"][str(member_id)] = {"verified_at": datetime.now().astimezone().isoformat()}
        save_state()


def unbind_admin(member_id: int) -> None:
    with state_lock:
        state["admins"].pop(str(member_id), None)
        save_state()


def verify_admin_attempt(member_id: int, secret: str) -> tuple[bool, str]:
    """验证管理员密钥，并限制单个用户的短时间暴力尝试。"""
    record = load_key_record(ADMIN_KEY_FILE)
    if not record:
        return False, "机器人尚未设置管理员密钥。请先在服务器运行 set_admin_key.py。"
    current = time.monotonic()
    with admin_attempt_lock:
        failures = [stamp for stamp in admin_failed_attempts.get(member_id, []) if current - stamp < 600]
        admin_failed_attempts[member_id] = failures
        if len(failures) >= 5:
            return False, "验证失败次数过多，请 10 分钟后再试。"
    if verify_key(secret, record):
        with admin_attempt_lock:
            admin_failed_attempts.pop(member_id, None)
        return True, "验证成功。"
    with admin_attempt_lock:
        admin_failed_attempts.setdefault(member_id, []).append(current)
    return False, "管理员密钥不正确。"


def safe_log_text(raw_text: str) -> str:
    if re.match(r"^管理员\s*验证", raw_text, flags=re.IGNORECASE):
        return "管理员验证 ***"
    return raw_text


def send(bot, accid: int, chat_id: int, text: str) -> None:
    if "**" not in text:
        bot.rpc.send_msg(accid, chat_id, MessageData(text=text))
        return
    plain_text = text.replace("**", "")
    escaped = html_lib.escape(text)
    formatted = re.sub(r"\*\*([\s\S]*?)\*\*", r"<strong>\1</strong>", escaped)
    html = "<div>" + formatted.replace("\n", "<br>") + "</div>"
    bot.rpc.send_msg(accid, chat_id, MessageData(text=plain_text, html=html))


def welcome_text() -> str:
    return "\n".join([
        f"你好，欢迎使用{BOT_NAME}。",
        "",
        "首次使用（请在这里私聊操作）",
        "1. 发送：网站",
        "2. 直接回复你所在的网站名称",
        "3. 发送：绑定 你的姓名",
        "4. 发送：打卡 灵修",
        "",
        "发送“帮助”可查看全部指令。",
    ])


def send_private_welcome_once(bot, accid: int, chat_id: int, member_id: int, is_group: bool) -> bool:
    """在账号首次与机器人建立私聊后发送一次引导。"""
    if is_group or member_id <= 9:
        return False
    member_key = str(member_id)
    with state_lock:
        if state["welcomed"].get(member_key):
            return False
        send(bot, accid, chat_id, welcome_text())
        state["welcomed"][member_key] = datetime.now().astimezone().isoformat()
        save_state()
    return True


def fetch_json(site: SiteConfig, path: str) -> dict:
    """读取 NAS API；对 Synology 反向代理偶发的 TLS EOF 做有限重试。"""
    url = f"{site.url}{path}"
    last_error: Exception | None = None
    for attempt in range(4):
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Connection": "close",
                "User-Agent": "MenxunDeltaChatBot/1.0",
            },
        )
        try:
            with urlopen(request, timeout=12) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError:
            raise
        except (URLError, TimeoutError, ssl.SSLError, ConnectionError, json.JSONDecodeError) as error:
            last_error = error
            if attempt == 3:
                break
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"读取门训网站失败（已重试 4 次）：{url}；{last_error}") from last_error


def fetch_text(site: SiteConfig, path: str) -> str:
    url = urljoin(f"{site.url}/", str(path or "").lstrip("./"))
    with config_cache_lock:
        cached = devotion_cache.get(url)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
    last_error: Exception | None = None
    for attempt in range(4):
        request = Request(url, headers={
            "Accept": "text/markdown,text/plain,*/*",
            "Connection": "close",
            "User-Agent": "MenxunDeltaChatBot/1.0",
        })
        try:
            with urlopen(request, timeout=15) as response:
                text = response.read().decode("utf-8-sig", errors="replace")
            with config_cache_lock:
                devotion_cache[url] = (time.monotonic(), text)
            return text
        except HTTPError:
            raise
        except (URLError, TimeoutError, ssl.SSLError, ConnectionError) as error:
            last_error = error
            if attempt == 3:
                break
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"读取灵修内容失败（已重试 4 次）：{url}；{last_error}") from last_error


def post_json(site: SiteConfig, path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{site.url}{path}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": "MenxunDeltaChatBot/1.0",
        },
    )
    with urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def delete_json(site: SiteConfig, path: str) -> dict:
    """DELETE 可安全重试；若首次请求已成功但连接中断，重试得到 404 也视为已删除。"""
    url = f"{site.url}{path}"
    last_error: Exception | None = None
    for attempt in range(4):
        request = Request(
            url,
            method="DELETE",
            headers={"Accept": "application/json", "Connection": "close", "User-Agent": "MenxunDeltaChatBot/1.0"},
        )
        try:
            with urlopen(request, timeout=12) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code == 404:
                return {"ok": True, "alreadyDeleted": True}
            raise
        except (URLError, TimeoutError, ssl.SSLError, ConnectionError, json.JSONDecodeError) as error:
            last_error = error
            if attempt == 3:
                break
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"取消门训打卡失败（已重试 4 次）：{url}；{last_error}") from last_error


def website_config(site: SiteConfig) -> dict:
    """配置缓存 60 秒，减少 NAS TLS 连接次数；状态数据始终实时读取。"""
    current_time = time.monotonic()
    with config_cache_lock:
        cached = config_cache.get(site.site_id)
        if cached and current_time - cached[0] < 60:
            return cached[1]
    config = fetch_json(site, "/api/config")
    with config_cache_lock:
        config_cache[site.site_id] = (current_time, config)
    return config


def website_snapshot(site: SiteConfig) -> tuple[dict, dict]:
    """读取指定网站当前成员、周计划、任务配置和打卡记录。"""
    return fetch_json(site, "/api/state"), website_config(site)


def format_title(value) -> str:
    if isinstance(value, list):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def current_week(source: dict | list, today: date) -> dict | None:
    if isinstance(source, dict):
        plans = source.get("weekly_schedule") or source.get("weeklySchedule") or []
    else:
        plans = source or []
    for plan in plans:
        try:
            if date.fromisoformat(str(plan.get("start"))) <= today <= date.fromisoformat(str(plan.get("end"))):
                return plan
        except (TypeError, ValueError):
            continue
    return None


def frontend_text(value) -> str:
    """按网站前端 String(value) 的方式生成任务标识。"""
    if isinstance(value, list):
        return ",".join(str(item or "").strip() for item in value)
    return str(value or "").strip()


def record_logical_date(record: dict, site: SiteConfig | None = None) -> str:
    """读取网站记录的逻辑日期，兼容 API 字段和旧导入字段。"""
    explicit = str(record.get("logical_date") or record.get("逻辑日期") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", explicit):
        return explicit[:10]
    detail = str(record.get("detail") or record.get("打卡详情") or "")
    month_day = re.search(r"(\d{1,2})月(\d{1,2})日", detail)
    if month_day:
        year = now(site).year
        checkin_time = str(record.get("checkin_time") or record.get("打卡时间") or "")
        try:
            year = datetime.fromisoformat(checkin_time.replace("Z", "+00:00")).year
        except ValueError:
            pass
        try:
            return date(year, int(month_day.group(1)), int(month_day.group(2))).isoformat()
        except ValueError:
            pass
    return ""


def is_done_value(value) -> bool:
    """与门训网站前端 isDoneValue 保持一致。"""
    return value in {"done", "已完成"}


def record_name(record: dict) -> str:
    return str(record.get("name") or record.get("姓名") or "").strip()


def record_is_retro(record: dict) -> bool:
    value = record.get("is_retro", record.get("是否补签", ""))
    return str(value).strip().lower() in {"yes", "true", "1", "是"}


def weekly_task_value(plan: dict, checkin_type: str) -> str:
    """取得网站用于判断周任务状态的当前任务标识。"""
    if checkin_type == "周读物":
        return frontend_text(plan.get("title"))
    if checkin_type == "周视频":
        videos = plan.get("videos") or plan.get("videoList") or plan.get("video_list") or []
        if isinstance(videos, list) and videos:
            first = videos[0]
            return frontend_text(first.get("title") if isinstance(first, dict) else first)
        return frontend_text(plan.get("video"))
    if checkin_type == "周背经":
        return frontend_text(plan.get("verse"))
    return ""


def website_record_matches(
    record: dict,
    name: str,
    checkin_type: str,
    target_date: date,
    weekly_schedule: list,
    site: SiteConfig | None = None,
) -> bool:
    """完全按照网站前端状态逻辑判断一条记录是否代表任务已完成。"""
    if record_name(record) != name.strip():
        return False
    column = {"每日灵修": "daily", "周读物": "book", "周视频": "video", "周背经": "verse"}[checkin_type]
    if not is_done_value(record.get(column)) and not is_done_value(record.get({
        "每日灵修": "每日灵修",
        "周读物": "周读物",
        "周视频": "周视频",
        "周背经": "周背经",
    }[checkin_type])):
        return False
    record_date_text = record_logical_date(record, site)
    if checkin_type == "每日灵修":
        return record_date_text == target_date.isoformat()
    if not record_date_text:
        return False
    target_plan = current_week(weekly_schedule, target_date)
    try:
        record_date = date.fromisoformat(record_date_text)
    except ValueError:
        return False
    record_plan = current_week(weekly_schedule, record_date)
    if not target_plan or not record_plan:
        return False
    target_value = weekly_task_value(target_plan, checkin_type)
    record_value = weekly_task_value(record_plan, checkin_type)
    return bool(target_value) and record_value == target_value


def current_scripture(config: dict, today: date) -> str:
    scripture = ((config.get("task_sections") or {}).get("daily") or {}).get("scripture") or {}
    start_raw = scripture.get("start_date")
    if not start_raw:
        return ""
    try:
        offset = (today - date.fromisoformat(str(start_raw))).days
    except ValueError:
        return ""
    chapter = int(scripture.get("start_chapter") or 1) + max(0, offset)
    for item in scripture.get("sequence") or []:
        chapters = int(item.get("chapters") or 0)
        if chapter <= chapters:
            return f"{item.get('book', '')} 第 {chapter} 章"
        chapter -= chapters
    return ""


def chinese_calendar_number(value: int) -> str:
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    if value == 10:
        return "十"
    if value < 20:
        return "十" + digits[value % 10]
    tens, ones = divmod(value, 10)
    return digits[tens] + "十" + (digits[ones] if ones else "")


def clean_devotion_markdown(lines: list[str]) -> str:
    text = "\n".join(lines)
    text = re.sub(r"</?div[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"!\[[^]]*]\([^)]*\)", "", text)
    text = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(?<!\*)「([\s\S]*?)」(?!\*)", r"**「\1」**", text)
    text = re.sub(r"(?<!\*)“([\s\S]*?)”(?!\*)", r"**“\1”**", text)
    text = re.sub(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_devotion_section(markdown: str, devotion: dict, target_date: date) -> str:
    lines = markdown.splitlines()
    source_path = str(devotion.get("path") or devotion.get("url") or "")
    mode = str(devotion.get("mode") or "").strip().lower()
    if mode == "numbered" or (not mode and source_path.lower().endswith("newtestament.md")):
        start_raw = devotion.get("numbered_start_date") or devotion.get("start_date") or target_date.isoformat()
        try:
            offset = (target_date - date.fromisoformat(str(start_raw))).days
        except ValueError:
            offset = 0
        section_number = max(1, int(devotion.get("numbered_start") or devotion.get("start_section") or 1) + offset)
        start_pattern = re.compile(rf"^#{{1,6}}\s*{section_number}\s*$")
        stop_pattern = re.compile(r"^#{1,6}\s*\d+\s*$")
        captured: list[str] = []
        active = False
        for raw_line in lines:
            line = raw_line.strip()
            if not active:
                if start_pattern.match(line):
                    active = True
                continue
            if stop_pattern.match(line):
                break
            captured.append(raw_line)
        numbered_content = clean_devotion_markdown(captured)
        if numbered_content:
            return numbered_content
        # 部分旧网站把日期型 Kuangye.md 误标成 numbered；未命中时继续按日期查找。

    month = target_date.month
    day = target_date.day
    targets = {
        f"{month}月{day}日",
        f"{chinese_calendar_number(month)}月{chinese_calendar_number(day)}日",
    }
    date_heading = re.compile(
        r"^(?:#{1,6}\s*)?(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*月\s*"
        r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*日"
    )
    captured = []
    active = False
    for raw_line in lines:
        line = raw_line.strip()
        normalized = re.sub(r"^#{1,6}\s*", "", line)
        if not active:
            if any(normalized.startswith(target) for target in targets) and len(normalized) < 100:
                active = True
                suffix = normalized
                for target in targets:
                    if suffix.startswith(target):
                        suffix = suffix[len(target):].lstrip(" -|:：")
                        break
                if suffix:
                    captured.append(suffix)
            continue
        if date_heading.match(line) and not any(normalized.startswith(target) for target in targets):
            break
        captured.append(raw_line)
    return clean_devotion_markdown(captured)


def daily_devotion_text(site: SiteConfig, target_date: date | None = None) -> str:
    target_date = target_date or now(site).date()
    _, config = website_snapshot(site)
    daily = ((config.get("task_sections") or {}).get("daily") or {})
    devotion = daily.get("devotion") or daily
    source_path = devotion.get("path") or devotion.get("url") or daily.get("path") or daily.get("url")
    title = str(devotion.get("title") or daily.get("label") or "每日灵修").strip()
    if not source_path:
        return f"📖 {site.name} · {target_date.isoformat()}\n该网站暂未配置可读取的灵修内容。"
    markdown = fetch_text(site, str(source_path))
    content = extract_devotion_section(markdown, devotion, target_date)
    if not content:
        return f"📖 {site.name} · {target_date.isoformat()}\n没有找到当天的灵修内容。"
    return f"📖 {site.name} · {target_date.isoformat()}\n{title}\n\n{content}"


def task_summary(config: dict, today: date | None = None, site: SiteConfig | None = None) -> str:
    today = today or now(site).date()
    sections = config.get("task_sections") or {}
    daily = sections.get("daily") or {}
    devotion = daily.get("devotion") or {}
    lines = [f"📅 {today.isoformat()} 今日任务"]
    if devotion.get("title"):
        lines.append(f"☀️ 灵修：{devotion['title']}")
    scripture = current_scripture(config, today)
    if scripture:
        lines.append(f"📖 读经：{scripture}")
    plan = current_week(config, today)
    if plan:
        lines.append(f"📚 本周：{format_title(plan.get('title'))}")
        readings = plan.get("readings") or []
        for reading in readings[:3]:
            if isinstance(reading, dict) and reading.get("title"):
                lines.append(f"  · 周读物：{reading['title']}")
        videos = plan.get("videos") or []
        video = (videos[0].get("title") if videos and isinstance(videos[0], dict) else plan.get("video"))
        if video:
            lines.append(f"🎬 视频：{video}")
        if plan.get("verse") or plan.get("reciteText"):
            lines.append(f"📝 背经：{plan.get('verse') or plan.get('reciteText')}")
    else:
        lines.append("📚 本周任务：网站暂未配置当前周计划")
    return "\n".join(lines)


def help_text(site: SiteConfig | None = None) -> str:
    lines = [
        f"{BOT_NAME} · 帮助",
        "",
    ]
    if len(SITES) > 1:
        lines.extend([
            "首次使用（请私聊）",
            "1. 发送“网站”",
            "2. 直接回复你所在的网站名称",
            "3. 发送“绑定 你的姓名”",
            "",
        ])
    else:
        lines.extend(["首次使用（请私聊）", "绑定 你的姓名 — 绑定身份", ""])
    lines.extend([
        "日常打卡",
        "灵修 — 阅读当天灵修内容",
        "打卡 灵修",
        "打卡 周读物",
        "打卡 视频",
        "打卡 背经",
        "补签 项目 日期",
        "取消打卡 项目 日期",
        "",
        "查看进度",
        "我的状态 — 查看个人完成情况",
        "我的月状态 — 查看个人本月月历",
        "群状态 — 查看全群完成情况",
        "任务 — 查看今日和本周任务",
        "",
        "日期可填写：2026-08-20、昨天、前天",
        "管理员帮助 — 查看管理员指令",
    ])
    return "\n".join(lines)


def resolve_message_site(bot, accid: int, chat_id: int, member_id: int) -> tuple[bool, SiteConfig | None]:
    chat = bot.rpc.get_basic_chat_info(accid, chat_id)
    if chat.chat_type == ChatType.GROUP:
        return True, SITE_BY_CHAT_ID.get(chat_id)
    active_site_id = str(state["active_sites"].get(str(member_id), ""))
    if active_site_id in SITE_BY_ID:
        return False, SITE_BY_ID[active_site_id]
    bindings = state["bindings"].get(str(member_id), {})
    if isinstance(bindings, str) and bindings.strip():
        return False, DEFAULT_SITE
    if isinstance(bindings, dict):
        bound_sites = [SITE_BY_ID[site_id] for site_id in bindings if site_id in SITE_BY_ID]
        if len(bound_sites) == 1:
            return False, bound_sites[0]
    return False, DEFAULT_SITE if len(SITES) == 1 else None


def bound_name(member_id: int, site: SiteConfig) -> str:
    binding = state["bindings"].get(str(member_id), {})
    if isinstance(binding, str):
        return binding.strip() if site.site_id == DEFAULT_SITE.site_id else ""
    if isinstance(binding, dict):
        return str(binding.get(site.site_id, "")).strip()
    return ""


def bind_member(member_id: int, site: SiteConfig, name: str, members: list[str]) -> bool:
    if name not in members:
        return False
    with state_lock:
        member_key = str(member_id)
        old_binding = state["bindings"].get(member_key, {})
        if isinstance(old_binding, str):
            old_binding = {DEFAULT_SITE.site_id: old_binding}
        if not isinstance(old_binding, dict):
            old_binding = {}
        old_binding[site.site_id] = name
        state["bindings"][member_key] = old_binding
        state["active_sites"][member_key] = site.site_id
        save_state()
    return True


def site_list_text(member_id: int = 0) -> str:
    active_id = str(state["active_sites"].get(str(member_id), ""))
    if not active_id:
        binding = state["bindings"].get(str(member_id), {})
        if isinstance(binding, str) and binding.strip():
            active_id = DEFAULT_SITE.site_id
        elif isinstance(binding, dict):
            configured = [site_id for site_id in binding if site_id in SITE_BY_ID]
            if len(configured) == 1:
                active_id = configured[0]
        if not active_id and len(SITES) == 1:
            active_id = DEFAULT_SITE.site_id
    lines = ["🌐 请选择门训网站："]
    for site in SITES:
        marker = " ✅" if site.site_id == active_id else ""
        lines.append(f"· {site_choice_name(site)}{marker}")
    lines.extend([
        "",
        "请直接回复上面的网站名称。",
        "例如：科大",
        "",
        "选择后，机器人会告诉你下一步怎么做。",
    ])
    return "\n".join(lines)


def site_choice_name(site: SiteConfig) -> str:
    return {
        "zk": "科大",
        "agape": "Agape",
        "zhewai": "浙外",
        "longway": "Longway",
        "tianlu": "天路历程",
    }.get(site.site_id, site.name)


def find_site(value: str) -> SiteConfig | None:
    clean = value.strip().lower()
    alias_id = {
        "科大": "zk",
        "科大门训": "zk",
        "agape": "agape",
        "浙外": "zhewai",
        "浙外门训": "zhewai",
        "longway": "longway",
        "天路": "tianlu",
        "天路历程": "tianlu",
    }.get(clean)
    return SITE_BY_ID.get(clean) or SITE_BY_ID.get(alias_id or "") or next(
        (site for site in SITES if site.name.lower() == clean),
        None,
    )


def parse_binding(text: str, current_site: SiteConfig | None) -> tuple[SiteConfig | None, str]:
    remainder = re.sub(r"^绑定\s*[:：]?\s*", "", text.strip(), flags=re.IGNORECASE)
    first, separator, rest = remainder.partition(" ")
    explicit_site = find_site(first) if separator else None
    return (explicit_site, rest.strip()) if explicit_site else (current_site, remainder.strip())


def reminder_due(current: datetime, target_time: str, grace_minutes: int = 10) -> bool:
    """在目标时间后的短暂宽限期内发送，避免重启或短暂停顿漏掉整天提醒。"""
    hour, minute = (int(part) for part in target_time.split(":"))
    current_minutes = current.hour * 60 + current.minute
    target_minutes = hour * 60 + minute
    return 0 <= current_minutes - target_minutes < grace_minutes


def record_local_checkin(site: SiteConfig, chat_id: int, member_id: int, name: str, kind: str, logical_date: str | None = None) -> None:
    key = f"{site.site_id}:{logical_date or now(site).date().isoformat()}:{chat_id}"
    with state_lock:
        state["checkins"].setdefault(key, {})[f"{member_id}:{kind}"] = {"name": name, "at": now().isoformat()}
        save_state()


def kind_from_text(text: str) -> tuple[str, str]:
    if any(word in text for word in ("视频", "周视频")):
        return "周视频", "video"
    if any(word in text for word in ("背经", "周背经")):
        return "周背经", "verse"
    if any(word in text for word in ("周读物", "基督", "救赎")):
        return "周读物", "book"
    return "每日灵修", "daily"


TASK_KINDS = {
    "灵修": "每日灵修",
    "每日灵修": "每日灵修",
    "读经": "每日灵修",
    "灵修读经": "每日灵修",
    "周读物": "周读物",
    "读物": "周读物",
    "视频": "周视频",
    "周视频": "周视频",
    "背经": "周背经",
    "周背经": "周背经",
}


def parse_task_kind(value: str, default_daily: bool = True) -> str | None:
    clean = re.sub(r"[\s:：]+", "", value.strip().rstrip("！!。.")).lower()
    if not clean:
        return "每日灵修" if default_daily else None
    return TASK_KINDS.get(clean)


def parse_retro_command(text: str, site: SiteConfig | None = None) -> tuple[str, str | None]:
    """解析“补签 [任务类型] 日期”，支持昨天/前天。"""
    remainder = re.sub(r"^(补签|补打卡)\s*", "", text.strip(), flags=re.IGNORECASE)
    if "昨天" in remainder:
        return remainder.replace("昨天", ""), (now(site).date() - timedelta(days=1)).isoformat()
    if "前天" in remainder:
        return remainder.replace("前天", ""), (now(site).date() - timedelta(days=2)).isoformat()
    match = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?", remainder)
    if not match:
        return remainder, None
    try:
        logical_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return remainder, None
    return remainder[:match.start()] + remainder[match.end():], logical_date


def parse_cancel_command(text: str, site: SiteConfig | None = None) -> tuple[str, str | None]:
    """解析取消打卡命令，未指定日期时默认取消今天。"""
    remainder = re.sub(r"^取消(?:补签|打卡|签到)\s*", "", text.strip(), flags=re.IGNORECASE)
    logical_date = now(site).date().isoformat()
    if "昨天" in remainder:
        remainder = remainder.replace("昨天", "")
        logical_date = (now(site).date() - timedelta(days=1)).isoformat()
    elif "前天" in remainder:
        remainder = remainder.replace("前天", "")
        logical_date = (now(site).date() - timedelta(days=2)).isoformat()
    else:
        match = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?", remainder)
        if match:
            try:
                logical_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
                remainder = remainder[:match.start()] + remainder[match.end():]
            except ValueError:
                return remainder, None
    return remainder, logical_date


def website_checkin(
    site: SiteConfig,
    name: str,
    checkin_type: str,
    detail: str = "",
    logical_date: str | None = None,
    is_retro: bool = False,
) -> tuple[bool, str]:
    website_state, config = website_snapshot(site)
    logical_date = logical_date or now(site).date().isoformat()
    target_date = date.fromisoformat(logical_date)
    weekly_schedule = website_state.get("weeklySchedule") or config.get("weekly_schedule") or []
    for record in website_state.get("records") or []:
        if website_record_matches(record, name, checkin_type, target_date, weekly_schedule, site):
            return False, f"{logical_date} 这项已经按照网站状态完成了。"
    post_json(
        site,
        "/api/checkins",
        {"name": name, "type": checkin_type, "logicalDate": logical_date, "isRetro": is_retro, "detail": detail},
    )
    return True, "已同步到门训打卡网站。"


def website_cancel_checkin(
    site: SiteConfig,
    name: str,
    checkin_type: str,
    logical_date: str,
    retro_only: bool = False,
) -> tuple[bool, str]:
    website_state, config = website_snapshot(site)
    target_date = date.fromisoformat(logical_date)
    weekly_schedule = website_state.get("weeklySchedule") or config.get("weekly_schedule") or []
    matches = [
        record for record in website_state.get("records") or []
        if website_record_matches(record, name, checkin_type, target_date, weekly_schedule, site)
        and (
            not retro_only
            or record_is_retro(record)
        )
    ]
    if not matches:
        label = "补签" if retro_only else ""
        return False, f"没有找到 {logical_date} 的{label}{checkin_type}记录。"
    record_ids = []
    for record in matches:
        record_id = record.get("Id") or record.get("id")
        if not record_id:
            return False, "找到了记录，但缺少记录编号，无法完整取消。"
        numeric_id = int(record_id)
        if numeric_id not in record_ids:
            record_ids.append(numeric_id)
    for record_id in record_ids:
        delete_json(site, f"/api/checkins/{record_id}")
    count_text = f"（共 {len(record_ids)} 条记录）" if len(record_ids) > 1 else ""
    return True, f"已取消 {logical_date} 的{checkin_type}记录{count_text}。"


def website_status(site: SiteConfig, name: str = "") -> str:
    website_state, config = website_snapshot(site)
    today = now(site).date()
    today_text = today.isoformat()
    weekly_schedule = website_state.get("weeklySchedule") or config.get("weekly_schedule") or []
    records = website_state.get("records") or []
    if name:
        values = {"daily": "灵修/读经", "book": "周读物", "video": "视频", "verse": "背经"}
        mine = []
        types = {"daily": "每日灵修", "book": "周读物", "video": "周视频", "verse": "周背经"}
        for column, label in values.items():
            checkin_type = types[column]
            done = any(website_record_matches(r, name, checkin_type, today, weekly_schedule, site) for r in records)
            mine.append(f"✅ {label}" if done else f"⬜ {label}")
        return f"🙋 {site.name} / {name}\n" + "\n".join(mine)
    types = {
        "daily": ("灵修/读经", "每日灵修"),
        "book": ("周读物", "周读物"),
        "video": ("视频", "周视频"),
        "verse": ("背经", "周背经"),
    }
    completed = {
        column: {
            record_name(r) for r in records
            if record_name(r) and website_record_matches(r, record_name(r), checkin_type, today, weekly_schedule, site)
        }
        for column, (_, checkin_type) in types.items()
    }
    members = [str(item).strip() for item in (website_state.get("members") or []) if str(item).strip()]
    member_order = {member: index for index, member in enumerate(members)}

    def ordered_names(names: set[str]) -> list[str]:
        return sorted(names, key=lambda value: (member_order.get(value, len(member_order)), value))

    lines = [f"📊 {site.name} · 群状态", f"日期：{today_text}｜成员：{len(members)} 人"]
    for column, (label, _) in types.items():
        names = ordered_names(completed[column])
        lines.extend([f"\n{label}（{len(names)} 人）", "、".join(names) if names else "暂无"])
    return "\n".join(lines)


def member_month_status(
    site: SiteConfig,
    name: str,
    month_text: str = "",
    today: date | None = None,
) -> str:
    """按照网站人物月历规则输出成员的文字月历。"""
    current_date = today or now(site).date()
    clean_month = month_text.strip()
    if clean_month:
        match = re.fullmatch(r"(20\d{2})[-年](\d{1,2})月?", clean_month)
        if not match:
            raise ValueError("月份格式无效")
        year, month = int(match.group(1)), int(match.group(2))
        if month < 1 or month > 12:
            raise ValueError("月份格式无效")
    else:
        year, month = current_date.year, current_date.month
    month_start = date(year, month, 1)
    current_month_start = current_date.replace(day=1)
    if month_start > current_month_start:
        raise ValueError("不能查看未来月份")

    website_state, config = website_snapshot(site)
    weekly_schedule = website_state.get("weeklySchedule") or config.get("weekly_schedule") or []
    records = website_state.get("records") or []
    last_day = monthrange(year, month)[1]
    if year == current_date.year and month == current_date.month:
        last_day = current_date.day

    task_types = ("每日灵修", "周读物", "周视频", "周背经")
    rows = []
    devotion_days = 0
    complete_days = 0
    for day_number in range(1, last_day + 1):
        target_date = date(year, month, day_number)
        done = [
            any(website_record_matches(record, name, task_type, target_date, weekly_schedule, site) for record in records)
            for task_type in task_types
        ]
        total_count = sum(done)
        daily_done = done[0]
        complete = daily_done and sum(done[1:]) >= 2
        devotion_days += int(daily_done)
        complete_days += int(complete)
        marker = "✅" if complete else "🟦" if daily_done else "🟨" if total_count else "⬜"
        rows.append(f"{day_number:02d}日  {marker} {total_count}/4")

    return "\n".join([
        f"📅 {site.name} · 人物月历",
        f"{name}｜{year}年{month}月",
        f"灵修：{devotion_days}/{last_day} 天｜完整：{complete_days}/{last_day} 天",
        "",
        *rows,
        "",
        "✅ 完整　🟦 已灵修　🟨 仅周任务　⬜ 未完成",
    ])


def record_datetime(record: dict, site: SiteConfig) -> datetime | None:
    raw = str(record.get("checkin_time") or record.get("打卡时间") or "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(site.timezone))
        return value.astimezone(ZoneInfo(site.timezone))
    except ValueError:
        return None


def checkin_timeline(
    site: SiteConfig,
    checkin_type: str,
    logical_date: str,
    excluded_names: frozenset[str] = frozenset(),
) -> tuple[str, list[str]]:
    """按网站现有状态生成某项任务的完成名单，每人只保留最早完成时间。"""
    website_state, config = website_snapshot(site)
    target_date = date.fromisoformat(logical_date)
    weekly_schedule = website_state.get("weeklySchedule") or config.get("weekly_schedule") or []
    first_by_name: dict[str, tuple[float, datetime | None]] = {}
    for record in website_state.get("records") or []:
        name = record_name(record)
        if name in excluded_names or not name or not website_record_matches(record, name, checkin_type, target_date, weekly_schedule, site):
            continue
        value = record_datetime(record, site)
        sort_value = value.timestamp() if value else float("inf")
        if name not in first_by_name or sort_value < first_by_name[name][0]:
            first_by_name[name] = (sort_value, value)

    ordered = sorted(first_by_name.items(), key=lambda item: (item[1][0], item[0]))
    display_type = {"每日灵修": "灵修", "周读物": "周读物", "周视频": "视频", "周背经": "背经"}[checkin_type]
    scope = f"{logical_date} · " if checkin_type == "每日灵修" else "本周"
    title = f"{scope}{display_type}（按时间）"
    rows = []
    for index, (name, (_, value)) in enumerate(ordered, start=1):
        if value:
            time_text = value.strftime("%H:%M") if checkin_type == "每日灵修" else value.strftime("%m-%d %H:%M")
        else:
            time_text = "时间未知"
        rows.append(f"{index}. {time_text}  {name}")
    return title, rows


def build_group_update(
    site: SiteConfig,
    name: str,
    checkin_type: str,
    logical_date: str,
    cancelled: bool = False,
    retro: bool = False,
    operation_time: str = "",
    event_time: str = "",
) -> str:
    display_type = {"每日灵修": "灵修", "周读物": "周读物", "周视频": "视频", "周背经": "背经"}[checkin_type]
    if cancelled:
        headline = f"↩️ {name}取消了打卡：{display_type}"
    else:
        headline = f"✅ {name}打卡了：{display_type}"
    if retro:
        headline += f"（补签 {logical_date}）"
    if operation_time:
        headline += f"\n操作时间：{operation_time}"
    try:
        title, rows = checkin_timeline(
            site,
            checkin_type,
            logical_date,
            frozenset({name}) if cancelled else frozenset(),
        )
    except Exception:
        return headline + "\n名单暂时无法读取，请发送“群状态”重试。"
    if not rows and not cancelled:
        fallback_time = event_time or now(site).strftime("%m-%d %H:%M")
        return headline + f"\n\n本次记录\n1. {fallback_time}  {name}"
    return headline + f"\n\n{title}\n" + ("\n".join(rows) if rows else "暂无打卡")


def record_fingerprint(record: dict) -> str:
    record_id = record.get("Id", record.get("id", record.get("ID")))
    if record_id not in (None, ""):
        return f"id:{record_id}"
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_completed_types(record: dict) -> list[str]:
    columns = (
        ("每日灵修", "daily"),
        ("周读物", "book"),
        ("周视频", "video"),
        ("周背经", "verse"),
    )
    return [
        checkin_type
        for checkin_type, column in columns
        if is_done_value(record.get(column)) or is_done_value(record.get(checkin_type))
    ]


def compact_record(record: dict, site: SiteConfig) -> dict:
    return {
        "name": record_name(record),
        "logical_date": record_logical_date(record, site),
        "types": record_completed_types(record),
        "retro": record_is_retro(record),
    }


def announcement_key(
    site: SiteConfig,
    name: str,
    checkin_type: str,
    logical_date: str,
    action: str = "checkin",
) -> str:
    return f"{action}:{site.site_id}:{logical_date}:{name}:{checkin_type}"


def remember_announced_change(
    site: SiteConfig,
    name: str,
    checkin_type: str,
    logical_date: str,
    action: str = "checkin",
) -> None:
    with state_lock:
        state["recent_announcements"][announcement_key(site, name, checkin_type, logical_date, action)] = time.time()
        save_state()


def poll_website_notifications(bot, accid: int, site: SiteConfig) -> int:
    """检测网站新增和删除记录；首次运行仅建立基线。"""
    website_state, _ = website_snapshot(site)
    records = [record for record in (website_state.get("records") or []) if isinstance(record, dict)]
    current = {record_fingerprint(record): record for record in records}
    current_compact = {key: compact_record(record, site) for key, record in current.items()}
    with state_lock:
        has_baseline = site.site_id in state["website_records"]
        previous_payload = state["website_records"].get(site.site_id, {})
        previous_records = previous_payload if isinstance(previous_payload, dict) else {}
        previous = set(previous_records if previous_records else previous_payload)
        if not has_baseline:
            state["website_records"][site.site_id] = current_compact
            save_state()
            return 0

    delivered_events = 0
    operation_time = now(site).strftime("%Y-%m-%d %H:%M:%S")
    for fingerprint in sorted(previous - set(current)):
        summary = previous_records.get(fingerprint)
        if not isinstance(summary, dict):
            continue
        name = str(summary.get("name") or "").strip()
        logical_date = str(summary.get("logical_date") or "").strip()
        if not name or not logical_date:
            continue
        for checkin_type in summary.get("types") or []:
            key = announcement_key(site, name, checkin_type, logical_date, "cancel")
            announced_at = float(state["recent_announcements"].get(key, 0) or 0)
            if time.time() - announced_at < 180:
                continue
            message = build_group_update(
                site,
                name,
                checkin_type,
                logical_date,
                cancelled=True,
                retro=bool(summary.get("retro")),
                operation_time=operation_time,
            )
            broadcast_group_update(bot, accid, site, message)
            delivered_events += 1

    fallback_time = datetime.min.replace(tzinfo=ZoneInfo(site.timezone))
    new_records = [current[key] for key in current.keys() - previous]
    new_records.sort(key=lambda record: (record_datetime(record, site) or fallback_time, record_fingerprint(record)))
    for record in new_records:
        name = record_name(record)
        logical_date = record_logical_date(record, site)
        if not name or not logical_date:
            continue
        for checkin_type in record_completed_types(record):
            key = announcement_key(site, name, checkin_type, logical_date)
            announced_at = float(state["recent_announcements"].get(key, 0) or 0)
            if time.time() - announced_at < 180:
                continue
            message = build_group_update(
                site,
                name,
                checkin_type,
                logical_date,
                retro=record_is_retro(record),
                event_time=(record_datetime(record, site) or now(site)).strftime("%m-%d %H:%M"),
            )
            broadcast_group_update(bot, accid, site, message)
            delivered_events += 1

    with state_lock:
        state["website_records"][site.site_id] = current_compact
        cutoff = time.time() - 600
        state["recent_announcements"] = {
            key: value for key, value in state["recent_announcements"].items()
            if float(value or 0) >= cutoff
        }
        save_state()
    return delivered_events


def broadcast_group_update(bot, accid: int, site: SiteConfig, message: str) -> int:
    delivered = 0
    for group_chat_id in sorted(site.chat_ids):
        try:
            send(bot, accid, group_chat_id, message)
            delivered += 1
        except Exception as error:
            bot.logger.exception("发送群通知失败：site=%s chat_id=%s error=%s", site.site_id, group_chat_id, error)
    return delivered


def announce_change(
    bot,
    accid: int,
    origin_chat_id: int,
    is_group: bool,
    site: SiteConfig,
    message: str,
    private_result: str,
) -> None:
    delivered = broadcast_group_update(bot, accid, site, message)
    if not is_group:
        suffix = f"已通知 {delivered} 个群。" if delivered else "该网站还没有配置通知群。"
        send(bot, accid, origin_chat_id, f"{private_result}\n{suffix}")
    elif origin_chat_id not in site.chat_ids:
        send(bot, accid, origin_chat_id, message)


def admin_help_text() -> str:
    return "\n".join([
        f"{BOT_NAME} · 管理员指令",
        "",
        "管理员验证 密钥 — 首次绑定（仅私聊）",
        "管理员状态 — 查看管理员身份",
        "管理员解除 — 解除自己的管理员身份",
        "",
        "管理员 网站状态 — 检查所有网站",
        "管理员 成员列表 — 查看当前网站成员",
        "管理员 群列表 — 查看网站与群 ID",
        "管理员 广播 内容 — 广播到当前网站群聊",
        "管理员 立即提醒 早间 — 立即发送早间提醒",
        "管理员 立即提醒 晚间 — 立即发送晚间提醒",
    ])


def admin_group_list_text() -> str:
    lines = ["🛡️ 网站与通知群"]
    for configured_site in SITES:
        groups = "、".join(str(chat_id) for chat_id in sorted(configured_site.chat_ids)) or "未配置"
        lines.append(f"{configured_site.site_id} · {configured_site.name}：{groups}")
    return "\n".join(lines)


def admin_site_status_text() -> str:
    lines = ["🛡️ 网站运行状态"]
    for configured_site in SITES:
        started = time.monotonic()
        try:
            website_state, config = website_snapshot(configured_site)
            elapsed_ms = round((time.monotonic() - started) * 1000)
            members = len(website_state.get("members") or [])
            records = len(website_state.get("records") or [])
            schedule = website_state.get("weeklySchedule") or config.get("weekly_schedule") or []
            lines.append(
                f"✅ {configured_site.site_id} · {configured_site.name}：{members} 名成员，"
                f"{records} 条记录，{len(schedule)} 周配置，{elapsed_ms} ms"
            )
        except Exception as error:
            lines.append(f"❌ {configured_site.site_id} · {configured_site.name}：{error}")
    return "\n".join(lines)


def admin_member_list_text(site: SiteConfig) -> str:
    website_state, _ = website_snapshot(site)
    members = [str(item).strip() for item in website_state.get("members") or [] if str(item).strip()]
    lines = [f"🛡️ {site.name} · 成员（{len(members)} 人）"]
    lines.extend(f"{index}. {name}" for index, name in enumerate(members, start=1))
    return "\n".join(lines) if members else lines[0] + "\n暂无成员"


def build_reminder_message(bot, site: SiteConfig, reminder_kind: str) -> str:
    try:
        _, config = website_snapshot(site)
        if reminder_kind == "morning":
            return (
                f"☀️ {site.name} · 早安提醒\n{task_summary(config, site=site)}\n\n"
                "请完成灵修和读经后，在私聊中绑定姓名并发送“打卡 灵修”。"
            )
        return (
            f"🌙 {site.name} · 晚间门训提醒\n{task_summary(config, site=site)}\n\n"
            "完成后发送“打卡 周读物”“打卡 视频”或“打卡 背经”。"
        )
    except Exception as error:
        bot.logger.warning("读取提醒内容失败：site=%s error=%s", site.site_id, error)
        return f"⏰ {site.name} 门训提醒\n请完成今天的门训任务，发送“任务”可查看安排。"


def handle_admin_command(
    bot,
    accid: int,
    chat_id: int,
    member_id: int,
    is_group: bool,
    site: SiteConfig | None,
    command_text: str,
) -> bool:
    """处理中文管理员指令；返回是否已识别为管理员命令。"""
    if not re.match(r"^管理员", command_text.strip(), flags=re.IGNORECASE):
        return False
    remainder = re.sub(r"^管理员\s*", "", command_text.strip(), count=1, flags=re.IGNORECASE)
    normalized = remainder.rstrip("！!。.").strip().lower()

    if normalized in {"", "帮助", "菜单"}:
        send(bot, accid, chat_id, admin_help_text())
        return True
    if normalized == "状态":
        key_status = "已设置" if load_key_record(ADMIN_KEY_FILE) else "未设置"
        identity = "已绑定管理员" if is_admin(member_id) else "普通用户"
        send(bot, accid, chat_id, f"🛡️ 管理员状态\n身份：{identity}\n验证密钥：{key_status}")
        return True
    if normalized.startswith("验证"):
        if is_group:
            send(bot, accid, chat_id, "管理员验证只能私聊机器人进行，避免密钥泄露。")
            return True
        if is_admin(member_id):
            send(bot, accid, chat_id, "你已经绑定为管理员，无需再次验证。")
            return True
        secret = re.sub(r"^验证\s*[:：]?\s*", "", remainder, count=1, flags=re.IGNORECASE)
        if not secret:
            send(bot, accid, chat_id, "请输入：管理员验证 你的密钥")
            return True
        verified, result = verify_admin_attempt(member_id, secret)
        if verified:
            bind_admin(member_id)
            send(bot, accid, chat_id, "✅ 管理员验证成功，当前用户已永久绑定。以后无需再次输入密钥。\n发送“管理员帮助”查看指令。")
        else:
            send(bot, accid, chat_id, f"❌ {result}")
        return True
    if normalized == "解除":
        if not is_admin(member_id):
            send(bot, accid, chat_id, "当前用户不是管理员。")
        elif is_group:
            send(bot, accid, chat_id, "为避免误操作，请私聊发送“管理员解除”。")
        else:
            unbind_admin(member_id)
            send(bot, accid, chat_id, "已解除当前用户的管理员身份。如需恢复，必须重新验证密钥。")
        return True

    if not is_admin(member_id):
        send(bot, accid, chat_id, "此指令仅限管理员。请私聊发送“管理员验证 你的密钥”。")
        return True
    if normalized == "网站状态":
        send(bot, accid, chat_id, admin_site_status_text())
        return True
    if normalized == "群列表":
        send(bot, accid, chat_id, admin_group_list_text())
        return True
    if normalized == "成员列表":
        if not site:
            send(bot, accid, chat_id, "请先发送“网站”，再切换到需要查看的网站。")
        else:
            send(bot, accid, chat_id, admin_member_list_text(site))
        return True
    if normalized.startswith("广播"):
        if not site:
            send(bot, accid, chat_id, "请先选择要广播的网站。")
            return True
        content = re.sub(r"^广播\s*[:：]?\s*", "", remainder, count=1, flags=re.IGNORECASE)
        if not content:
            send(bot, accid, chat_id, "请输入：管理员 广播 公告内容")
            return True
        message = f"📢 {site.name} · 管理员公告\n\n{content}"
        delivered = broadcast_group_update(bot, accid, site, message)
        send(bot, accid, chat_id, f"✅ 广播完成：已发送到 {delivered} 个群。")
        return True
    if normalized.startswith("立即提醒"):
        if not site:
            send(bot, accid, chat_id, "请先选择要提醒的网站。")
            return True
        value = re.sub(r"^立即提醒\s*", "", normalized)
        reminder_kind = "morning" if value in {"早间", "早上", "早晨"} else "evening" if value in {"晚间", "晚上"} else ""
        if not reminder_kind:
            send(bot, accid, chat_id, "请输入“管理员 立即提醒 早间”或“管理员 立即提醒 晚间”。")
            return True
        delivered = broadcast_group_update(bot, accid, site, build_reminder_message(bot, site, reminder_kind))
        send(bot, accid, chat_id, f"✅ {('早间' if reminder_kind == 'morning' else '晚间')}提醒已发送到 {delivered} 个群。")
        return True

    send(bot, accid, chat_id, "没有识别该管理员指令。\n\n" + admin_help_text())
    return True


@cli.on(events.NewMessage)
def on_new_message(bot, accid: int, event) -> None:
    msg = event.msg
    chat_id = int(msg.chat_id)
    member_id = int(getattr(msg, "from_id", 0))
    raw_text = (msg.text or "").strip()
    command_text = raw_text
    text = command_text.rstrip("！!。.").lower()
    is_group, site = resolve_message_site(bot, accid, chat_id, member_id)

    try:
        send_private_welcome_once(bot, accid, chat_id, member_id, is_group)
        if not text:
            return
        bot.logger.info("收到消息：chat_id=%s text=%r", chat_id, safe_log_text(raw_text))
        if handle_admin_command(bot, accid, chat_id, member_id, is_group, site, command_text):
            return
        if text in {"帮助", "菜单"}:
            send(bot, accid, chat_id, help_text(site))
            return
        if text in {"网站", "网站列表", "站点"}:
            send(bot, accid, chat_id, site_list_text(member_id))
            return
        directly_selected = find_site(command_text) if not is_group else None
        if directly_selected:
            with state_lock:
                state["active_sites"][str(member_id)] = directly_selected.site_id
                save_state()
            send(
                bot,
                accid,
                chat_id,
                f"✅ 已选择：{directly_selected.name}\n\n下一步请发送：绑定 你的姓名\n例如：绑定 张三",
            )
            return
        if text.startswith("切换 ") or text.startswith("切换:") or text.startswith("切换："):
            if is_group:
                send(bot, accid, chat_id, "群聊已由群 ID 自动关联网站，不需要切换。")
                return
            requested = re.sub(r"^切换\s*[:：]?\s*", "", command_text, flags=re.IGNORECASE)
            selected = find_site(requested)
            if not selected:
                send(bot, accid, chat_id, "没有找到该网站。\n" + site_list_text(member_id))
                return
            with state_lock:
                state["active_sites"][str(member_id)] = selected.site_id
                save_state()
            send(bot, accid, chat_id, f"✅ 已选择：{selected.name}\n\n下一步请发送：绑定 你的姓名\n例如：绑定 张三")
            return
        if text.startswith("绑定 ") or text.startswith("绑定:") or text.startswith("绑定："):
            if is_group:
                send(bot, accid, chat_id, "为避免冒用姓名，请私聊机器人完成绑定。")
                return
            binding_site, name = parse_binding(command_text, site)
            if not binding_site:
                send(bot, accid, chat_id, "请先发送“网站”，再直接回复你所在的网站名称。")
                return
            website_state, _ = website_snapshot(binding_site)
            members = [str(item).strip() for item in website_state.get("members") or []]
            if bind_member(member_id, binding_site, name, members):
                send(bot, accid, chat_id, f"已绑定：{binding_site.name} / {name}\n以后私聊发送“打卡”即可同步到该网站。")
            else:
                send(bot, accid, chat_id, f"{binding_site.name} 没有找到成员“{name}”。请使用网站中的准确姓名。")
            return
        if not site:
            if is_group:
                send(bot, accid, chat_id, f"这个群尚未关联门训网站，日志中的 chat_id={chat_id}。请把它加入 sites.json。")
            else:
                send(bot, accid, chat_id, "你还没有选择门训网站。\n请先发送“网站”，再按提示完成切换和绑定。")
            return
        if text in {"任务", "今日", "今天", "本周"}:
            _, config = website_snapshot(site)
            send(bot, accid, chat_id, f"🌐 {site.name}\n" + task_summary(config, site=site))
            return
        if text in {"灵修", "今日灵修", "灵修内容"}:
            send(bot, accid, chat_id, daily_devotion_text(site))
            return
        if text in {"状态", "进度"}:
            send(bot, accid, chat_id, "请选择：\n· 我的状态\n· 我的月状态\n· 群状态")
            return
        if text in {"群状态", "小组状态", "群进度"}:
            send(bot, accid, chat_id, website_status(site))
            return
        if text in {"我的状态", "个人状态"}:
            name = bound_name(member_id, site)
            send(bot, accid, chat_id, website_status(site, name) if name else f"请先绑定 {site.name} 的身份，例如：绑定 {site.site_id} 你的姓名")
            return
        if text.startswith("我的月状态") or text.startswith("个人月状态") or text.startswith("我的月历"):
            name = bound_name(member_id, site)
            if not name:
                send(bot, accid, chat_id, f"请先绑定 {site.name} 的身份，例如：绑定 你的姓名")
                return
            month_value = re.sub(r"^(?:我的月状态|个人月状态|我的月历)\s*", "", command_text).strip()
            try:
                send(bot, accid, chat_id, member_month_status(site, name, month_value))
            except ValueError:
                send(bot, accid, chat_id, "月份格式不正确。\n请发送“我的月状态”，或例如“我的月状态 2026-07”。")
            return
        if text.startswith("补签") or text.startswith("补打卡"):
            name = bound_name(member_id, site)
            if not name:
                send(bot, accid, chat_id, f"请先绑定 {site.name} 的身份，例如：绑定 {site.site_id} 张迦勒")
                return
            task_text, logical_date = parse_retro_command(command_text, site)
            if not logical_date:
                send(bot, accid, chat_id, "请提供补签日期，例如：补签 2026-08-20，或使用“补签 昨天”。")
                return
            if date.fromisoformat(logical_date) > now(site).date():
                send(bot, accid, chat_id, "补签日期不能晚于今天。")
                return
            checkin_type = parse_task_kind(task_text)
            if not checkin_type:
                send(bot, accid, chat_id, "项目无效。可用项目：灵修、周读物、视频、背经。\n例如：补签 视频 2026-08-20")
                return
            try:
                ok, result = website_checkin(site, name, checkin_type, logical_date=logical_date, is_retro=True)
            except Exception as error:
                bot.logger.exception("网站 %s 补签同步失败：%s", site.site_id, error)
                ok, result = False, "网站暂时不可用，本次补签没有写入，请稍后重试。"
            if ok:
                remember_announced_change(site, name, checkin_type, logical_date)
                message = build_group_update(
                    site,
                    name,
                    checkin_type,
                    logical_date,
                    retro=True,
                    event_time=now(site).strftime("%m-%d %H:%M"),
                )
                announce_change(
                    bot,
                    accid,
                    chat_id,
                    is_group,
                    site,
                    message,
                    f"✅ 补签成功：{site.name} / {name} / {logical_date} / {checkin_type}",
                )
            else:
                send(bot, accid, chat_id, f"ℹ️ {result}")
            return
        if text.startswith("取消打卡") or text.startswith("取消补签") or text.startswith("取消签到"):
            name = bound_name(member_id, site)
            if not name:
                send(bot, accid, chat_id, f"请先绑定 {site.name} 的身份，例如：绑定 {site.site_id} 张迦勒")
                return
            task_text, logical_date = parse_cancel_command(command_text, site)
            if not logical_date:
                send(bot, accid, chat_id, "日期无效。请使用 2026-08-20、昨天或前天。")
                return
            if date.fromisoformat(logical_date) > now(site).date():
                send(bot, accid, chat_id, "取消日期不能晚于今天。")
                return
            checkin_type = parse_task_kind(task_text)
            if not checkin_type:
                send(bot, accid, chat_id, "项目无效。可用项目：灵修、周读物、视频、背经。\n例如：取消打卡 视频 2026-08-20")
                return
            retro_only = text.startswith("取消补签")
            try:
                ok, result = website_cancel_checkin(site, name, checkin_type, logical_date, retro_only=retro_only)
            except Exception as error:
                bot.logger.exception("网站 %s 取消打卡失败：%s", site.site_id, error)
                ok, result = False, "网站暂时无法连接，取消失败，请稍后再试。"
            if ok:
                operation_time = now(site).strftime("%Y-%m-%d %H:%M:%S")
                remember_announced_change(site, name, checkin_type, logical_date, action="cancel")
                message = build_group_update(
                    site,
                    name,
                    checkin_type,
                    logical_date,
                    cancelled=True,
                    retro=retro_only,
                    operation_time=operation_time,
                )
                announce_change(
                    bot,
                    accid,
                    chat_id,
                    is_group,
                    site,
                    message,
                    f"↩️ 取消打卡成功：{site.name} / {name} / {logical_date} / {checkin_type}",
                )
            else:
                send(bot, accid, chat_id, f"ℹ️ {result}")
            return
        if text.startswith("打卡") or text.startswith("签到"):
            name = bound_name(member_id, site)
            if not name:
                send(bot, accid, chat_id, f"请先绑定 {site.name} 的身份，例如：绑定 {site.site_id} 张迦勒")
                return
            task_text = re.sub(r"^(打卡|签到)\s*", "", command_text, count=1, flags=re.IGNORECASE)
            checkin_type = parse_task_kind(task_text)
            if not checkin_type:
                send(bot, accid, chat_id, "项目无效。可用项目：灵修、周读物、视频、背经。\n例如：打卡 视频")
                return
            try:
                ok, result = website_checkin(site, name, checkin_type)
            except Exception as error:
                bot.logger.exception("网站 %s 打卡同步失败：%s", site.site_id, error)
                ok, result = False, "网站暂时不可用，本次打卡没有写入，请稍后重试。"
            if ok:
                logical_date = now(site).date().isoformat()
                remember_announced_change(site, name, checkin_type, logical_date)
                message = build_group_update(
                    site,
                    name,
                    checkin_type,
                    logical_date,
                    event_time=now(site).strftime("%m-%d %H:%M"),
                )
                announce_change(
                    bot,
                    accid,
                    chat_id,
                    is_group,
                    site,
                    message,
                    f"✅ 打卡成功：{site.name} / {name} / {checkin_type}",
                )
            else:
                send(bot, accid, chat_id, f"ℹ️ {result}")
            return
        if text in {"提醒", "时间"}:
            send(bot, accid, chat_id, f"⏰ {site.name}：{site.morning_time} 灵修和读经；{site.evening_time} 门训学习（{site.timezone}）。")
            return
    except Exception as error:
        site_label = site.site_id if site else "unresolved"
        bot.logger.exception("处理门训命令失败：site=%s error=%s", site_label, error)
        send(bot, accid, chat_id, "门训网站暂时无法连接或配置有误，请稍后再试。")


def reminder_loop(bot, accid: int) -> None:
    if not any(site.chat_ids for site in SITES):
        bot.logger.warning("所有网站都未设置 chat_ids，定时提醒不会广播；收到私聊消息仍会响应。")
    while True:
        write_health()
        for site in SITES:
            if not site.chat_ids:
                continue
            try:
                poll_website_notifications(bot, accid, site)
            except Exception as error:
                bot.logger.exception("监听网站打卡失败：site=%s error=%s", site.site_id, error)
            current = now(site)
            if reminder_due(current, site.morning_time):
                reminder_kind = "morning"
            elif reminder_due(current, site.evening_time):
                reminder_kind = "evening"
            else:
                continue
            reminder_prefix = f"{site.site_id}:{current.date().isoformat()}:{reminder_kind}"
            pending_chat_ids = [
                chat_id for chat_id in site.chat_ids
                if not state["reminded"].get(f"{reminder_prefix}:{chat_id}")
            ]
            if not pending_chat_ids:
                continue
            message = build_reminder_message(bot, site, reminder_kind)
            for chat_id in pending_chat_ids:
                try:
                    send(bot, accid, chat_id, message)
                    with state_lock:
                        state["reminded"][f"{reminder_prefix}:{chat_id}"] = now(site).isoformat()
                        save_state()
                except Exception as error:
                    bot.logger.exception("发送提醒失败：site=%s chat_id=%s error=%s", site.site_id, chat_id, error)
        time.sleep(20)


@cli.on_start
def on_start(bot, args) -> None:
    global reminder_started
    write_health("starting")
    if not reminder_started:
        reminder_started = True
        accounts = bot.rpc.get_all_account_ids()
        if accounts:
            threading.Thread(target=reminder_loop, args=(bot, accounts[0]), daemon=True).start()
    for site in SITES:
        bot.logger.info(
            "网站已加载：id=%s name=%s url=%s chat_ids=%s reminders=%s/%s timezone=%s",
            site.site_id,
            site.name,
            site.url,
            sorted(site.chat_ids),
            site.morning_time,
            site.evening_time,
            site.timezone,
        )
    bot.logger.info("门训同行多网站服务已启动，共 %s 个网站", len(SITES))


if __name__ == "__main__":
    cli.start()
