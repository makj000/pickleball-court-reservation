#!/usr/bin/env python3
"""
rec.us Foster City reservation availability scanner.

1. Logs into rec.us with your credentials
2. Navigates to the requested dates
3. Reads visible slot times and available courts from the modal
4. Returns structured availability data
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

APP_DIR = Path(__file__).parent
CONFIG_FILE = APP_DIR / "config.json"
load_dotenv(APP_DIR / ".env")

PT = ZoneInfo("America/Los_Angeles")
APP_VERSION = "2.4.0"
SCAN_HISTORY_MAX = 300  # safety cap; UI filters to past 24h, normalize trims older.

# ── Credentials ───────────────────────────────────────────────────────────────
EMAIL    = os.environ.get("REC_US_LOGIN") or os.environ["EMAIL"]
PASSWORD = os.environ.get("REC_US_PASSWORD") or os.environ["PASSWORD"]
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


BASE_URL      = load_config()["base_url"]
SYNC_SCAN_URL = load_config().get("sync_scan_url", "")
HEADLESS      = True
DEFAULT_TIME_FILTER = "8:00 AM"
NOTIFY_NUMBER = "+14154380400"
STATE_BUCKET  = os.environ.get("STATE_BUCKET", "")
WORK_QUEUE_URL = os.environ.get("PICKLEBALL_QUEUE_URL") or os.environ.get("WORK_QUEUE_URL", "")
STATE_KEY           = "state.json"
TELEGRAM_USAGE_KEY  = "telegram_usage.json"
TELEGRAM_USAGE_MAX  = 100
SCAN_LOCK_TTL_SECONDS = 20 * 60
SYNC_TOKEN_TTL_SECONDS = 300
SQS_DELAY_MAX_SECONDS = 15 * 60
SQS_STALE_GRACE_SECONDS = 5 * 60
_RELEASE_PRE_INTERVAL_S  = 15   # seconds between probes before 8:00 AM
_RELEASE_BURST_UNTIL_S   = 30   # back-to-back until 8:00:30
_RELEASE_POST_INTERVAL_S = 15   # seconds between probes after burst
_RELEASE_END_S           = 120  # stop at 8:02:00
SYNC_SIGNING_SECRET = os.environ.get("SYNC_SIGNING_SECRET") or os.environ.get("API_PASSWORD", "")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, PUT, POST, OPTIONS",
    "Content-Type": "application/json",
}

SLOT_TIMES = [
    "8:00 AM", "9:00 AM", "10:00 AM", "11:00 AM",
    "4:00 PM", "5:00 PM", "6:00 PM",
]

COURT_PREFERENCE = ["6", "4", "5"]
TARGET_COURTS = COURT_PREFERENCE[:]

# rec.us API site UUIDs for each court number (no auth required)
COURT_SITE_IDS: dict[str, str] = {
    "6": "a474166c-53fb-4444-9f49-e5da379deab0",
    "4": "ce22b935-aeb9-44ae-852d-bf2e7c91617c",
    "5": "445abe2b-cb2f-450d-a376-0f643890731c",
}

# rec.us API courtSportIds (used in booking POST body) — from v1/locations/availability
COURT_SPORT_IDS: dict[str, str] = {
    "6": "e4def6e2-b46d-4d1f-a44f-6bb65f603198",
    "4": "d3bfa8f9-03f4-4c80-ac27-fbb4dbfb9a15",
    "5": "671d9687-dfa5-4f1c-8d29-de68baf12137",
}
PARTICIPANT_USER_ID  = "06ba5791-1e5b-45f3-8910-1b9c75d020cc"
FIREBASE_API_KEY     = "YOUR_FIREBASE_API_KEY"
FIREBASE_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
    f"?key={FIREBASE_API_KEY}"
)

_TIME_TEXT_TO_HHMMSS: dict[str, str] = {
    "8:00 AM": "08:00:00", "9:00 AM": "09:00:00", "10:00 AM": "10:00:00",
    "11:00 AM": "11:00:00", "4:00 PM": "16:00:00", "5:00 PM": "17:00:00",
    "6:00 PM": "18:00:00",
}

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*(AM|PM)", re.IGNORECASE)
COURT_RE = re.compile(r"^court\s+(\d+)", re.IGNORECASE)

# ── Config helpers ────────────────────────────────────────────────────────────

def load_check_dates() -> list[date]:
    cfg = load_config()
    return sorted(date.fromisoformat(d) for d in cfg.get("check_dates", []))


def build_next_dates(days: int, start: date | None = None) -> list[date]:
    start = start or date.today()
    return [start + timedelta(days=offset) for offset in range(days)]


def parse_query_params(event) -> dict[str, str]:
    return (event or {}).get("queryStringParameters") or {}


def get_header(event, name: str) -> str:
    headers = (event or {}).get("headers") or {}
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return ""


def get_path(event) -> str:
    rc = (event or {}).get("requestContext") or {}
    path = rc.get("http", {}).get("path") or event.get("path") or "/"
    # HTTP API v2 includes the stage name as the first path segment — strip it
    stage = rc.get("stage", "")
    if stage and stage != "$default":
        prefix = f"/{stage}"
        if path.startswith(prefix + "/"):
            path = path[len(prefix):]
        elif path == prefix:
            path = "/"
    return path.rstrip("/") or "/"


def get_method(event) -> str:
    rc = (event or {}).get("requestContext") or {}
    return (
        rc.get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()


def get_body(event) -> dict:
    raw = (event or {}).get("body") or ""
    if event.get("isBase64Encoded") and raw:
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def parse_requested_dates(event) -> list[date]:
    params = parse_query_params(event)
    dates_param = (params.get("dates") or "").strip()
    if dates_param:
        return sorted(date.fromisoformat(r.strip()) for r in dates_param.split(",") if r.strip())
    start_raw = (params.get("start_date") or "").strip()
    start_date = date.fromisoformat(start_raw) if start_raw else date.today()
    days_raw = (params.get("days") or "").strip()
    if days_raw:
        days = max(1, min(int(days_raw), 7))
        return build_next_dates(days=days, start=start_date)
    configured = load_check_dates()
    if configured:
        return configured
    return build_next_dates(days=3, start=start_date)


def parse_time_filter(event, *, default: str | None = None) -> str | None:
    params = parse_query_params(event)
    raw = (params.get("time") or "").strip()
    return raw if raw else default


def summarize_results(results: dict[str, list[dict]]) -> dict[str, int]:
    return {
        "days_with_availability": len(results),
        "slots_found": sum(len(slots) for slots in results.values()),
    }


def build_scan_payload(*, targets, target_time, results):
    days = []
    for target in targets:
        key = target.strftime("%A, %B %-d")
        days.append({"date": target.isoformat(), "label": key, "slots": results.get(key, [])})
    summary = summarize_results(results)
    summary["days_scanned"] = len(targets)
    return {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "filters": {"dates": [t.isoformat() for t in targets], "time": target_time},
        "court_preference": COURT_PREFERENCE,
        "summary": summary,
        "days": days,
    }


def _mark_booked_slots_in_scan_results(
    *,
    targets: list[date],
    results: dict[str, list[dict]],
    booked_slots: list[dict],
) -> None:
    booked_keys = {(b["date"], b["time"]) for b in booked_slots}
    if not booked_keys:
        return
    for target in targets:
        date_str = target.isoformat()
        label = target.strftime("%A, %B %-d")
        for slot in results.get(label, []):
            if (date_str, slot.get("time")) not in booked_keys:
                continue
            slot["court_avail"] = _empty_court_availability(False)
            slot["courts"] = []
            slot["preferred_court"] = None


def _is_lambda_url_request(event) -> bool:
    domain = ((event or {}).get("requestContext") or {}).get("domainName", "")
    return ".lambda-url." in domain


def _sync_token_payload(path: str, params: dict[str, str], expires: str) -> str:
    filtered = {
        key: value
        for key, value in params.items()
        if key not in {"sync_token", "sync_expires"}
    }
    query = urlencode(sorted(filtered.items()))
    return f"{path}\n{expires}\n{query}"


def _sign_sync_token(path: str, params: dict[str, str], expires: str) -> str:
    if not SYNC_SIGNING_SECRET:
        raise RuntimeError("SYNC_SIGNING_SECRET or API_PASSWORD must be configured")
    payload = _sync_token_payload(path, params, expires)
    return hmac.new(
        SYNC_SIGNING_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _has_valid_sync_token(event) -> bool:
    params = parse_query_params(event)
    token = (params.get("sync_token") or "").strip()
    expires = (params.get("sync_expires") or "").strip()
    if not token or not expires:
        return False
    try:
        expires_at = int(expires)
    except ValueError:
        return False
    now = int(datetime.now(tz=timezone.utc).timestamp())
    if expires_at < now:
        return False
    expected = _sign_sync_token(get_path(event), params, expires)
    return hmac.compare_digest(token, expected)


def _build_sync_redirect(event) -> dict:
    if not SYNC_SCAN_URL:
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "sync_scan_url is not configured"}),
        }
    if not SYNC_SIGNING_SECRET:
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "sync signing secret is not configured"}),
        }

    params = parse_query_params(event).copy()
    target_path = "/scan"
    expires = str(int(datetime.now(tz=timezone.utc).timestamp()) + SYNC_TOKEN_TTL_SECONDS)
    params["sync_expires"] = expires
    params["sync_token"] = _sign_sync_token(target_path, params, expires)
    target_url = urljoin(SYNC_SCAN_URL, target_path.lstrip("/"))
    location = f"{target_url}?{urlencode(sorted(params.items()))}"
    return {
        "statusCode": 307,
        "headers": {**CORS_HEADERS, "Location": location, "Cache-Control": "no-store"},
        "body": "",
    }


# ── Notifications ─────────────────────────────────────────────────────────────

NOTIFY_EMAIL = "kejia.ma@gmail.com"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

def send_sms(to: str, message: str) -> None:
    sns = boto3.client("sns", region_name="us-west-2")
    sns.publish(PhoneNumber=to, Message=message)


def send_email(to: str, subject: str, body: str) -> None:
    ses = boto3.client("sesv2", region_name="us-west-2")
    ses.send_email(
        FromEmailAddress=to,
        Destination={"ToAddresses": [to]},
        Content={"Simple": {
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        }},
    )


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured")

    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        body = resp.read()
    data = json.loads(body.decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")


def notify(message: str, subject: str = "Pickleball alert") -> None:
    try:
        send_sms(NOTIFY_NUMBER, message)
        print(f"SMS sent: {message}")
    except Exception as exc:
        print(f"SMS failed: {exc}")
    try:
        send_telegram(message)
        print("Telegram sent.")
    except Exception as exc:
        print(f"Telegram failed: {exc}")
    try:
        send_email(NOTIFY_EMAIL, subject, message)
        print(f"Email sent to {NOTIFY_EMAIL}.")
    except Exception as exc:
        print(f"Email failed: {exc}")


def _ordered_open_courts(court_availability: dict[str, bool | None]) -> list[str]:
    ordered = [court for court in COURT_PREFERENCE if court_availability.get(court) is True]
    extras = sorted(
        court
        for court, is_open in court_availability.items()
        if is_open is True and court not in COURT_PREFERENCE
    )
    return ordered + extras


def _alert_lines_for_open_targets(
    state: dict,
    scanned_availability: dict[str, dict[str, dict[str, bool | None]]],
) -> list[str]:
    watched_set = {
        (slot["date"], slot["time"], slot["court"])
        for slot in state.get("watched_slots", [])
    }
    auto_book_set = {
        (slot.get("date"), slot.get("time"))
        for slot in (state.get("auto_book_slots") or [])
        if slot.get("date") and slot.get("time")
    }

    lines: list[str] = []
    for date_str in sorted(scanned_availability):
        time_map = scanned_availability[date_str]
        ordered_times = sorted(
            time_map,
            key=lambda t: SLOT_TIMES.index(t) if t in SLOT_TIMES else len(SLOT_TIMES),
        )
        for time_text in ordered_times:
            court_availability = time_map[time_text]
            open_courts = _ordered_open_courts(court_availability)
            if not open_courts:
                continue

            watched_open = [
                court for court in open_courts
                if (date_str, time_text, court) in watched_set
            ]
            auto_book_open = (date_str, time_text) in auto_book_set
            if not watched_open and not auto_book_open:
                continue

            tags = []
            if watched_open:
                tags.append("watched " + ", ".join(f"Court {court}" for court in watched_open))
            if auto_book_open:
                tags.append("auto-book")
            lines.append(
                f"{date_str} {time_text}: open {', '.join(f'Court {court}' for court in open_courts)}"
                + (f" ({'; '.join(tags)})" if tags else "")
            )
    return lines


def ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


# ── S3 State ──────────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "watched_slots":       [],   # [{"date": "YYYY-MM-DD", "time": "H:MM AM", "court": "6"}]
        "watched_slots_updated_at": None,
        "my_reservations":     [],   # [{"date": "YYYY-MM-DD", "time": "H:MM AM", "court": "6"}]
        "my_reservations_synced_at": None,
        "my_reservations_source": None,
        "availability":        {},   # {"YYYY-MM-DD": {"H:MM AM": {"6": true, "4": false, "5": true}}}
        "notified_slots":      [],   # ["YYYY-MM-DD|H:MM AM|6"] — already SMS'd, avoids repeat texts
        "last_scanned":        None,
        "last_scan_started_at": None,
        "last_scan_kind":      None,
        "recent_scan_history": [],
        "scan_started_kind":   None,
        "scan_interval_hours": 1.0,
        "queued_scheduled_probe_at": None,
        "queued_scheduled_probe_token": None,
        "queued_publish_probe_date": None,
        "auto_watched_weekends": [],  # ISO date strings already auto-watched; user removals are respected
        "auto_watch_weekends_enabled": True,
        "focus_newest_weekend": False, # when True, only scan the latest weekend day (skip older ones)
        "auto_book_slots":     [],   # [{"date": "YYYY-MM-DD", "time": "H:MM AM"}] — slots to auto-book
        "seen_open_days":      [],   # ISO dates where we first observed ≥1 open slot
        "cached_jwt":              None, # Firebase JWT pre-fetched before 8am release
        "cached_jwt_expires_at":   None,
        "release_probe_session_date": None, # ISO date of queued session (dedup)
        "last_release_probe_session": None, # ISO timestamp of last session start
        "release_probe_log":       [],  # [{ts, phase, result, booked?, open?, error?}]
    }


def _empty_court_availability(default=None) -> dict[str, bool | None]:
    return {court: default for court in TARGET_COURTS}


def _normalize_court_number(value) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw in TARGET_COURTS:
        return raw
    match = re.search(r"(\d+)", raw)
    if match and match.group(1) in TARGET_COURTS:
        return match.group(1)
    return None


def _preferred_open_court(court_availability: dict[str, bool | None]) -> str | None:
    for court in COURT_PREFERENCE:
        if court_availability.get(court) is True:
            return court
    return None


def _normalize_time_availability(value) -> dict[str, bool | None]:
    if isinstance(value, dict):
        normalized = _empty_court_availability(None)
        matched = False
        for key, raw_avail in value.items():
            court = _normalize_court_number(key)
            if court is None:
                continue
            matched = True
            normalized[court] = None if raw_avail is None else bool(raw_avail)
        if matched:
            return normalized
    if value is False:
        return _empty_court_availability(False)
    if value is True:
        return _empty_court_availability(None)
    return _empty_court_availability(None)


def _normalize_slot_records(slots, *, expand_legacy: bool, default_court: str | None = None) -> list[dict]:
    normalized: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        slot_date = slot.get("date")
        slot_time = slot.get("time")
        if not slot_date or not slot_time:
            continue
        court = _normalize_court_number(slot.get("court"))
        if court:
            courts = [court]
        elif expand_legacy:
            courts = TARGET_COURTS
        elif default_court:
            courts = [default_court]
        else:
            courts = []
        for court_num in courts:
            key = (slot_date, slot_time, court_num)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"date": slot_date, "time": slot_time, "court": court_num})
    return normalized


def _normalize_notified_slots(notified_slots) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in notified_slots or []:
        parts = str(entry).split("|")
        if len(parts) == 3:
            court = _normalize_court_number(parts[2])
            if court is None:
                continue
            expanded = [f"{parts[0]}|{parts[1]}|{court}"]
        elif len(parts) == 2:
            expanded = [f"{parts[0]}|{parts[1]}|{court}" for court in TARGET_COURTS]
        else:
            continue
        for value in expanded:
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
    return normalized


def _normalize_state(state: dict) -> dict:
    normalized = _empty_state()
    normalized["watched_slots_updated_at"] = state.get("watched_slots_updated_at")
    normalized["last_scanned"] = state.get("last_scanned")
    normalized["last_scan_started_at"] = state.get("last_scan_started_at")
    normalized["last_scan_kind"] = state.get("last_scan_kind") or None
    recent_scan_history = []
    history_cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    for entry in (state.get("recent_scan_history") or [])[:SCAN_HISTORY_MAX]:
        if not isinstance(entry, dict):
            continue
        cmp_ts = entry.get("completed_at") or entry.get("started_at") or ""
        if cmp_ts and cmp_ts < history_cutoff:
            continue
        targets = []
        for target in (entry.get("targets") or []):
            if not isinstance(target, dict):
                continue
            target_date = target.get("date")
            times = [t for t in (target.get("times") or []) if t in SLOT_TIMES]
            if not target_date or not times:
                continue
            raw_courts = target.get("courts") if isinstance(target.get("courts"), dict) else {}
            courts: dict[str, dict[str, bool | None]] = {}
            for time_text in times:
                court_avail = raw_courts.get(time_text)
                if isinstance(court_avail, dict):
                    normalized_courts: dict[str, bool | None] = {}
                    for court in COURT_PREFERENCE:
                        raw = court_avail.get(court)
                        normalized_courts[court] = None if raw is None else bool(raw)
                    courts[time_text] = normalized_courts
            target_entry = {"date": target_date, "times": times}
            if courts:
                target_entry["courts"] = courts
            targets.append(target_entry)
        recent_scan_history.append({
            "kind": entry.get("kind") or None,
            "started_at": entry.get("started_at"),
            "completed_at": entry.get("completed_at"),
            "status": entry.get("status") or "completed",
            "error": entry.get("error"),
            "targets": targets,
        })
    normalized["recent_scan_history"] = recent_scan_history
    normalized["scan_interval_hours"] = state.get("scan_interval_hours", 1.0)
    normalized["queued_scheduled_probe_at"] = state.get("queued_scheduled_probe_at")
    normalized["queued_scheduled_probe_token"] = state.get("queued_scheduled_probe_token")
    normalized["queued_publish_probe_date"] = state.get("queued_publish_probe_date")
    if state.get("scan_started_at"):
        normalized["scan_started_at"] = state["scan_started_at"]
        normalized["scan_started_kind"] = state.get("scan_started_kind") or None

    availability: dict[str, dict[str, dict[str, bool | None]]] = {}
    for date_key, day_map in (state.get("availability") or {}).items():
        if not isinstance(day_map, dict):
            continue
        availability[date_key] = {}
        for time_key in SLOT_TIMES:
            availability[date_key][time_key] = _normalize_time_availability(day_map.get(time_key))
    normalized["availability"] = availability
    normalized["watched_slots"] = _normalize_slot_records(
        state.get("watched_slots"),
        expand_legacy=True,
    )
    normalized["my_reservations"] = _normalize_slot_records(
        state.get("my_reservations"),
        expand_legacy=False,
        default_court=COURT_PREFERENCE[0],
    )
    normalized["my_reservations_synced_at"] = state.get("my_reservations_synced_at")
    normalized["my_reservations_source"] = state.get("my_reservations_source")
    normalized["notified_slots"] = _normalize_notified_slots(state.get("notified_slots"))
    today_str = date.today().isoformat()
    normalized["auto_watched_weekends"] = sorted(
        d for d in (state.get("auto_watched_weekends") or [])
        if isinstance(d, str) and d >= today_str
    )
    normalized["seen_open_days"] = sorted(
        d for d in (state.get("seen_open_days") or [])
        if isinstance(d, str) and d >= today_str
    )
    raw_enabled = state.get("auto_watch_weekends_enabled")
    normalized["auto_watch_weekends_enabled"] = True if raw_enabled is None else bool(raw_enabled)
    normalized["focus_newest_weekend"] = bool(state.get("focus_newest_weekend", False))
    # Normalize auto_book_slots: {date, time} pairs, future only, valid times only
    auto_book_slots = []
    seen_ab: set[tuple[str, str]] = set()
    for slot in (state.get("auto_book_slots") or []):
        if not isinstance(slot, dict):
            continue
        slot_date = slot.get("date")
        slot_time = slot.get("time")
        if not slot_date or not slot_time:
            continue
        if slot_date < today_str or slot_time not in SLOT_TIMES:
            continue
        key = (slot_date, slot_time)
        if key in seen_ab:
            continue
        seen_ab.add(key)
        auto_book_slots.append({"date": slot_date, "time": slot_time})
    normalized["auto_book_slots"] = auto_book_slots
    return normalized


def _history_targets_from_map(targets_by_date: dict[str, list[str]]) -> list[dict]:
    normalized_targets = []
    for date_str, times in sorted(targets_by_date.items()):
        ordered_times = [t for t in SLOT_TIMES if t in set(times)]
        if ordered_times:
            normalized_targets.append({"date": date_str, "times": ordered_times})
    return normalized_targets


def _attach_history_results(targets: list[dict], new_avail: dict) -> list[dict]:
    """Return targets with per-time court results filled from new_avail."""
    enriched = []
    for target in targets:
        date_str = target.get("date")
        times = target.get("times") or []
        new_target = {"date": date_str, "times": list(times)}
        day = (new_avail or {}).get(date_str) or {}
        courts: dict[str, dict[str, bool | None]] = {}
        for time_text in times:
            court_avail = day.get(time_text)
            if not isinstance(court_avail, dict):
                continue
            courts[time_text] = {
                c: (None if court_avail.get(c) is None else bool(court_avail.get(c)))
                for c in COURT_PREFERENCE
            }
        if courts:
            new_target["courts"] = courts
        enriched.append(new_target)
    return enriched


def _record_scan_history(
    state: dict,
    *,
    kind: str,
    started_at: str | None,
    completed_at: str | None,
    status: str,
    targets: list[dict],
    error: str | None = None,
) -> None:
    history = list(state.get("recent_scan_history") or [])
    history.insert(0, {
        "kind": kind,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "error": error,
        "targets": targets,
    })
    state["recent_scan_history"] = history[:SCAN_HISTORY_MAX]


def load_state() -> dict:
    if not STATE_BUCKET:
        return _empty_state()
    s3 = boto3.client("s3", region_name="us-west-2")
    try:
        obj = s3.get_object(Bucket=STATE_BUCKET, Key=STATE_KEY)
        return _normalize_state(json.loads(obj["Body"].read()))
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return _empty_state()
        raise


def save_state(state: dict) -> None:
    if not STATE_BUCKET:
        return
    s3 = boto3.client("s3", region_name="us-west-2")
    state = _normalize_state(state)
    s3.put_object(
        Bucket=STATE_BUCKET,
        Key=STATE_KEY,
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _active_scan_started_at(state: dict) -> datetime | None:
    started = _parse_utc_iso(state.get("scan_started_at"))
    if not started:
        return None
    age = (datetime.now(tz=timezone.utc) - started).total_seconds()
    if age < 0 or age > SCAN_LOCK_TTL_SECONDS:
        return None
    return started


def _has_future_watched_slots(state: dict) -> bool:
    today_str = date.today().isoformat()
    return any(
        slot.get("date", "") >= today_str
        for slot in state.get("watched_slots", [])
    )


def _upcoming_weekends(n: int = 3) -> list[tuple[date, date]]:
    today = date.today()
    days_until_sat = (5 - today.weekday()) % 7  # 0 if today is already Saturday
    first_sat = today + timedelta(days=days_until_sat)
    return [(first_sat + timedelta(weeks=i), first_sat + timedelta(weeks=i, days=1)) for i in range(n)]


def _auto_watch_upcoming_weekends(state: dict) -> bool:
    """Add 9 AM slots for the next 3 weekends (once per weekend). Returns True if state was modified."""
    if not state.get("auto_watch_weekends_enabled", True):
        return False
    weekends = _upcoming_weekends(3)
    auto_watched = set(state.get("auto_watched_weekends") or [])
    new_dates = [
        d.isoformat()
        for sat, sun in weekends
        for d in (sat, sun)
        if d.isoformat() not in auto_watched
    ]
    if not new_dates:
        return False

    existing = {(s["date"], s["time"], s["court"]) for s in state.get("watched_slots", [])}
    new_slots = [
        {"date": d_str, "time": "9:00 AM", "court": court}
        for d_str in new_dates
        for court in COURT_PREFERENCE
        if (d_str, "9:00 AM", court) not in existing
    ]

    today_str = date.today().isoformat()
    state["auto_watched_weekends"] = sorted(
        (auto_watched | set(new_dates)) - {d for d in auto_watched if d < today_str}
    )
    if new_slots:
        state["watched_slots"] = _normalize_slot_records(
            state.get("watched_slots", []) + new_slots,
            expand_legacy=False,
        )
        state["watched_slots_updated_at"] = _utc_now_iso()
        print(f"Auto-watched {len(new_slots)} 9 AM slot(s) for weekends {new_dates}.")
    return True


def _auto_watch_on_new_day_openings(state: dict, new_avail: dict) -> bool:
    """When we first see open slots on the newly published day (14 days out), auto-watch
    9 AM for that day and the next day if they are weekend days. Returns True if modified."""
    new_day_str = _new_day_iso()
    day_avail = new_avail.get(new_day_str, {})
    any_open = any(
        v is True
        for court_map in day_avail.values()
        for v in (court_map.values() if isinstance(court_map, dict) else [])
    )
    if not any_open:
        return False

    seen_open = set(state.get("seen_open_days") or [])
    if new_day_str in seen_open:
        return False  # already triggered for this day

    seen_open.add(new_day_str)
    state["seen_open_days"] = sorted(seen_open)

    existing = {(s["date"], s["time"], s["court"]) for s in state.get("watched_slots", [])}
    auto_watched = set(state.get("auto_watched_weekends") or [])
    new_slots = []
    new_day = date.fromisoformat(new_day_str)
    for d in (new_day, new_day + timedelta(days=1)):
        if d.weekday() < 5:
            continue
        d_str = d.isoformat()
        for court in COURT_PREFERENCE:
            if (d_str, "9:00 AM", court) not in existing:
                new_slots.append({"date": d_str, "time": "9:00 AM", "court": court})
        auto_watched.add(d_str)

    if new_slots:
        state["watched_slots"] = _normalize_slot_records(
            state.get("watched_slots", []) + new_slots,
            expand_legacy=False,
        )
        state["watched_slots_updated_at"] = _utc_now_iso()
        state["auto_watched_weekends"] = sorted(auto_watched)
        days_str = sorted({s["date"] for s in new_slots})
        print(f"New day {new_day_str} is open — auto-watched 9 AM for {days_str}.")
    return True


def _enqueue_work(kind: str, payload: dict | None = None, *, delay_seconds: int = 0) -> bool:
    """Queue internal Lambda work without sleeping inside a running invocation."""
    if not WORK_QUEUE_URL:
        print(f"Work queue is not configured; cannot enqueue {kind}.")
        return False
    delay = max(0, min(int(delay_seconds), SQS_DELAY_MAX_SECONDS))
    body = {"kind": kind, **(payload or {})}
    boto3.client("sqs", region_name="us-west-2").send_message(
        QueueUrl=WORK_QUEUE_URL,
        DelaySeconds=delay,
        MessageBody=json.dumps(body),
    )
    print(f"Queued {kind}" + (f" in {delay}s." if delay else "."))
    return True


# ── Date navigation ───────────────────────────────────────────────────────────

async def select_date(page, target: date) -> None:
    # Dismiss any lingering overlay (e.g. login backdrop still open)
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(200)
    await page.locator("button").filter(has_text=re.compile(r"^(Today|[A-Z][a-z]{2}\s+\d{1,2})$")).first.click()
    await page.wait_for_timeout(400)

    target_header = target.strftime("%B %Y")
    for _ in range(4):
        body = await page.inner_text("body")
        if target_header in body:
            break
        await page.get_by_role("button", name="Go to the Next Month").click()
        await page.wait_for_timeout(500)

    day_name = target.strftime("%A")
    month    = target.strftime("%B")
    aria     = f"{day_name}, {month} {ordinal(target.day)}, {target.year}"
    await page.get_by_role("button", name=aria).click(timeout=5_000)
    await page.wait_for_timeout(200)
    await page.get_by_role("button", name="Done").click(timeout=5_000)
    await page.wait_for_timeout(800)


# ── Slot collection ───────────────────────────────────────────────────────────

async def collect_all_slot_buttons(page, stop_when_found: set[str] | None = None) -> list[str]:
    """Collect visible time slot buttons. If stop_when_found is given, stop scrolling
    as soon as all those times are visible — avoids paginating through unused slots."""
    seen: set[str] = set()
    slots: list[str] = []

    async def harvest():
        texts = await page.locator("button").all_inner_texts()
        for text in texts:
            first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
            if TIME_RE.match(first_line) and first_line not in seen:
                seen.add(first_line)
                slots.append(first_line)

    await harvest()

    for _ in range(20):
        if stop_when_found and stop_when_found.issubset(seen):
            break
        next_btn = page.get_by_role("button", name="Next slot")
        if await next_btn.count() == 0:
            break
        before = len(slots)
        try:
            await next_btn.click(timeout=2_000)
        except PwTimeout:
            break
        await page.wait_for_timeout(400)
        await harvest()
        if len(slots) == before:
            break

    return slots


async def get_courts_from_modal(page) -> dict[str, bool]:
    court_availability = {court: False for court in TARGET_COURTS}
    try:
        dialog = page.locator('[role="dialog"]').first
        await dialog.wait_for(timeout=5_000)
        await page.wait_for_timeout(300)
        court_combo = dialog.locator('button[role="combobox"]').last
        if await court_combo.count() > 0:
            try:
                await court_combo.click(timeout=2_000)
                await page.wait_for_timeout(250)
                option_texts = await page.locator('[role="option"]').all_inner_texts()
                for text in option_texts:
                    match = COURT_RE.match(text.strip())
                    if not match:
                        continue
                    court = _normalize_court_number(match.group(1))
                    if court:
                        court_availability[court] = True
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(150)
            except PwTimeout:
                pass
        if not any(court_availability.values()):
            raw = await dialog.inner_text()
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            for line in lines:
                match = COURT_RE.match(line)
                if not match:
                    continue
                court = _normalize_court_number(match.group(1))
                if court:
                    court_availability[court] = True
    except PwTimeout:
        pass
    try:
        close = page.locator(
            '[role="dialog"] button[aria-label*="close" i], '
            '[role="dialog"] button[aria-label*="dismiss" i], '
            '[role="dialog"] button:text-is("Cancel"), '
            '[role="dialog"] button:text-is("Close")'
        ).first
        await close.click(timeout=3_000)
    except PwTimeout:
        await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)
    return court_availability


def _get_cached_jwt(state: dict) -> str | None:
    jwt = state.get("cached_jwt")
    expires_at = state.get("cached_jwt_expires_at")
    if not jwt or not expires_at:
        return None
    try:
        exp = datetime.fromisoformat(expires_at)
        if datetime.now(tz=timezone.utc) < exp - timedelta(minutes=2):
            return jwt
    except (ValueError, TypeError):
        pass
    return None


def _cache_jwt(state: dict, jwt: str) -> None:
    state["cached_jwt"] = jwt
    state["cached_jwt_expires_at"] = (datetime.now(tz=timezone.utc) + timedelta(minutes=55)).isoformat()


def _firebase_login() -> str:
    """Return a fresh rec.us Bearer token via Firebase REST auth (~0.4 s, no browser)."""
    payload = json.dumps({
        "returnSecureToken": True,
        "email": EMAIL,
        "password": PASSWORD,
        "clientType": "CLIENT_TYPE_WEB",
    }).encode()
    hdrs = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.rec.us/",
        "Origin": "https://www.rec.us",
    }
    req = Request(FIREBASE_SIGN_IN_URL, data=payload, headers=hdrs, method="POST")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["idToken"]


def _rec_api(url: str, method: str = "GET", body=None, jwt: str = "") -> tuple[int, dict]:
    """Thin HTTP wrapper for the rec.us JSON API."""
    from urllib.error import HTTPError
    data = json.dumps(body).encode() if body else None
    hdrs: dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    if jwt:
        hdrs["Authorization"] = "Bearer " + jwt
    req = Request(url, data=data, headers=hdrs, method=method)
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as e:
        raw = e.read() or b""
        try:
            body_r: dict = json.loads(raw) if raw else {}
        except Exception:
            body_r = {"raw": raw.decode(errors="replace")[:300]}
        return e.code, body_r


def _rec_api_required(url: str, *, jwt: str) -> dict:
    status, body = _rec_api(url, jwt=jwt)
    if status != 200:
        raise RuntimeError(f"rec.us API failed [{status}]: {json.dumps(body)[:200]}")
    return body


def _rec_user_id(jwt: str) -> str:
    body = _rec_api_required("https://api.rec.us/v1/users/me", jwt=jwt)
    user = body.get("data") or body.get("user") or body
    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        raise RuntimeError("rec.us user id not found")
    return user_id


def _slot_from_rec_reservation(reservation: dict, court: str) -> dict | None:
    time_range = reservation.get("reservationTimestampRange")
    if not isinstance(time_range, list) or not time_range:
        return None
    start_text = str(time_range[0])
    parts = start_text.split()
    if len(parts) < 2:
        return None
    date_text, time_text = parts[0], parts[1]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
        return None
    slot_time = _HHMM_TO_TIME_TEXT.get(time_text[:5])
    if slot_time is None:
        return None
    return {"date": date_text, "time": slot_time, "court": court}


def _extract_my_reservations_from_rec_bookings(page: dict) -> list[dict]:
    included = page.get("included") if isinstance(page, dict) else {}
    included = included if isinstance(included, dict) else {}
    reservations = [
        r for r in included.get("reservations", [])
        if isinstance(r, dict) and not r.get("canceledAt")
    ]
    sites = {
        s.get("id"): s
        for s in included.get("sites", [])
        if isinstance(s, dict) and s.get("id")
    }
    reservation_site_ids = included.get("reservationSiteIds") or {}
    if not isinstance(reservation_site_ids, dict):
        reservation_site_ids = {}

    reservations_by_id = {r.get("id"): r for r in reservations if r.get("id")}
    reservations_by_facility: dict[str, list[dict]] = {}
    for reservation in reservations:
        facility_id = reservation.get("facilityRentalId")
        if facility_id:
            reservations_by_facility.setdefault(facility_id, []).append(reservation)

    slots = []
    bookings = page.get("data", []) if isinstance(page, dict) else []
    for booking in bookings:
        if not isinstance(booking, dict):
            continue
        if booking.get("status") != "confirmed" or booking.get("canceledAt"):
            continue
        matching_reservations = []
        linked_reservation_id = booking.get("linkedReservationId")
        if linked_reservation_id and linked_reservation_id in reservations_by_id:
            matching_reservations.append(reservations_by_id[linked_reservation_id])
        facility_id = booking.get("facilityRentalId")
        if facility_id:
            matching_reservations.extend(reservations_by_facility.get(facility_id, []))

        seen_reservations: set[str] = set()
        for reservation in matching_reservations:
            reservation_id = reservation.get("id")
            if reservation_id in seen_reservations:
                continue
            if reservation_id:
                seen_reservations.add(reservation_id)
            site_ids = reservation_site_ids.get(reservation_id, []) if reservation_id else []
            for site_id in site_ids:
                site = sites.get(site_id) or {}
                court = _normalize_court_number(site.get("courtNumber") or site.get("name"))
                if not court:
                    continue
                slot = _slot_from_rec_reservation(reservation, court)
                if slot and slot["date"] >= date.today().isoformat():
                    slots.append(slot)
    return _normalize_slot_records(slots, expand_legacy=False)


def fetch_rec_my_reservations(jwt: str | None = None) -> list[dict]:
    jwt = jwt or _firebase_login()
    user_id = _rec_user_id(jwt)
    all_slots: list[dict] = []
    page_num = 1
    page_size = 100
    while True:
        query = f"pg[num]={page_num}&pg[size]={page_size}"
        page = _rec_api_required(
            f"https://api.rec.us/v1/users/{user_id}/bookings?{query}",
            jwt=jwt,
        )
        all_slots.extend(_extract_my_reservations_from_rec_bookings(page))
        pg = ((page.get("meta") or {}).get("pg") or {}) if isinstance(page, dict) else {}
        total = int(pg.get("totalResults") or len(page.get("data", []) or []))
        size = int(pg.get("size") or page_size)
        num = int(pg.get("num") or page_num)
        if num * size >= total:
            break
        page_num += 1

    order = {time_text: idx for idx, time_text in enumerate(SLOT_TIMES)}
    court_order = {court: idx for idx, court in enumerate(COURT_PREFERENCE)}
    normalized = _normalize_slot_records(all_slots, expand_legacy=False)
    return sorted(
        normalized,
        key=lambda s: (
            s["date"],
            order.get(s["time"], len(order)),
            court_order.get(s["court"], len(court_order)),
        ),
    )


def sync_rec_my_reservations(state: dict, *, strict: bool = False) -> bool:
    try:
        slots = fetch_rec_my_reservations()
    except Exception as exc:
        print(f"rec.us reservations sync failed: {exc}")
        if strict:
            raise
        return False

    state["my_reservations"] = slots
    state["my_reservations_synced_at"] = _utc_now_iso()
    state["my_reservations_source"] = "rec.us"
    return True


def book_slot_api(jwt: str, target_date: date, time_text: str, court: str) -> bool:
    """Book a court fully via API (no browser)."""
    time_str = _TIME_TEXT_TO_HHMMSS.get(time_text)
    if not time_str:
        print(f"  book_slot_api: unknown time_text '{time_text}'")
        return False

    court_sport_id = COURT_SPORT_IDS.get(court)
    if not court_sport_id:
        print(f"  book_slot_api: unknown court '{court}'")
        return False

    date_str = target_date.isoformat()
    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    end_str = (start_dt + timedelta(hours=1)).strftime("%H:%M:%S")

    print(f"  Booking Court {court} {date_str} {time_text} via API…")
    s, order = _rec_api(
        "https://api.rec.us/v1/reservations",
        method="POST",
        body={
            "courtSportIds": [court_sport_id],
            "from": {"date": date_str, "time": time_str},
            "participantUserId": PARTICIPANT_USER_ID,
            "to": {"date": date_str, "time": end_str},
        },
        jwt=jwt,
    )
    if s not in (200, 201):
        print(f"  API booking failed [{s}]: {json.dumps(order)[:200]}")
        return False

    od = order.get("data", order)
    order_id: str = od["id"]
    total: int = od.get("total", 0)
    max_credit: int = od.get("maxCreditAdjustmentAllowed", 0)
    print(f"  Order {order_id[:8]} | ${total/100:.2f} | credit: ${max_credit/100:.2f}")

    s2, result = _rec_api(
        f"https://api.rec.us/v1/orders/{order_id}/pay",
        method="POST",
        body={"data": {}},
        jwt=jwt,
    )
    if s2 == 200:
        print(f"  Confirmed! Court {court} {date_str} {time_text}.")
        return True

    print(f"  Payment failed [{s2}]: {json.dumps(result)[:200]}")
    return False


async def scan_day(page, target: date, target_time: str | None = None) -> list[dict]:
    """Full scan with court details — for the /scan endpoint."""
    label = target.strftime("%a %b %-d")
    print(f"  {label}… ", end="", flush=True)
    await select_date(page, target)
    slot_times = await collect_all_slot_buttons(page)
    if target_time:
        slot_times = [t for t in slot_times if t.upper() == target_time.upper()]
    if not slot_times:
        print("no matching slots.")
        return []
    results: list[dict] = []
    for time_text in slot_times:
        btn = page.locator("button").filter(has_text=re.compile(re.escape(time_text)))
        try:
            await btn.first.click(timeout=4_000)
        except PwTimeout:
            results.append({"time": time_text, "courts": [], "court_avail": _empty_court_availability(None), "preferred_court": None})
            continue
        court_availability = await get_courts_from_modal(page)
        if not any(court_availability.values()):
            court_availability = _empty_court_availability(None)
        available_courts = [
            f"Court {court} - Pickleball"
            for court in COURT_PREFERENCE
            if court_availability.get(court) is True
        ]
        results.append({
            "time": time_text,
            "courts": available_courts,
            "court_avail": court_availability,
            "preferred_court": _preferred_open_court(court_availability),
        })
    print(f"{len(results)} slot(s): {', '.join(s['time'] for s in results)}")
    return results


def _slots_to_availability(slots: list[dict]) -> dict[str, dict[str, bool | None]]:
    availability = {t: _empty_court_availability(False) for t in SLOT_TIMES}
    for slot in slots:
        time_text = slot.get("time")
        if time_text in availability:
            court_availability = slot.get("court_avail")
            if isinstance(court_availability, dict):
                availability[time_text] = _normalize_time_availability(court_availability)
            else:
                availability[time_text] = _empty_court_availability(None)
    return availability


# ── Browser session helpers ───────────────────────────────────────────────────

def _browser_args() -> list[str]:
    return ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]


def _user_agent() -> str:
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )


_BLOCK_RESOURCE_TYPES = {"stylesheet", "image", "font", "media"}
_BLOCK_URL_PATTERNS = (
    "google-analytics.com",
    "googletagmanager.com",
    "analytics.google.com",
    "segment.io",
    "segment.com",
    "amplitude.com",
    "mixpanel.com",
    "hotjar.com",
    "intercom.io",
    "intercomcdn.com",
    "fullstory.com",
    "heap.io",
    "sentry.io",
    "datadog-browser-agent",
)


async def _block_non_essential(route, request):
    if request.resource_type in _BLOCK_RESOURCE_TYPES:
        await route.abort()
        return
    if any(p in request.url for p in _BLOCK_URL_PATTERNS):
        await route.abort()
        return
    await route.continue_()


_SESSION_FILE = "/tmp/pl_session.json"


async def _new_page(pw):
    browser = await pw.chromium.launch(headless=HEADLESS, args=_browser_args())
    session = _SESSION_FILE if os.path.exists(_SESSION_FILE) else None
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=_user_agent(),
        storage_state=session,
    )
    page = await context.new_page()
    await page.route("**/*", _block_non_essential)
    return browser, context, page


# ── Full scan (existing /scan endpoint) ───────────────────────────────────────

async def main(*, targets=None, target_time=None) -> dict[str, list[dict]]:
    async with async_playwright() as pw:
        browser, context, page = await _new_page(pw)
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)

        all_results: dict[str, list[dict]] = {}
        targets = targets or load_check_dates()
        if not targets:
            print("No dates to check.")
            return {}

        labels = ", ".join(t.strftime("%a %b %-d") for t in targets)
        print(f"Scanning {labels} for {target_time or 'all slots'}…\n")

        for target in targets:
            slots = await scan_day(page, target, target_time=target_time)
            if slots:
                all_results[target.strftime("%A, %B %-d")] = slots

        await browser.close()

    print()
    print("=" * 60)
    print(f"  AVAILABILITY — {target_time or 'all visible slots'}")
    print("=" * 60)
    if not all_results:
        print(f"\n  No {target_time or ''} slots available.\n")
    else:
        for day_label, slots in all_results.items():
            print(f"\n  {day_label}")
            print(f"  {'─' * 40}")
            for s in slots:
                courts_str = ", ".join(s["courts"]) if s["courts"] else "(could not read courts)"
                print(f"    {s['time']:<12}  →  {courts_str}")
    print()
    return all_results


# ── Direct HTTP availability API ─────────────────────────────────────────────

_REC_API_BASE = "https://api.rec.us/v1/sites"
_API_TIMEOUT  = 10  # seconds per request


def _time_text_to_hhmm(time_text: str) -> str:
    """'9:00 AM' → '09:00'  (matches HH:MM prefix in API start_time strings)."""
    try:
        dt = datetime.strptime(time_text.strip(), "%I:%M %p")
        return dt.strftime("%H:%M")
    except ValueError:
        return ""


def _fetch_one_court_raw(court_num: str, site_id: str) -> tuple[str, dict[str, dict]]:
    """Fetch 14-day availability for one court from rec.us API.

    Returns (court_num, date_map) where date_map is {date_iso: {HH:MM:SS: {...}}}.
    Keys present in the map are available; absent = booked/unavailable.
    """
    url = f"{_REC_API_BASE}/{site_id}/availability"
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=_API_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    date_map = data.get("data") if isinstance(data, dict) else {}
    return court_num, date_map or {}


# Build a lookup from "HH:MM" → time_text (e.g. "09:00" → "9:00 AM") once at import time.
_HHMM_TO_TIME_TEXT: dict[str, str] = {
    _time_text_to_hhmm(t): t for t in SLOT_TIMES if _time_text_to_hhmm(t)
}


def _api_fetch_availability(
    target_times_by_date: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, dict[str, bool | None]]]:
    """Fetch availability for all courts in parallel via the rec.us REST API.

    Returns {date_iso: {time_text: {court_num: True|False|None}}}.
    A slot is True (open) if the API returns it; absent = False (booked/unavailable).
    """
    raw: dict[str, dict[str, dict]] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_fetch_one_court_raw, court_num, site_id): court_num
            for court_num, site_id in COURT_SITE_IDS.items()
        }
        for future in as_completed(futures):
            court_num, date_map = future.result()
            raw[court_num] = date_map

    date_strs = {
        (date.today() + timedelta(days=offset)).isoformat()
        for offset in range(16)
    }
    if target_times_by_date is not None:
        date_strs.update(target_times_by_date.keys())
    for date_map in raw.values():
        date_strs.update(date_map.keys())

    result: dict[str, dict[str, dict[str, bool | None]]] = {
        date_str: {
            time_text: {court: False for court in TARGET_COURTS}
            for time_text in SLOT_TIMES
        }
        for date_str in sorted(date_strs)
    }

    # Mark open slots True for ALL dates the API returns — no date filter.
    # This gives us free coverage of weekdays we didn't explicitly watch.
    for court_num, date_map in raw.items():
        for date_str, times_dict in date_map.items():
            for time_key in times_dict:
                hhmm = time_key[:5]  # "09:00:00" → "09:00"
                time_text = _HHMM_TO_TIME_TEXT.get(hhmm)
                if time_text is None:
                    continue
                result.setdefault(date_str, {}).setdefault(
                    time_text, {c: False for c in TARGET_COURTS}
                )[court_num] = True

    return result


def _api_scan(
    target_times_by_date: dict[str, list[str]] | None = None,
    auto_book_slots: list[dict] | None = None,
    jwt: str | None = None,
) -> tuple[dict[str, dict[str, dict[str, bool | None]]], list[dict]]:
    """Scan via HTTP API and book any newly open auto-book slots via Playwright.

    Returns (availability, booked_slots) matching the _scan_dates_quick contract.
    """
    new_avail = _api_fetch_availability(target_times_by_date)

    if not auto_book_slots:
        return new_avail, []

    today_str = date.today().isoformat()
    auto_book_set: set[tuple[str, str]] = {
        (ab["date"], ab["time"])
        for ab in auto_book_slots
        if ab.get("date", "") >= today_str and ab.get("time", "") in SLOT_TIMES
    }

    booked: list[dict] = []
    to_book = [
        (date_str, time_text, new_avail[date_str][time_text])
        for date_str, time_map in new_avail.items()
        for time_text, court_avail in time_map.items()
        if (date_str, time_text) in auto_book_set and _preferred_open_court(court_avail) is not None
    ]

    if not to_book:
        return new_avail, []

    if jwt is None:
        try:
            state = load_state()
            jwt = _get_cached_jwt(state) or _firebase_login()
        except Exception as exc:
            try:
                send_telegram(f"❌ Auto-book login failed: {exc}")
            except Exception:
                pass
            raise
    for date_str, time_text, court_avail in to_book:
        open_courts = [c for c in COURT_PREFERENCE if court_avail.get(c) is True]
        try:
            send_telegram(f"🎯 Trying to book {date_str} {time_text} (courts: {', '.join(open_courts)})")
        except Exception:
            pass
        booked_court: str | None = None
        for attempt in range(1, 6):  # up to 5 quick retries for 8am race conditions
            for court in COURT_PREFERENCE:
                if court_avail.get(court) is not True:
                    continue
                try:
                    ok = book_slot_api(jwt, date.fromisoformat(date_str), time_text, court)
                except Exception as exc:
                    print(f"  Booking error {date_str} {time_text} Court {court} (attempt {attempt}/5): {exc}")
                    ok = False
                if ok:
                    booked_court = court
                    break  # stop trying other courts
            if booked_court:
                break  # stop retrying
            if attempt < 5:
                print(f"  All courts failed (attempt {attempt}/5), retrying…")
        if booked_court:
            booked.append({"date": date_str, "time": time_text, "court": booked_court})
            for c in new_avail.get(date_str, {}).get(time_text, {}):
                new_avail[date_str][time_text][c] = False
        else:
            try:
                send_telegram(f"❌ Failed to book {date_str} {time_text} after 5 attempts")
            except Exception:
                pass
    return new_avail, booked


# ── Auto-book helpers ─────────────────────────────────────────────────────────

def _apply_booked_slots(state: dict, booked_slots: list[dict]) -> None:
    """Update state after successful bookings: remove from auto_book_slots, add to my_reservations."""
    if not booked_slots:
        return
    booked_keys: set[tuple[str, str]] = {(b["date"], b["time"]) for b in booked_slots}
    state["auto_book_slots"] = [
        ab for ab in (state.get("auto_book_slots") or [])
        if (ab.get("date"), ab.get("time")) not in booked_keys
    ]
    existing_reservations = {
        (r["date"], r["time"], r["court"])
        for r in (state.get("my_reservations") or [])
    }
    for b in booked_slots:
        key = (b["date"], b["time"], b["court"])
        if key not in existing_reservations:
            state.setdefault("my_reservations", []).append(
                {"date": b["date"], "time": b["time"], "court": b["court"]}
            )
            existing_reservations.add(key)
    state["my_reservations_synced_at"] = _utc_now_iso()
    state["my_reservations_source"] = "auto-book"


def _notify_booked_slots(booked_slots: list[dict]) -> None:
    """Send SMS/email notification for each successfully booked slot."""
    if not booked_slots:
        return
    lines = [f"{b['date']} {b['time']} Court {b['court']}" for b in booked_slots]
    msg = "Auto-booked pickleball slot(s):\n" + "\n".join(lines)
    notify(msg, subject="Pickleball auto-booking confirmed")


# ── Scheduled worker ──────────────────────────────────────────────────────────

def _new_day_iso(today: date | None = None) -> str:
    return ((today or date.today()) + timedelta(days=14)).isoformat()


def _new_day_from_pt_now(now_pt: datetime | None = None) -> date:
    return (now_pt or datetime.now(tz=PT)).date() + timedelta(days=14)


def _matches_focused_scan_policy(date_str: str, time_text: str) -> bool:
    """Focused policy: scan the new day at all times, plus 9 AM on weekends only."""
    if date_str == _new_day_iso():
        return True
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return False
    return d.weekday() >= 5 and time_text == "9:00 AM"


def _future_watched_time_map(state: dict) -> dict[str, list[str]]:
    today_str = date.today().isoformat()
    watched_by_date: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    for slot in state.get("watched_slots", []):
        slot_date = slot.get("date")
        slot_time = slot.get("time")
        if not slot_date or not slot_time or slot_date < today_str:
            continue
        if not _matches_focused_scan_policy(slot_date, slot_time):
            continue
        key = (slot_date, slot_time)
        if key in seen:
            continue
        seen.add(key)
        watched_by_date.setdefault(slot_date, set()).add(slot_time)
    return {
        slot_date: [time_text for time_text in SLOT_TIMES if time_text in times]
        for slot_date, times in watched_by_date.items()
    }


def _auto_book_time_map(auto_book_slots: list[dict]) -> dict[str, list[str]]:
    today_str = date.today().isoformat()
    auto_book_by_date: dict[str, set[str]] = {}
    for slot in auto_book_slots:
        slot_date = slot.get("date", "")
        slot_time = slot.get("time", "")
        if not slot_date or not slot_time or slot_date < today_str or slot_time not in SLOT_TIMES:
            continue
        auto_book_by_date.setdefault(slot_date, set()).add(slot_time)
    return {
        slot_date: [time_text for time_text in SLOT_TIMES if time_text in times]
        for slot_date, times in auto_book_by_date.items()
    }


def _watched_and_auto_book_targets(state: dict) -> dict[str, list[str]]:
    watched_times = _future_watched_time_map(state)
    auto_book_times = _auto_book_time_map(state.get("auto_book_slots") or [])

    if state.get("focus_newest_weekend"):
        weekend_dates = [d for d in watched_times if date.fromisoformat(d).weekday() >= 5]
        if len(weekend_dates) > 1:
            newest = max(weekend_dates)
            for d in weekend_dates:
                if d != newest:
                    del watched_times[d]

    for ab_date, ab_times in auto_book_times.items():
        existing = set(watched_times.get(ab_date, []))
        existing.update(ab_times)
        watched_times[ab_date] = [t for t in SLOT_TIMES if t in existing]

    return watched_times


def _is_within_scan_window() -> bool:
    return 8 <= datetime.now(tz=PT).hour <= 22  # 8 AM – 10 PM PT inclusive


def _is_publish_detection_window() -> bool:
    hour = datetime.now(tz=PT).hour
    return hour == 23 or 0 <= hour < 8  # 11 PM – 7:59 AM PT


def _scheduled_scan_targets(state: dict) -> tuple[dict[str, list[str]], bool]:
    """Return (targets, is_special_new_day_scan) for the scheduled worker."""
    now_pt = datetime.now(tz=PT)
    target_14 = (date.today() + timedelta(days=14)).isoformat()

    # The 8 AM and 9 AM runs are mandatory probes for the newly opened day only.
    if now_pt.hour in {8, 9}:
        return {target_14: ["8:00 AM", "9:00 AM"]}, True

    return _watched_and_auto_book_targets(state), False


def _full_scan_targets() -> dict[str, list[str]]:
    today = date.today()
    return {
        (today + timedelta(days=day_offset)).isoformat(): SLOT_TIMES[:]
        for day_offset in range(16)
    }


def _run_full_refresh_worker(*, force: bool = False) -> None:
    """Refresh all displayed dates/times for an ad-hoc full scan."""
    state = load_state()
    active_scan = _active_scan_started_at(state)
    if active_scan:
        print(f"Another scan started at {active_scan.isoformat()}. Skipping.")
        return

    _auto_watch_upcoming_weekends(state)
    scan_targets = _full_scan_targets()
    history_targets = _history_targets_from_map(scan_targets)
    started_at = _utc_now_iso()

    state["scan_started_at"] = started_at
    state["scan_started_kind"] = "ad_hoc"
    save_state(state)

    print(
        "Scanning all displayed slots for "
        + ", ".join(f"{d} ({', '.join(scan_targets[d])})" for d in sorted(scan_targets))
        + "…"
    )
    auto_book_slots = state.get("auto_book_slots") or []
    try:
        new_avail, booked_slots = _api_scan(
            target_times_by_date=scan_targets,
            auto_book_slots=auto_book_slots,
        )

        state = load_state()
        availability = state.get("availability", {})
        availability.update(new_avail)
        state["availability"] = availability
        state["last_scan_started_at"] = started_at
        state["last_scanned"] = _utc_now_iso()
        state["last_scan_kind"] = "ad_hoc"
        _record_scan_history(
            state,
            kind="ad_hoc",
            started_at=started_at,
            completed_at=state["last_scanned"],
            status="completed",
            targets=_attach_history_results(history_targets, new_avail),
        )

        open_target_lines = _alert_lines_for_open_targets(state, new_avail)

        sync_rec_my_reservations(state)
        _apply_booked_slots(state, booked_slots)
        state["notified_slots"] = []
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
            state.pop("scan_started_kind", None)
        save_state(state)
        _notify_booked_slots(booked_slots)
    except Exception as exc:
        state = load_state()
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
            state.pop("scan_started_kind", None)
            _record_scan_history(
                state,
                kind="ad_hoc",
                started_at=started_at,
                completed_at=_utc_now_iso(),
                status="failed",
                targets=history_targets,
                error=str(exc),
            )
            save_state(state)
        raise

    if open_target_lines:
        msg = "Pickleball slot(s) now available:\n" + "\n".join(open_target_lines)
        notify(msg)
    else:
        print("No open watched or auto-book slots in this scan.")


def _run_scheduled_worker() -> None:
    """Hourly watched-slot scan used by the EventBridge cron."""
    if not _is_within_scan_window():
        print("Outside scan window (8 AM – 10 PM PT). Skipping.")
        return

    state = load_state()
    active_scan = _active_scan_started_at(state)
    if active_scan:
        print(f"Another scheduled scan started at {active_scan.isoformat()}. Skipping.")
        return

    if _auto_watch_upcoming_weekends(state):
        save_state(state)

    watched_times, special_new_day_scan = _scheduled_scan_targets(state)

    if not watched_times:
        print("No future watched slots or auto-book slots. Skipping cron scan.")
        return

    started_at = _utc_now_iso()
    state["scan_started_at"] = started_at
    state["scan_started_kind"] = "scheduled"
    save_state(state)

    print(
        "Scanning watched slots for "
        + ", ".join(f"{d} ({', '.join(watched_times[d])})" for d in sorted(watched_times))
        + "…"
    )
    auto_book_slots = state.get("auto_book_slots") or []
    try:
        new_avail, booked_slots = _api_scan(
            target_times_by_date=watched_times,
            auto_book_slots=auto_book_slots,
        )

        # Build history targets from actual results so weekend days show all fetched times.
        history_targets = _attach_history_results(
            _history_targets_from_map({d: list(t.keys()) for d, t in new_avail.items()}),
            new_avail,
        )

        state = load_state()
        availability = state.get("availability", {})
        for date_str, time_map in new_avail.items():
            day_availability = availability.get(date_str, {})
            for time_text, court_availability in time_map.items():
                day_availability[time_text] = _normalize_time_availability(court_availability)
            availability[date_str] = day_availability
        state["availability"] = availability
        _auto_watch_on_new_day_openings(state, new_avail)
        state["last_scan_started_at"] = started_at
        state["last_scanned"] = _utc_now_iso()
        state["last_scan_kind"] = "scheduled"
        _record_scan_history(
            state,
            kind="scheduled",
            started_at=started_at,
            completed_at=state["last_scanned"],
            status="completed",
            targets=history_targets,
        )

        open_target_lines = _alert_lines_for_open_targets(state, new_avail)

        sync_rec_my_reservations(state)
        _apply_booked_slots(state, booked_slots)
        state["notified_slots"] = []
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
            state.pop("scan_started_kind", None)
        save_state(state)
        _notify_booked_slots(booked_slots)
    except Exception as exc:
        state = load_state()
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
            state.pop("scan_started_kind", None)
            _record_scan_history(
                state,
                kind="scheduled",
                started_at=started_at,
                completed_at=_utc_now_iso(),
                status="failed",
                targets=history_targets,
                error=str(exc),
            )
            save_state(state)
        raise

    if open_target_lines:
        msg = "Pickleball slot(s) now available:\n" + "\n".join(open_target_lines)
        notify(msg)
    else:
        print("No open watched or auto-book slots in this scan.")


# ── Telegram bot (webhook) ────────────────────────────────────────────────────

ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", os.environ.get("API_PASSWORD", ""))
BOT_MODEL             = "claude-haiku-4-5-20251001"
BOT_MAX_HISTORY_TURNS = 12
BOT_HISTORY_PREFIX    = "telegram_history/"

BOT_SYSTEM_PROMPT = """You are a helpful assistant managing a pickleball court monitor.

Capabilities (via tools):
- Read current monitor state (slot availability, watched/auto-book lists).
- Trigger an ad-hoc scan.
- Add or remove watched slots (date + time + court 4/5/6).
- Add or remove auto-book slots (date + time). The monitor will book them when an open court appears on the next scan.
- Toggle weekend 9 AM auto-watch.

Conventions:
- Times are "8:00 AM", "9:00 AM", "10:00 AM", "11:00 AM", "4:00 PM", "5:00 PM", "6:00 PM".
- Courts: "4", "5", "6" (preference order: 6 > 4 > 5).
- Dates: YYYY-MM-DD. Resolve relative dates ("next Saturday", "this Sunday") using today's Pacific date.
- Before any write (watch/auto-book), confirm with the user. Auto-booking is irreversible once a court opens.
- Call get_state first if you're unsure about current watched/auto-book lists.

Keep replies short. Use plain text."""

BOT_TOOLS: list[dict] = [
    {
        "name": "get_state",
        "description": "Return current monitor state: availability, watched_slots, auto_book_slots, auto_watch_weekends_enabled, last_scanned.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "scan_now",
        "description": "Trigger an immediate scan. Pass `dates` (YYYY-MM-DD list) to scan specific dates, or omit to scan all visible slots.",
        "input_schema": {
            "type": "object",
            "properties": {"dates": {"type": "array", "items": {"type": "string"}}},
            "required": [],
        },
    },
    {
        "name": "add_watched_slots",
        "description": "Add (date, time, court) triplets to the watched list. Court must be '4', '5', or '6'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                            "court": {"type": "string"},
                        },
                        "required": ["date", "time", "court"],
                    },
                }
            },
            "required": ["slots"],
        },
    },
    {
        "name": "remove_watched_slots",
        "description": "Remove (date, time, court) triplets from the watched list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                            "court": {"type": "string"},
                        },
                        "required": ["date", "time", "court"],
                    },
                }
            },
            "required": ["slots"],
        },
    },
    {
        "name": "add_auto_book_slots",
        "description": "Add (date, time) pairs to the auto-book list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                        },
                        "required": ["date", "time"],
                    },
                }
            },
            "required": ["slots"],
        },
    },
    {
        "name": "remove_auto_book_slots",
        "description": "Remove (date, time) pairs from the auto-book list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                        },
                        "required": ["date", "time"],
                    },
                }
            },
            "required": ["slots"],
        },
    },
    {
        "name": "set_auto_watch_weekends",
        "description": "Enable or disable the weekend 9 AM auto-watch behavior.",
        "input_schema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    },
]


def _bot_compact_state() -> dict:
    state = load_state()
    today_str = date.today().isoformat()
    watched = [
        {"date": s["date"], "time": s["time"], "court": s["court"]}
        for s in state.get("watched_slots", [])
        if s.get("date", "") >= today_str
    ]
    open_slots = []
    for date_str, day_avail in state.get("availability", {}).items():
        if date_str < today_str:
            continue
        for time_text, court_avail in day_avail.items():
            for court, avail in court_avail.items():
                if avail is True:
                    open_slots.append({"date": date_str, "time": time_text, "court": court})
    return {
        "today": today_str,
        "last_scanned": state.get("last_scanned"),
        "scan_in_progress": bool(state.get("scan_started_at")),
        "watched_slots": watched,
        "auto_book_slots": state.get("auto_book_slots", []),
        "auto_watch_weekends_enabled": state.get("auto_watch_weekends_enabled", True),
        "open_slots": sorted(open_slots, key=lambda x: (x["date"], x["time"], x["court"])),
    }


def _bot_run_tool(name: str, args: dict):
    if name == "get_state":
        return _bot_compact_state()
    if name == "scan_now":
        date_strs = args.get("dates") or []
        if date_strs:
            for d_str in date_strs:
                date.fromisoformat(d_str)
        _run_full_refresh_worker(force=True)
        return {"ok": True, "message": "Scan completed", "state": _bot_compact_state()}
    if name == "add_watched_slots":
        state = load_state()
        existing = {(s["date"], s["time"], s["court"]) for s in state.get("watched_slots", [])}
        for s in args.get("slots", []):
            existing.add((s["date"], s["time"], s["court"]))
        state["watched_slots"] = _normalize_slot_records(
            [{"date": d, "time": t, "court": c} for d, t, c in sorted(existing)],
            expand_legacy=False,
        )
        state["watched_slots_updated_at"] = _utc_now_iso() if _has_future_watched_slots(state) else None
        save_state(state)
        return {"ok": True, "watched": len(state["watched_slots"])}
    if name == "remove_watched_slots":
        state = load_state()
        rm = {(s["date"], s["time"], s["court"]) for s in args.get("slots", [])}
        state["watched_slots"] = [
            s for s in state.get("watched_slots", [])
            if (s["date"], s["time"], s["court"]) not in rm
        ]
        state["watched_slots_updated_at"] = _utc_now_iso() if _has_future_watched_slots(state) else None
        save_state(state)
        return {"ok": True, "watched": len(state["watched_slots"])}
    if name == "add_auto_book_slots":
        state = load_state()
        existing = {(s["date"], s["time"]) for s in state.get("auto_book_slots", [])}
        for s in args.get("slots", []):
            existing.add((s["date"], s["time"]))
        state["auto_book_slots"] = [{"date": d, "time": t} for d, t in sorted(existing)]
        save_state(state)
        return {"ok": True, "auto_book": len(state["auto_book_slots"])}
    if name == "remove_auto_book_slots":
        state = load_state()
        rm = {(s["date"], s["time"]) for s in args.get("slots", [])}
        state["auto_book_slots"] = [
            s for s in state.get("auto_book_slots", [])
            if (s["date"], s["time"]) not in rm
        ]
        save_state(state)
        return {"ok": True, "auto_book": len(state["auto_book_slots"])}
    if name == "set_auto_watch_weekends":
        state = load_state()
        state["auto_watch_weekends_enabled"] = bool(args.get("enabled"))
        if args.get("enabled"):
            _auto_watch_upcoming_weekends(state)
        save_state(state)
        return {"ok": True, "auto_watch_weekends_enabled": bool(args.get("enabled"))}
    return {"error": f"unknown tool: {name}"}


def _bot_content_to_dicts(content) -> list[dict]:
    result = []
    for block in content or []:
        if isinstance(block, dict):
            result.append(block)
        elif hasattr(block, "model_dump"):
            result.append(block.model_dump())
        else:
            d: dict = {"type": getattr(block, "type", "text")}
            if hasattr(block, "text"):
                d["text"] = block.text
            if hasattr(block, "id"):
                d["id"] = block.id
            if hasattr(block, "name"):
                d["name"] = block.name
            if hasattr(block, "input"):
                d["input"] = block.input
            result.append(d)
    return result


def _load_chat_history(chat_id: str) -> list[dict]:
    if not STATE_BUCKET:
        return []
    s3 = boto3.client("s3", region_name="us-west-2")
    try:
        obj = s3.get_object(Bucket=STATE_BUCKET, Key=BOT_HISTORY_PREFIX + chat_id + ".json")
        data = json.loads(obj["Body"].read())
        return data if isinstance(data, list) else []
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return []
        raise


def _save_chat_history(chat_id: str, history: list[dict]) -> None:
    if not STATE_BUCKET:
        return
    trimmed = history[-(BOT_MAX_HISTORY_TURNS * 2):]
    s3 = boto3.client("s3", region_name="us-west-2")
    s3.put_object(
        Bucket=STATE_BUCKET,
        Key=BOT_HISTORY_PREFIX + chat_id + ".json",
        Body=json.dumps(trimmed),
        ContentType="application/json",
    )


def _load_telegram_usage() -> list[dict]:
    if not STATE_BUCKET:
        return []
    s3 = boto3.client("s3", region_name="us-west-2")
    try:
        obj = s3.get_object(Bucket=STATE_BUCKET, Key=TELEGRAM_USAGE_KEY)
        data = json.loads(obj["Body"].read())
        return data if isinstance(data, list) else []
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return []
        raise


def _record_telegram_usage(record: dict) -> None:
    if not STATE_BUCKET:
        return
    s3 = boto3.client("s3", region_name="us-west-2")
    try:
        obj = s3.get_object(Bucket=STATE_BUCKET, Key=TELEGRAM_USAGE_KEY)
        history = json.loads(obj["Body"].read())
        if not isinstance(history, list):
            history = []
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            history = []
        else:
            raise
    history.insert(0, record)
    s3.put_object(
        Bucket=STATE_BUCKET,
        Key=TELEGRAM_USAGE_KEY,
        Body=json.dumps(history[:TELEGRAM_USAGE_MAX]),
        ContentType="application/json",
    )


def _tg_send_to(chat_id: str, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        resp.read()


def _bot_reply(chat_id: str, user_text: str) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    history = _load_chat_history(chat_id)
    history.append({"role": "user", "content": user_text})
    now_pt = datetime.now(tz=PT)
    date_hint = f"Current Pacific time: {now_pt.strftime('%Y-%m-%d %A %H:%M %Z')}."
    cached_tools = [
        {**t, "cache_control": {"type": "ephemeral"}} if i == len(BOT_TOOLS) - 1 else t
        for i, t in enumerate(BOT_TOOLS)
    ]
    system_blocks = [
        {"type": "text", "text": BOT_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": date_hint},
    ]
    total_in = total_out = total_cache_read = total_cache_write = 0
    all_tools: list[str] = []
    started_at = _utc_now_iso()
    while True:
        response = client.messages.create(
            model=BOT_MODEL,
            max_tokens=1024,
            system=system_blocks,
            tools=cached_tools,
            messages=history,
        )
        content_dicts = _bot_content_to_dicts(response.content)
        u = response.usage
        total_in          += u.input_tokens
        total_out         += u.output_tokens
        total_cache_read  += getattr(u, "cache_read_input_tokens", 0) or 0
        total_cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        tools_called = [b.get("name") for b in content_dicts if b.get("type") == "tool_use"]
        all_tools.extend(tools_called)
        print(
            f"Bot [{chat_id}] tokens: in={u.input_tokens} out={u.output_tokens}"
            + (f" cache_read={u.cache_read_input_tokens}" if getattr(u, "cache_read_input_tokens", None) else "")
            + (f" cache_write={u.cache_creation_input_tokens}" if getattr(u, "cache_creation_input_tokens", None) else "")
            + (f" tools={tools_called}" if tools_called else "")
        )
        if response.stop_reason != "tool_use":
            text_out = "".join(
                b.get("text", "") for b in content_dicts if b.get("type") == "text"
            ).strip()
            history.append({"role": "assistant", "content": content_dicts})
            _save_chat_history(chat_id, history)
            _record_telegram_usage({
                "at": started_at,
                "input_tokens": total_in,
                "output_tokens": total_out,
                "cache_read_tokens": total_cache_read,
                "cache_write_tokens": total_cache_write,
                "tools": all_tools,
                "reply_preview": (text_out or "")[:80],
            })
            return text_out or "(no reply)"
        history.append({"role": "assistant", "content": content_dicts})
        tool_results = []
        for block in content_dicts:
            if block.get("type") != "tool_use":
                continue
            try:
                result = _bot_run_tool(block["name"], block.get("input", {}))
                content_str = json.dumps(result, default=str)
                is_error = False
            except Exception as exc:
                content_str = f"{type(exc).__name__}: {exc}"
                is_error = True
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": content_str,
                "is_error": is_error,
            })
        history.append({"role": "user", "content": tool_results})


def handle_telegram(event) -> dict:
    secret = get_header(event, "x-telegram-bot-api-secret-token")
    if TELEGRAM_WEBHOOK_SECRET and secret != TELEGRAM_WEBHOOK_SECRET:
        return {"statusCode": 403, "headers": CORS_HEADERS, "body": json.dumps({"error": "Forbidden"})}
    body = get_body(event)
    msg = body.get("message") or body.get("edited_message")
    if not msg:
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    allowed = {
        cid.strip()
        for cid in (os.environ.get("TELEGRAM_ALLOWED_IDS") or os.environ.get("TELEGRAM_CHAT_ID", "")).split(",")
        if cid.strip()
    }
    if allowed and chat_id not in allowed:
        print(f"Telegram: ignoring message from chat {chat_id}")
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
    text = (msg.get("text") or "").strip()
    if not text:
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
    if text in ("/reset", "/clear", "/start"):
        _save_chat_history(chat_id, [])
        _tg_send_to(chat_id, "Conversation reset.")
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
    try:
        answer = _bot_reply(chat_id, text)
    except Exception as exc:
        print(f"Telegram bot error for chat {chat_id}: {exc}")
        answer = f"Error: {type(exc).__name__}: {exc}"
    _tg_send_to(chat_id, answer)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _check_auth(event) -> bool:
    if _has_valid_sync_token(event):
        return True
    expected_user = os.environ.get("API_USERNAME", "")
    expected_pass = os.environ.get("API_PASSWORD", "")
    if not expected_user or not expected_pass:
        return False
    auth_header = get_header(event, "authorization")
    if not auth_header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username == expected_user and password == expected_pass
    except Exception:
        return False


# ── Route handlers ────────────────────────────────────────────────────────────

def handle_state(event) -> dict:
    state = load_state()
    today = date.today()

    watched_set   = {(s["date"], s["time"], s["court"]) for s in state.get("watched_slots", [])}
    mine_set      = {(s["date"], s["time"], s["court"]) for s in state.get("my_reservations", [])}
    auto_book_set = {(ab["date"], ab["time"]) for ab in state.get("auto_book_slots", [])}

    grid = []
    for i in range(16):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        day_avail = state.get("availability", {}).get(d_str, {})
        slots = []
        for t in SLOT_TIMES:
            time_avail = _normalize_time_availability(day_avail.get(t))
            for court in COURT_PREFERENCE:
                slots.append({
                    "time":        t,
                    "court":       court,
                    "available":   time_avail.get(court),
                    "watching":    (d_str, t, court) in watched_set,
                    "mine":        (d_str, t, court) in mine_set,
                    "auto_booking": (d_str, t) in auto_book_set,
                })
        grid.append({
            "date":  d_str,
            "label": d.strftime("%a, %b %-d"),
            "slots": slots,
        })

    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({
            "grid":                grid,
            "slot_times":          SLOT_TIMES,
            "court_nums":          COURT_PREFERENCE,
            "court_preference":    COURT_PREFERENCE,
            "app_version":         APP_VERSION,
            "watched_slots_updated_at": state.get("watched_slots_updated_at"),
            "scan_started_at":     state.get("scan_started_at"),
            "scan_started_kind":   state.get("scan_started_kind"),
            "last_scanned":        state.get("last_scanned"),
            "last_scan_started_at": state.get("last_scan_started_at"),
            "last_scan_kind":      state.get("last_scan_kind"),
            "recent_scan_history": state.get("recent_scan_history", []),
            "scan_interval_hours": state.get("scan_interval_hours", 1.0),
            "queued_scheduled_probe_at": state.get("queued_scheduled_probe_at"),
            "queued_publish_probe_date": state.get("queued_publish_probe_date"),
            "rec_url":             BASE_URL,
            "auto_book_slots":     state.get("auto_book_slots", []),
            "my_reservations_synced_at": state.get("my_reservations_synced_at"),
            "my_reservations_source": state.get("my_reservations_source"),
            "auto_watch_weekends_enabled": bool(state.get("auto_watch_weekends_enabled", True)),
            "focus_newest_weekend": bool(state.get("focus_newest_weekend", False)),
            "telegram_call_history": _load_telegram_usage()[:50],
        }),
    }


def handle_watch(event) -> dict:
    body = get_body(event)
    slots = body.get("slots")
    if slots is None:
        return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "Missing slots"})}
    for s in slots:
        try:
            date.fromisoformat(s["date"])
            if _normalize_court_number(s.get("court")) is None:
                raise ValueError("Missing court")
        except (KeyError, ValueError):
            return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": f"Invalid slot: {s}"})}

    state = load_state()
    state["watched_slots"] = _normalize_slot_records(slots, expand_legacy=False)
    state["watched_slots_updated_at"] = _utc_now_iso() if _has_future_watched_slots(state) else None
    watched_keys = {f"{s['date']}|{s['time']}|{s['court']}" for s in state["watched_slots"]}
    state["notified_slots"] = [n for n in state.get("notified_slots", []) if n in watched_keys]
    save_state(state)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "watched": len(state['watched_slots'])})}


def handle_my_reservations(event) -> dict:
    body = get_body(event)
    slots = body.get("slots")
    if slots is None:
        return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "Missing slots"})}
    for s in slots:
        try:
            date.fromisoformat(s["date"])
            if _normalize_court_number(s.get("court")) is None:
                raise ValueError("Missing court")
        except (KeyError, ValueError):
            return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": f"Invalid slot: {s}"})}
    state = load_state()
    state["my_reservations"] = _normalize_slot_records(slots, expand_legacy=False)
    state["my_reservations_synced_at"] = _utc_now_iso()
    state["my_reservations_source"] = "manual"
    save_state(state)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "mine": len(state['my_reservations'])})}


def handle_my_reservations_refresh(event) -> dict:
    state = load_state()
    try:
        sync_rec_my_reservations(state, strict=True)
    except Exception as exc:
        print(f"handle_my_reservations_refresh failed: {exc}")
        return {
            "statusCode": 502,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Failed to fetch rec.us reservations: {exc}"}),
        }
    save_state(state)
    slots = state.get("my_reservations", [])
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({
            "ok": True,
            "mine": len(slots),
            "slots": slots,
            "synced_at": state["my_reservations_synced_at"],
        }),
    }


def handle_auto_book(event) -> dict:
    body = get_body(event)
    slots = body.get("slots")
    if slots is None:
        return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "Missing slots"})}
    today_str = date.today().isoformat()
    normalized: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for s in slots:
        try:
            slot_date = s["date"]
            date.fromisoformat(slot_date)  # validate format
            slot_time = s["time"]
            if slot_time not in SLOT_TIMES:
                raise ValueError(f"Invalid time: {slot_time}")
            if slot_date < today_str:
                continue  # silently drop past dates
        except (KeyError, ValueError) as exc:
            return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": f"Invalid slot: {s} — {exc}"})}
        key = (slot_date, slot_time)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"date": slot_date, "time": slot_time})

    state = load_state()
    state["auto_book_slots"] = normalized
    save_state(state)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "auto_book": len(normalized)})}


def handle_focus_newest_weekend(event) -> dict:
    body = get_body(event)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"ok": False, "error": "enabled must be true or false"}),
        }
    state = load_state()
    state["focus_newest_weekend"] = enabled
    save_state(state)
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({"ok": True, "focus_newest_weekend": enabled}),
    }


def handle_auto_watch_weekends(event) -> dict:
    body = get_body(event)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"ok": False, "error": "enabled must be true or false"}),
        }
    state = load_state()
    state["auto_watch_weekends_enabled"] = enabled
    if enabled:
        _auto_watch_upcoming_weekends(state)
    save_state(state)
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({"ok": True, "auto_watch_weekends_enabled": enabled}),
    }


_INTERVAL_15S  = round(15 / 3600, 6)   # 0.004167 hr
_INTERVAL_1MIN = round(1  / 60,   6)   # 0.016667 hr
_INTERVAL_5MIN = round(5  / 60,   6)   # 0.083333 hr
ALLOWED_SCAN_INTERVALS = (_INTERVAL_15S, _INTERVAL_1MIN, _INTERVAL_5MIN, 0.25, 0.5, 1.0, 2.0, 3.0)


def _should_run_scheduled_tick(state: dict, now: datetime) -> bool:
    """Decide whether a 15-min EventBridge tick should actually run a probe."""
    interval = float(state.get("scan_interval_hours") or 1.0)
    minute = now.minute
    if interval <= 0.25:
        return True
    if interval == 0.5:
        return minute in (0, 30)
    if minute != 0:
        return False
    if interval == 1.0:
        return True
    if interval == 2.0:
        return now.hour % 2 == 0
    if interval == 3.0:
        return now.hour % 3 == 0
    return True


def _scheduled_probe_delay_seconds(interval_hours: float) -> int:
    return max(1, min(round(interval_hours * 3600), SQS_DELAY_MAX_SECONDS))


def _clear_queued_scheduled_probe(state: dict) -> None:
    state["queued_scheduled_probe_at"] = None
    state["queued_scheduled_probe_token"] = None


def _queued_scheduled_probe_is_current(state: dict, interval_hours: float) -> bool:
    token = state.get("queued_scheduled_probe_token")
    queued_at = _parse_utc_iso(state.get("queued_scheduled_probe_at"))
    if not token or not queued_at:
        return False
    delay = _scheduled_probe_delay_seconds(interval_hours)
    stale_after = max(delay * 2, SQS_STALE_GRACE_SECONDS)
    return queued_at >= datetime.now(tz=timezone.utc) - timedelta(seconds=stale_after)


def _queue_next_scheduled_probe(interval_hours: float, *, state: dict | None = None, force: bool = False) -> bool:
    if interval_hours >= 0.25 or not _is_within_scan_window():
        if state is not None and force:
            _clear_queued_scheduled_probe(state)
            save_state(state)
        return False

    state = state or load_state()
    if not force and _queued_scheduled_probe_is_current(state, interval_hours):
        print(f"Scheduled probe already queued for {state.get('queued_scheduled_probe_at')}.")
        return True
    if not _scheduled_scan_targets(state)[0]:
        _clear_queued_scheduled_probe(state)
        save_state(state)
        print("No future watched or auto-book slots; not queueing scheduled probe.")
        return False

    delay_seconds = _scheduled_probe_delay_seconds(interval_hours)
    token = str(time.time_ns())
    queued_at = (
        datetime.now(tz=timezone.utc) + timedelta(seconds=delay_seconds)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    state["queued_scheduled_probe_at"] = queued_at
    state["queued_scheduled_probe_token"] = token
    save_state(state)

    if _enqueue_work("scheduled_probe", {"token": token}, delay_seconds=delay_seconds):
        return True

    latest = load_state()
    if latest.get("queued_scheduled_probe_token") == token:
        _clear_queued_scheduled_probe(latest)
        save_state(latest)
    return False


def _run_queued_scheduled_probe(token: str) -> None:
    state = load_state()
    if not token or token != state.get("queued_scheduled_probe_token"):
        print("Skipping stale scheduled probe queue item.")
        return

    interval = float(state.get("scan_interval_hours") or 1.0)
    if interval >= 0.25 or not _is_within_scan_window():
        _clear_queued_scheduled_probe(state)
        save_state(state)
        print(f"Cleared queued scheduled probe; interval={interval} hr or outside scan window.")
        return

    _run_scheduled_worker()

    state = load_state()
    if token != state.get("queued_scheduled_probe_token"):
        print("Scheduled probe changed while this item was running; not queueing another.")
        return

    interval = float(state.get("scan_interval_hours") or 1.0)
    if interval < 0.25:
        _queue_next_scheduled_probe(interval, state=state, force=True)
    else:
        _clear_queued_scheduled_probe(state)
        save_state(state)


def _run_release_probe_session() -> None:
    """Single Lambda invocation that owns the 7:58–8:02 AM slot-release window.

    Phases:
      pre  (7:58–8:00):   probe all watched/auto-book targets every 15 s
      burst (8:00–8:00:30): back-to-back probes for the new weekend day's 9:00 AM slot only;
                            exits early once that slot is booked or already in my_reservations
      post (8:00:30–8:02): probe all watched/auto-book targets every 15 s

    All probes share one JWT obtained at session start.
    Results are appended to state["release_probe_log"] for auditing.
    """
    import time as _time

    now_pt = datetime.now(tz=PT)
    eight_am = now_pt.replace(hour=8, minute=0, second=0, microsecond=0)

    # Burst target: new day's 9:00 AM slot, but only on weekends.
    new_day = now_pt.date() + timedelta(days=14)
    burst_target: tuple[str, str] | None = (
        (new_day.isoformat(), "9:00 AM") if new_day.weekday() >= 5 else None
    )
    burst_done = False  # flipped when slot is booked or already reserved

    # Login once upfront, using cached token if available.
    state = load_state()
    try:
        jwt = _get_cached_jwt(state) or _firebase_login()
        if not state.get("cached_jwt"):
            _cache_jwt(state, jwt)
            save_state(state)
        print(f"Release probe session: logged in, JWT expires {state.get('cached_jwt_expires_at', '?')}")
        if burst_target:
            print(f"  Burst target: {burst_target[0]} {burst_target[1]}")
    except Exception as exc:
        try:
            send_telegram(f"❌ Release probe login failed: {exc}")
        except Exception:
            pass
        return

    # Acquire scan lock so regular EventBridge scans yield.
    state = load_state()
    if _active_scan_started_at(state):
        print("Release probe session: another scan in progress, skipping.")
        return
    started_at = _utc_now_iso()
    state["scan_started_at"] = started_at
    state["scan_started_kind"] = "release_probe"
    save_state(state)

    probe_log: list[dict] = []

    def _one_probe(phase: str, targets_override: dict | None = None) -> list[dict]:
        """Run one availability+booking probe. Returns list of booked slots."""
        ts = _utc_now_iso()
        st = load_state()
        auto_book_slots = st.get("auto_book_slots") or []
        targets = targets_override if targets_override is not None else _watched_and_auto_book_targets(st)
        if not targets:
            probe_log.append({"ts": ts, "phase": phase, "result": "no_targets"})
            print(f"  [{phase}] no targets")
            return []
        try:
            new_avail, booked = _api_scan(
                target_times_by_date=targets,
                auto_book_slots=auto_book_slots,
                jwt=jwt,
            )
            st = load_state()
            avail = st.get("availability", {})
            for d, tm in new_avail.items():
                day = avail.get(d, {})
                for t, courts in tm.items():
                    day[t] = _normalize_time_availability(courts)
                avail[d] = day
            st["availability"] = avail
            open_slots = [
                f"{d} {t}"
                for d, tm in new_avail.items()
                for t, courts in tm.items()
                if any(v is True for v in courts.values())
            ]
            entry: dict = {
                "ts": ts,
                "phase": phase,
                "result": "booked" if booked else ("open" if open_slots else "empty"),
            }
            if booked:
                entry["booked"] = [f"{b['date']} {b['time']} Court {b['court']}" for b in booked]
            if open_slots:
                entry["open"] = open_slots
            probe_log.append(entry)
            print(
                f"  [{phase}] {entry['result']}"
                + (f" — {entry.get('booked') or entry.get('open')}" if entry["result"] != "empty" else "")
            )
            _apply_booked_slots(st, booked)
            save_state(st)
            _notify_booked_slots(booked)
            return booked
        except Exception as exc:
            probe_log.append({"ts": ts, "phase": phase, "result": "error", "error": str(exc)})
            print(f"  [{phase}] error: {exc}")
            return []

    def _burst_slot_already_reserved(burst_date: str, burst_time: str) -> bool:
        """True if my_reservations already contains this slot (booked now or previously)."""
        st = load_state()
        return any(
            r.get("date") == burst_date and r.get("time") == burst_time
            for r in (st.get("my_reservations") or [])
        )

    try:
        while True:
            now = datetime.now(tz=PT)
            secs = (now - eight_am).total_seconds()

            if secs >= _RELEASE_END_S:
                break

            in_burst_window = 0 <= secs < _RELEASE_BURST_UNTIL_S

            if in_burst_window and burst_target and not burst_done:
                burst_date, burst_time = burst_target
                if _burst_slot_already_reserved(burst_date, burst_time):
                    burst_done = True
                    probe_log.append({
                        "ts": _utc_now_iso(), "phase": "burst",
                        "result": "already_reserved",
                        "slot": f"{burst_date} {burst_time}",
                    })
                    print(f"  [burst] {burst_date} {burst_time} already reserved — stopping burst")
                    # Fall through to a post-interval probe below
                else:
                    booked = _one_probe("burst", targets_override={burst_date: [burst_time]})
                    if any(b.get("date") == burst_date and b.get("time") == burst_time for b in booked):
                        burst_done = True
                    continue  # back-to-back: no sleep, loop immediately

            # Pre / post phase (also covers burst window once burst is done or N/A)
            phase = "pre" if secs < 0 else "post"
            interval = _RELEASE_PRE_INTERVAL_S if secs < 0 else _RELEASE_POST_INTERVAL_S
            t0 = _time.monotonic()
            _one_probe(phase)
            elapsed = _time.monotonic() - t0
            _time.sleep(max(0.0, interval - elapsed))

    finally:
        state = load_state()
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
            state.pop("scan_started_kind", None)
        existing = state.get("release_probe_log") or []
        state["release_probe_log"] = (existing + probe_log)[-500:]
        state["last_release_probe_session"] = started_at
        save_state(state)
        n = len(probe_log)
        booked_all = [b for e in probe_log for b in (e.get("booked") or [])]
        open_all   = sorted({s for e in probe_log for s in (e.get("open") or [])})
        errors     = sum(1 for e in probe_log if e.get("result") == "error")
        lines = [f"8am session done: {n} probes"]
        if booked_all:
            lines.append("✅ Booked: " + ", ".join(booked_all))
        elif open_all:
            lines.append("⚠️ Saw open but not booked: " + ", ".join(open_all))
        else:
            lines.append("No openings found")
        if errors:
            lines.append(f"⚠️ {errors} probe error(s)")
        summary = "\n".join(lines)
        print(summary)
        try:
            send_telegram(summary)
        except Exception:
            pass


def _queue_release_probe_session_if_needed(state: dict, now_pt: datetime | None = None) -> bool:
    """Queue the release probe session from the 7:45 AM EventBridge tick (780 s delay → ~7:58 AM)."""
    now_pt = now_pt or datetime.now(tz=PT)
    if now_pt.hour != 7 or now_pt.minute != 45:
        return False
    if not WORK_QUEUE_URL:
        print("Work queue not configured; cannot queue release probe session.")
        return False
    today_str = now_pt.date().isoformat()
    if state.get("release_probe_session_date") == today_str:
        print(f"Release probe session already queued for {today_str}.")
        return True
    if not _watched_and_auto_book_targets(state):
        print("No watched/auto-book slots — skipping release probe session.")
        return False
    state["release_probe_session_date"] = today_str
    save_state(state)
    queued = bool(_enqueue_work("release_probe_session", {}, delay_seconds=780))
    if queued:
        print(f"Queued release probe session for ~7:58 AM PT ({today_str}).")
    return queued


def _run_queue_work(message: dict) -> None:
    kind = message.get("kind")
    if kind == "scheduled_probe":
        _run_queued_scheduled_probe(str(message.get("token") or ""))
    elif kind == "release_probe_session":
        _run_release_probe_session()
    else:
        print(f"Ignoring unknown queue work kind: {kind}")


def _is_sqs_event(event: dict) -> bool:
    records = (event or {}).get("Records") or []
    return bool(records) and all(record.get("eventSource") == "aws:sqs" for record in records)


def _handle_queue_event(event: dict) -> None:
    for record in event.get("Records") or []:
        try:
            message = json.loads(record.get("body") or "{}")
        except json.JSONDecodeError:
            print("Ignoring queue item with invalid JSON body.")
            continue
        _run_queue_work(message)


def handle_scan_interval(event) -> dict:
    body = get_body(event)
    try:
        requested = float(body.get("scan_interval_hours"))
    except (TypeError, ValueError):
        requested = None
    if requested not in ALLOWED_SCAN_INTERVALS:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "ok": False,
                "error": "scan_interval_hours must be one of "
                         + ", ".join(str(v) for v in ALLOWED_SCAN_INTERVALS),
            }),
        }
    state = load_state()
    state["scan_interval_hours"] = requested
    _clear_queued_scheduled_probe(state)
    if requested < 0.25:
        queued = _queue_next_scheduled_probe(requested, state=state, force=True)
        state = load_state()
    else:
        save_state(state)
        queued = False
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({
            "ok": True,
            "scan_interval_hours": requested,
            "queued_scheduled_probe_at": state.get("queued_scheduled_probe_at"),
            "scheduled_probe_queued": queued,
        }),
    }


# ── Targeted daily probe (diagnostic) ────────────────────────────────────────

def _run_targeted_daily_scan() -> None:
    """Probe scan: weekends at 9 AM plus the newly published day for all times."""
    state = load_state()
    active_scan = _active_scan_started_at(state)
    if active_scan:
        print(f"Targeted daily scan: release probe session running since {active_scan.isoformat()}, skipping.")
        return
    if _auto_watch_upcoming_weekends(state):
        save_state(state)

    today = date.today()
    new_day = today + timedelta(days=14)
    times_by_date: dict[str, list[str]] = {new_day.isoformat(): SLOT_TIMES[:]}

    for day_offset in range(16):
        target = today + timedelta(days=day_offset)
        if target.weekday() >= 5:
            times_by_date.setdefault(target.isoformat(), ["9:00 AM"])

    auto_book_slots = state.get("auto_book_slots") or []
    for date_str, times in _auto_book_time_map(auto_book_slots).items():
        existing = set(times_by_date.get(date_str, []))
        existing.update(times)
        times_by_date[date_str] = [t for t in SLOT_TIMES if t in existing]

    history_targets = _history_targets_from_map(times_by_date)
    started_at = _utc_now_iso()

    t0 = time.monotonic()
    print(
        "Targeted probe: "
        + ", ".join(f"{d} for {times_by_date[d]}" for d in sorted(times_by_date))
        + "…"
    )
    try:
        new_avail, booked_slots = _api_scan(
            target_times_by_date=times_by_date,
            auto_book_slots=auto_book_slots,
        )
        elapsed = time.monotonic() - t0

        for date_str in sorted(times_by_date):
            day_avail = new_avail.get(date_str, {})
            print(f"  {date_str}:")
            for time_text in times_by_date[date_str]:
                court_avail = day_avail.get(time_text, {})
                open_courts = [c for c, v in court_avail.items() if v is True]
                null_courts = [c for c, v in court_avail.items() if v is None]
                closed_courts = [c for c, v in court_avail.items() if v is False]
                print(f"    {time_text}: open={open_courts} null={null_courts} closed={closed_courts}")

        print(f"Targeted probe finished in {elapsed:.1f}s.")

        state = load_state()
        state["last_scan_started_at"] = started_at
        state["last_scanned"] = _utc_now_iso()
        state["last_scan_kind"] = "probe"
        _record_scan_history(
            state,
            kind="probe",
            started_at=started_at,
            completed_at=state["last_scanned"],
            status="completed",
            targets=_attach_history_results(history_targets, new_avail),
        )
        open_target_lines = _alert_lines_for_open_targets(state, new_avail)
        availability = state.get("availability", {})
        for date_str, day_avail in new_avail.items():
            day_state = availability.get(date_str, {})
            for time_text, court_avail in day_avail.items():
                day_state[time_text] = _normalize_time_availability(court_avail)
            availability[date_str] = day_state
        state["availability"] = availability
        sync_rec_my_reservations(state)
        _apply_booked_slots(state, booked_slots)
        save_state(state)
        _notify_booked_slots(booked_slots)
        if open_target_lines:
            msg = "Pickleball slot(s) now available:\n" + "\n".join(open_target_lines)
            notify(msg)
        else:
            print("No open watched or auto-book slots in this scan.")
    except Exception as exc:
        elapsed = time.monotonic() - t0
        state = load_state()
        _record_scan_history(
            state,
            kind="probe",
            started_at=started_at,
            completed_at=_utc_now_iso(),
            status="failed",
            targets=history_targets,
            error=str(exc),
        )
        save_state(state)
        print(f"Targeted probe failed after {elapsed:.1f}s: {exc}")
        raise


# ── Lambda handler ────────────────────────────────────────────────────────────

def handler(event, context):
    if _is_sqs_event(event):
        _handle_queue_event(event)
        return

    # Internal: targeted daily probe (8 AM & 9 AM, 14 days out)
    if event.get("_targeted_daily_scan"):
        _run_targeted_daily_scan()
        return

    # Internal: direct scheduled invocation (for testing)
    if event.get("_scheduled"):
        _run_full_refresh_worker(force=True)
        return

    # EventBridge tick (fires every 15 min); SQS handles sub-15-min follow-up probes.
    if event.get("source") == "aws.events":
        state = load_state()
        interval = float(state.get("scan_interval_hours") or 1.0)
        _queue_release_probe_session_if_needed(state)
        state = load_state()
        interval = float(state.get("scan_interval_hours") or 1.0)
        if not _should_run_scheduled_tick(state, datetime.now(tz=timezone.utc)):
            print(f"Skipping tick: interval={interval} hr.")
            return
        if interval < 0.25:
            state = load_state()
            if _queued_scheduled_probe_is_current(state, interval):
                print(f"Skipping tick: scheduled probe already queued for {state.get('queued_scheduled_probe_at')}.")
                return
            _run_scheduled_worker()
            state = load_state()
            _queue_next_scheduled_probe(float(state.get("scan_interval_hours") or 1.0), state=state, force=True)
        else:
            if state.get("queued_scheduled_probe_token"):
                _clear_queued_scheduled_probe(state)
                save_state(state)
            _run_targeted_daily_scan()
        return

    method = get_method(event)
    path   = get_path(event)

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    if path == "/telegram" and method == "POST":
        return handle_telegram(event)

    if not _check_auth(event):
        return {
            "statusCode": 401,
            "headers": {**CORS_HEADERS, "WWW-Authenticate": 'Basic realm="pickleball"'},
            "body": json.dumps({"error": "Unauthorized"}),
        }

    if path == "/state" and method == "GET":
        return handle_state(event)

    if path == "/watch" and method == "PUT":
        return handle_watch(event)

    if path == "/my-reservations" and method == "GET":
        return handle_my_reservations_refresh(event)

    if path == "/my-reservations" and method == "PUT":
        return handle_my_reservations(event)

    if path == "/scan-interval" and method == "PUT":
        return handle_scan_interval(event)

    if path == "/auto-watch-weekends" and method == "PUT":
        return handle_auto_watch_weekends(event)

    if path == "/focus-newest-weekend" and method == "PUT":
        return handle_focus_newest_weekend(event)

    if path == "/auto-book" and method == "PUT":
        return handle_auto_book(event)

    if path == "/force-scan" and method == "POST":
        _run_full_refresh_worker(force=True)
        return handle_state(event)

    if path in ("/scan", "/prod/scan"):
        params = parse_query_params(event)
        mode   = (params.get("mode") or "sync").strip().lower()

        if mode != "sync":
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Async scans have been removed; use mode=sync."}),
            }
        if not _is_lambda_url_request(event):
            return _build_sync_redirect(event)
        try:
            targets     = parse_requested_dates(event)
            target_time = parse_time_filter(event, default=None)
        except ValueError as exc:
            return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": str(exc)})}
        state = load_state()
        auto_book_slots = state.get("auto_book_slots") or []
        booked_slots = []
        auto_book_avail = {}
        auto_book_targets = _auto_book_time_map(auto_book_slots)
        if auto_book_targets:
            auto_book_avail, booked_slots = _api_scan(
                target_times_by_date=auto_book_targets,
                auto_book_slots=auto_book_slots,
            )
        if auto_book_avail or booked_slots:
            state = load_state()
            if auto_book_avail:
                availability = state.get("availability", {})
                for date_str, day_avail in auto_book_avail.items():
                    day_state = availability.get(date_str, {})
                    for time_text, court_avail in day_avail.items():
                        day_state[time_text] = _normalize_time_availability(court_avail)
                    availability[date_str] = day_state
                state["availability"] = availability
            if booked_slots:
                _apply_booked_slots(state, booked_slots)
            save_state(state)
        if booked_slots:
            _notify_booked_slots(booked_slots)
        results = asyncio.run(main(targets=targets, target_time=target_time))
        if booked_slots:
            _mark_booked_slots_in_scan_results(
                targets=targets,
                results=results,
                booked_slots=booked_slots,
            )
        payload = build_scan_payload(targets=targets, target_time=target_time, results=results)
        payload["booked_slots"] = booked_slots
        state = load_state()
        sync_rec_my_reservations(state)
        save_state(state)
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps(payload)}

    return {"statusCode": 404, "headers": CORS_HEADERS, "body": json.dumps({"error": "Not found"})}


if __name__ == "__main__":
    targets = load_check_dates()
    results = asyncio.run(main(targets=targets, target_time=DEFAULT_TIME_FILTER))
    if results:
        lines = [
            f"{day} {s['time']} -> {', '.join(s['courts']) or '(could not read courts)'}"
            for day, slots in results.items()
            for s in slots
        ]
        msg = f"Pickleball {DEFAULT_TIME_FILTER} available:\n" + "\n".join(lines)
        notify(msg)
