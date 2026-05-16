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
APP_VERSION = "2.1.0"
SCAN_HISTORY_MAX = 300  # safety cap; UI filters to past 24h, normalize trims older.

# ── Credentials ───────────────────────────────────────────────────────────────
EMAIL    = os.environ["REC_US_LOGIN"]
PASSWORD = os.environ["REC_US_PASSWORD"]
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


BASE_URL      = load_config()["base_url"]
SYNC_SCAN_URL = load_config().get("sync_scan_url", "")
HEADLESS      = True
DEFAULT_TIME_FILTER = "8:00 AM"
NOTIFY_NUMBER = "+14154380400"
STATE_BUCKET  = os.environ.get("STATE_BUCKET", "")
STATE_KEY           = "state.json"
TELEGRAM_USAGE_KEY  = "telegram_usage.json"
TELEGRAM_USAGE_MAX  = 100
SCAN_LOCK_TTL_SECONDS = 20 * 60
SYNC_TOKEN_TTL_SECONDS = 300
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
PARTICIPANT_USER_ID = "06ba5791-1e5b-45f3-8910-1b9c75d020cc"

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
        "pending_partial_rescan_dates": [],
        "pending_partial_rescan_at": None,
        "my_reservations":     [],   # [{"date": "YYYY-MM-DD", "time": "H:MM AM", "court": "6"}]
        "availability":        {},   # {"YYYY-MM-DD": {"H:MM AM": {"6": true, "4": false, "5": true}}}
        "notified_slots":      [],   # ["YYYY-MM-DD|H:MM AM|6"] — already SMS'd, avoids repeat texts
        "last_scanned":        None,
        "last_scan_started_at": None,
        "last_scan_kind":      None,
        "recent_scan_history": [],
        "pending_full_scan":   False,
        "scan_started_kind":   None,
        "scan_interval_hours": 1.0,
        "auto_watched_weekends": [],  # ISO date strings already auto-watched; user removals are respected
        "auto_watch_weekends_enabled": True,
        "focus_newest_weekend": False, # when True, only scan the latest weekend day (skip older ones)
        "auto_book_slots":     [],   # [{"date": "YYYY-MM-DD", "time": "H:MM AM"}] — slots to auto-book
        "seen_open_days":      [],   # ISO dates where we first observed ≥1 open slot
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
    normalized["pending_partial_rescan_dates"] = sorted(
        d for d in (state.get("pending_partial_rescan_dates") or [])
        if isinstance(d, str)
    )
    normalized["pending_partial_rescan_at"] = state.get("pending_partial_rescan_at")
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
    normalized["pending_full_scan"] = bool(state.get("pending_full_scan"))
    normalized["scan_interval_hours"] = state.get("scan_interval_hours", 1.0)
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


def _detect_missed_dates(avail: dict) -> list[str]:
    """Return dates where any focused-policy time slot has all-None court availability."""
    today_str = date.today().isoformat()
    return [
        d for d, day_avail in avail.items()
        if d >= today_str and any(
            all(v is None for v in court_avail.values())
            for time_key, court_avail in day_avail.items()
            if _matches_focused_scan_policy(d, time_key)
        )
    ]


def _invoke_rescan_lambda(date_strs: list[str], *, delay_seconds: int = 60) -> None:
    lambda_client = boto3.client("lambda", region_name="us-west-2")
    lambda_client.invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({"_rescan_dates": date_strs, "_delay_seconds": delay_seconds}).encode(),
    )


# ── Login ─────────────────────────────────────────────────────────────────────

async def login(page, context=None) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)

    # If a cached session was loaded, check if we're already authenticated
    if os.path.exists(_SESSION_FILE):
        already_in = await page.get_by_role("button", name="Log In").count() == 0
        if already_in:
            print("Session restored from cache — skipping login.\n")
            return
        print("Cached session expired — logging in…")
    else:
        print("Logging in…")

    await page.wait_for_timeout(2_000)
    await page.get_by_role("button", name="Log In").first.click()
    await page.wait_for_timeout(500)
    await page.get_by_role("button", name="Log in with your email").click(timeout=10_000)
    await page.wait_for_timeout(500)
    await page.get_by_test_id("email").fill(EMAIL)
    await page.get_by_role("button", name="Click here to log in with your password").click(timeout=8_000)
    await page.wait_for_timeout(500)
    await page.get_by_test_id("email").fill(EMAIL)
    await page.get_by_test_id("password").fill(PASSWORD)
    await page.get_by_role("button", name="Log in & continue").click(timeout=8_000)
    await page.wait_for_load_state("domcontentloaded", timeout=20_000)
    await page.wait_for_timeout(1_000)

    if context is not None:
        await context.storage_state(path=_SESSION_FILE)
        print("Session saved to cache.\n")
    else:
        print("Logged in.\n")


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


async def book_slot_api(page, target_date: date, time_text: str, court: str) -> bool:
    """Book a court via the rec.us API and complete checkout using account credits.

    Reuses the already-logged-in Playwright page (same session — no second login).
    Returns True if the booking is confirmed (checkout successful).
    """
    time_str = _TIME_TEXT_TO_HHMMSS.get(time_text)
    if not time_str:
        print(f"  book_slot_api: unknown time_text '{time_text}'")
        return False

    court_sport_id = COURT_SPORT_IDS.get(court)
    if not court_sport_id:
        print(f"  book_slot_api: unknown court '{court}'")
        return False

    # Extract JWT from the active browser session
    cookies = await page.context.cookies()
    jwt = next((c["value"] for c in cookies if c["name"] == "access_token"), "")
    if not jwt:
        print("  book_slot_api: no access_token cookie found")
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
    print(f"  Order {order_id[:8]} | ${total/100:.2f} | credit available: ${max_credit/100:.2f}")

    # Navigate to checkout in the same session (no second login needed)
    checkout_url = f"https://www.rec.us/app/orders/{order_id}/checkout"
    await page.goto(checkout_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(7_000)

    # Apply eligible credits via the checkbox if credits are available
    if max_credit > 0:
        credit_cb = page.locator('[data-component="account-credit"] button[role="checkbox"]')
        if await credit_cb.count() > 0:
            state = await credit_cb.first.get_attribute("data-state")
            if state != "checked":
                await credit_cb.first.click(timeout=3_000)
                await page.wait_for_timeout(3_000)

    # Click "Confirm" (appears when total=$0 after credits) or "Continue to Payment"
    for btn_text in ("Confirm", "Continue to Payment"):
        btn = page.locator("button", has_text=btn_text)
        if await btn.count() > 0 and await btn.first.get_attribute("disabled") is None:
            await btn.first.click(timeout=5_000)
            await page.wait_for_timeout(6_000)
            break

    # Verify success via page text (most reliable — order API may lag)
    body_text = await page.inner_text("body")
    if "Checkout Successful" in body_text:
        print(f"  Checkout Successful! Court {court} {date_str} {time_text} confirmed.")
        return True

    # Fallback: check order status via API
    s2, b2 = _rec_api(f"https://api.rec.us/v1/orders/{order_id}", jwt=jwt)
    od2 = b2.get("order", b2.get("data", b2))
    status2 = od2.get("status", "") if isinstance(od2, dict) else ""
    if status2 == "complete":
        print(f"  Order complete. Court {court} {date_str} {time_text} confirmed.")
        return True

    print(f"  Checkout not confirmed. Page: {body_text[:200]}")
    return False


async def book_slot(page, target_date: date, time_text: str, court: str) -> bool:
    """Navigate to the slot, open the modal, select the court, and confirm booking.

    Returns True if the booking succeeded, False otherwise.
    The caller is responsible for navigating to the correct date first.
    """
    print(f"  Attempting to book {target_date.isoformat()} {time_text} Court {court}…")
    await select_date(page, target_date)
    visible_times = set(await collect_all_slot_buttons(page))
    if time_text not in visible_times:
        print(f"  Time slot {time_text} not visible — possibly already taken.")
        return False

    btn = page.locator("button").filter(has_text=re.compile(re.escape(time_text)))
    try:
        await btn.first.click(timeout=4_000)
    except PwTimeout:
        print(f"  Could not click time slot button for {time_text}.")
        return False

    try:
        dialog = page.locator('[role="dialog"]').first
        await dialog.wait_for(timeout=5_000)
        await page.wait_for_timeout(300)
    except PwTimeout:
        print(f"  Dialog did not appear for {time_text}.")
        await page.keyboard.press("Escape")
        return False

    # Select the court from the combobox dropdown
    court_combo = dialog.locator('button[role="combobox"]').last
    selected_court = False
    if await court_combo.count() > 0:
        try:
            await court_combo.click(timeout=2_000)
            await page.wait_for_timeout(250)
            options = page.locator('[role="option"]')
            option_count = await options.count()
            for i in range(option_count):
                opt = options.nth(i)
                text = (await opt.inner_text()).strip()
                match = COURT_RE.match(text)
                if match and _normalize_court_number(match.group(1)) == court:
                    await opt.click(timeout=2_000)
                    await page.wait_for_timeout(300)
                    selected_court = True
                    break
            if not selected_court:
                # Court not in dropdown options — not available
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(150)
                print(f"  Court {court} not available in dropdown for {time_text}.")
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
                return False
        except PwTimeout:
            print(f"  Timeout interacting with court combobox for {time_text}.")
            await page.keyboard.press("Escape")
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
            return False

    # Click the Reserve / Book button in the dialog
    reserve_btn = dialog.locator(
        'button:text-is("Reserve"), button:text-is("Book"), '
        'button:text-is("Reserve Now"), button:text-is("Book Now"), '
        'button[type="submit"]'
    ).first
    try:
        await reserve_btn.wait_for(timeout=4_000)
        await reserve_btn.click(timeout=4_000)
        await page.wait_for_timeout(1_500)
    except PwTimeout:
        print(f"  Could not find or click Reserve button for {time_text} Court {court}.")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        return False

    # Check for a confirmation dialog / success indicator
    # rec.us may show a confirmation step — look for a "Confirm" button or success message
    try:
        confirm_btn = page.locator(
            'button:text-is("Confirm"), button:text-is("Confirm Booking"), '
            'button:text-is("Complete Booking"), button:text-is("Yes")'
        ).first
        if await confirm_btn.count() > 0:
            await confirm_btn.click(timeout=4_000)
            await page.wait_for_timeout(1_500)
    except PwTimeout:
        pass

    # Dismiss any remaining dialog
    try:
        close = page.locator(
            '[role="dialog"] button[aria-label*="close" i], '
            '[role="dialog"] button[aria-label*="dismiss" i], '
            '[role="dialog"] button:text-is("Done"), '
            '[role="dialog"] button:text-is("Close")'
        ).first
        if await close.count() > 0:
            await close.click(timeout=3_000)
    except PwTimeout:
        await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)

    print(f"  Booked {target_date.isoformat()} {time_text} Court {court}.")
    return True


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


async def scan_day_quick(
    page,
    target: date,
    target_times: list[str] | None = None,
) -> dict[str, dict[str, bool | None]]:
    """Build state booleans from the same slot scan path used by /scan."""
    if not target_times:
        slots = await scan_day(page, target, target_time=None)
        return _slots_to_availability(slots)

    requested_times = []
    seen_times: set[str] = set()
    for time_text in target_times:
        if time_text not in SLOT_TIMES or time_text in seen_times:
            continue
        seen_times.add(time_text)
        requested_times.append(time_text)

    if not requested_times:
        return {}

    label = target.strftime("%a %b %-d")
    print(f"  {label}… ", end="", flush=True)
    await select_date(page, target)
    visible_times = set(await collect_all_slot_buttons(page, stop_when_found=set(requested_times)))
    results: dict[str, dict[str, bool | None]] = {}

    for time_text in requested_times:
        slot_t0 = time.monotonic()
        if time_text not in visible_times:
            results[time_text] = _empty_court_availability(False)
            slot_elapsed = time.monotonic() - slot_t0
            print(f"    {time_text} scanned in {slot_elapsed:.1f}s -> not visible")
            continue
        btn = page.locator("button").filter(has_text=re.compile(re.escape(time_text)))
        try:
            await btn.first.click(timeout=4_000)
        except PwTimeout:
            results[time_text] = _empty_court_availability(None)
            slot_elapsed = time.monotonic() - slot_t0
            print(f"    {time_text} scanned in {slot_elapsed:.1f}s -> click timeout")
            continue
        court_availability = await get_courts_from_modal(page)
        results[time_text] = (
            _normalize_time_availability(court_availability)
            if any(court_availability.values())
            else _empty_court_availability(None)
        )
        slot_elapsed = time.monotonic() - slot_t0
        open_courts = [c for c, v in results[time_text].items() if v is True]
        null_courts = [c for c, v in results[time_text].items() if v is None]
        closed_courts = [c for c, v in results[time_text].items() if v is False]
        print(
            f"    {time_text} scanned in {slot_elapsed:.1f}s -> "
            f"open={open_courts} null={null_courts} closed={closed_courts}"
        )

    print(f"{len(results)} watched slot(s): {', '.join(requested_times)}")
    return results


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

    # `watched` tracks specifically requested (date, time) pairs so we can seed them
    # all-False (confirmed booked) even if absent from the API response.
    watched: dict[str, set[str]] | None = (
        {
            d: set(SLOT_TIMES) if date.fromisoformat(d).weekday() >= 5 else set(times)
            for d, times in target_times_by_date.items()
        }
        if target_times_by_date is not None
        else None
    )

    # Seed watched slots as all-False; API will flip open ones to True below.
    result: dict[str, dict[str, dict[str, bool | None]]] = {}
    if watched is not None:
        for date_str, times in watched.items():
            result[date_str] = {
                t: {c: False for c in TARGET_COURTS}
                for t in times
                if t in SLOT_TIMES
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

    # Only launch Playwright when there is actually something to book
    import asyncio as _asyncio

    async def _do_book():
        async with async_playwright() as pw:
            browser, context, page = await _new_page(pw)
            await login(page, context)
            for date_str, time_text, court_avail in to_book:
                for court in COURT_PREFERENCE:
                    if court_avail.get(court) is not True:
                        continue
                    try:
                        success = await book_slot_api(page, date.fromisoformat(date_str), time_text, court)
                    except Exception as exc:
                        print(f"  Booking error {date_str} {time_text} Court {court}: {exc}")
                        success = False
                    if success:
                        booked.append({"date": date_str, "time": time_text, "court": court})
                        for c in new_avail.get(date_str, {}).get(time_text, {}):
                            new_avail[date_str][time_text][c] = False
                        break
            await browser.close()

    _asyncio.run(_do_book())
    return new_avail, booked


# ── Quick scan (for scheduled worker) ────────────────────────────────────────

async def _scan_dates_quick(
    targets: list[date],
    target_times_by_date: dict[str, list[str]] | None = None,
    auto_book_slots: list[dict] | None = None,
) -> tuple[dict[str, dict[str, dict[str, bool | None]]], list[dict]]:
    """Returns (availability, booked_slots).

    availability: {date_iso: {time: {court: available}}}
    booked_slots: list of {date, time, court} that were successfully booked this session.
    """
    today_str = date.today().isoformat()
    # Build a lookup: {(date_iso, time_text)} for quick membership check
    auto_book_set: set[tuple[str, str]] = set()
    if auto_book_slots:
        for ab in auto_book_slots:
            ab_date = ab.get("date", "")
            ab_time = ab.get("time", "")
            if ab_date >= today_str and ab_time in SLOT_TIMES:
                auto_book_set.add((ab_date, ab_time))

    async with async_playwright() as pw:
        browser, context, page = await _new_page(pw)
        logged_in = False
        if auto_book_set:
            await login(page, context)
            logged_in = True
        else:
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
        results: dict[str, dict[str, dict[str, bool | None]]] = {}
        booked: list[dict] = []

        for target in targets:
            date_iso = target.isoformat()
            print(f"  Quick scan {date_iso}…")
            try:
                target_times = None
                if target_times_by_date is not None:
                    target_times = target_times_by_date.get(date_iso, [])
                avail = await scan_day_quick(page, target, target_times=target_times)
                results[date_iso] = avail

                # Auto-book: check if any auto-book slot for this date is now available
                for time_text, court_avail in avail.items():
                    if (date_iso, time_text) not in auto_book_set:
                        continue
                    best_court = _preferred_open_court(court_avail)
                    if best_court is None:
                        continue
                    # Attempt to book in preference order
                    for court in COURT_PREFERENCE:
                        if court_avail.get(court) is not True:
                            continue
                        if not logged_in:
                            await login(page, context)
                            logged_in = True
                        try:
                            success = await book_slot(page, target, time_text, court)
                        except Exception as book_exc:
                            print(f"  Booking error for {date_iso} {time_text} Court {court}: {book_exc}")
                            success = False
                        if success:
                            booked.append({"date": date_iso, "time": time_text, "court": court})
                            auto_book_set.discard((date_iso, time_text))  # remove so we don't double-book
                            # Mark court as taken in availability so caller sees the updated state
                            for c in results[date_iso].get(time_text, {}):
                                results[date_iso][time_text][c] = False
                            break
            except Exception as exc:
                print(f"  Error scanning {date_iso}: {exc}")
                fallback_times = (
                    target_times_by_date.get(date_iso, [])
                    if target_times_by_date is not None
                    else SLOT_TIMES
                )
                results[date_iso] = {
                    t: _empty_court_availability(None)
                    for t in fallback_times
                    if t in SLOT_TIMES
                }
        await browser.close()
    return results, booked


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


def _notify_booked_slots(booked_slots: list[dict]) -> None:
    """Send SMS/email notification for each successfully booked slot."""
    if not booked_slots:
        return
    lines = [f"{b['date']} {b['time']} Court {b['court']}" for b in booked_slots]
    msg = "Auto-booked pickleball slot(s):\n" + "\n".join(lines)
    notify(msg, subject="Pickleball auto-booking confirmed")


# ── Scheduled worker ──────────────────────────────────────────────────────────

def _run_rescan_dates(date_strs: list[str], *, delay_seconds: int = 0) -> None:
    """Rescan specific dates (used for missed-slot retry)."""
    if delay_seconds > 0:
        time.sleep(delay_seconds)

    valid_strs = []
    for d_str in date_strs:
        try:
            date.fromisoformat(d_str)
            valid_strs.append(d_str)
        except ValueError:
            continue
    if not valid_strs:
        return
    date_strs = valid_strs
    history_targets = _history_targets_from_map({d_str: SLOT_TIMES[:] for d_str in date_strs})

    state = load_state()
    if _active_scan_started_at(state):
        print("Active scan in progress. Skipping rescan.")
        return

    started_at = _utc_now_iso()
    pending_dates = [
        d for d in (state.get("pending_partial_rescan_dates") or [])
        if d not in set(date_strs)
    ]
    state["pending_partial_rescan_dates"] = pending_dates
    state["pending_partial_rescan_at"] = None if not pending_dates else state.get("pending_partial_rescan_at")
    state["scan_started_at"] = started_at
    state["scan_started_kind"] = "missed_rescan"
    save_state(state)

    print(f"Rescanning {len(targets)} missed date(s): {date_strs}")
    auto_book_slots = state.get("auto_book_slots") or []
    rescan_times = {d_str: SLOT_TIMES[:] for d_str in date_strs}
    try:
        new_avail, booked_slots = _api_scan(
            target_times_by_date=rescan_times,
            auto_book_slots=auto_book_slots,
        )
        state = load_state()
        availability = state.get("availability", {})
        availability.update(new_avail)
        state["availability"] = availability
        state["last_scan_started_at"] = started_at
        state["last_scanned"] = _utc_now_iso()
        state["last_scan_kind"] = "missed_rescan"
        _record_scan_history(
            state,
            kind="missed_rescan",
            started_at=started_at,
            completed_at=state["last_scanned"],
            status="completed",
            targets=_attach_history_results(history_targets, new_avail),
        )
        open_target_lines = _alert_lines_for_open_targets(state, new_avail)
        _apply_booked_slots(state, booked_slots)
        pending_full_scan = bool(state.get("pending_full_scan"))
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
            state.pop("scan_started_kind", None)
        save_state(state)
        _notify_booked_slots(booked_slots)
        if open_target_lines:
            msg = "Pickleball slot(s) now available:\n" + "\n".join(open_target_lines)
            notify(msg)
        else:
            print("No open watched or auto-book slots in this scan.")
        print(f"Rescan complete for: {date_strs}")
        if pending_full_scan:
            state = load_state()
            state["pending_full_scan"] = False
            save_state(state)
            print("Queued full scan will start after partial rescan.")
            _run_full_refresh_worker(force=True)
    except Exception as exc:
        state = load_state()
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
            state.pop("scan_started_kind", None)
            _record_scan_history(
                state,
                kind="missed_rescan",
                started_at=started_at,
                completed_at=_utc_now_iso(),
                status="failed",
                targets=history_targets,
                error=str(exc),
            )
            save_state(state)
        raise


def _new_day_iso(today: date | None = None) -> str:
    return ((today or date.today()) + timedelta(days=14)).isoformat()


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


def _watched_and_auto_book_targets(state: dict) -> dict[str, list[str]]:
    watched_times = _future_watched_time_map(state)

    today_str = date.today().isoformat()
    for ab in (state.get("auto_book_slots") or []):
        ab_date = ab.get("date", "")
        ab_time = ab.get("time", "")
        if not ab_date or not ab_time or ab_date < today_str or ab_time not in SLOT_TIMES:
            continue
        existing = set(watched_times.get(ab_date, []))
        if ab_time not in existing:
            existing.add(ab_time)
            watched_times[ab_date] = [t for t in SLOT_TIMES if t in existing]

    if state.get("focus_newest_weekend"):
        weekend_dates = [d for d in watched_times if date.fromisoformat(d).weekday() >= 5]
        if len(weekend_dates) > 1:
            newest = max(weekend_dates)
            for d in weekend_dates:
                if d != newest:
                    del watched_times[d]

    return watched_times


def _scan_completion_status(state: dict) -> tuple[str, list[str]]:
    incomplete_dates = _detect_missed_dates(state.get("availability", {}))
    if state.get("scan_started_at"):
        return "running", incomplete_dates
    if state.get("pending_partial_rescan_dates"):
        return "partial_rescan_scheduled", incomplete_dates
    if incomplete_dates:
        return "incomplete", incomplete_dates
    return "complete", incomplete_dates


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
    new_day = _new_day_iso(today)
    targets: dict[str, list[str]] = {new_day: SLOT_TIMES[:]}
    for day_offset in range(16):
        target = today + timedelta(days=day_offset)
        if target.weekday() >= 5:
            targets.setdefault(target.isoformat(), ["9:00 AM"])
    return targets


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

    state["pending_full_scan"] = False
    state["pending_partial_rescan_dates"] = []
    state["pending_partial_rescan_at"] = None
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

        _apply_booked_slots(state, booked_slots)
        state["notified_slots"] = []
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
            state.pop("scan_started_kind", None)
        missed_dates = _detect_missed_dates(new_avail)
        state["pending_partial_rescan_dates"] = missed_dates
        state["pending_partial_rescan_at"] = (
            (datetime.now(tz=timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds").replace("+00:00", "Z")
            if missed_dates else None
        )
        save_state(state)
        _notify_booked_slots(booked_slots)

        if missed_dates:
            print(f"Scheduling rescan for {len(missed_dates)} missed date(s) in 60s: {missed_dates}")
            try:
                _invoke_rescan_lambda(missed_dates, delay_seconds=60)
            except Exception as exc:
                print(f"Failed to schedule rescan: {exc}")
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
    state["pending_full_scan"] = False
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
        payload = (
            json.dumps({"_rescan_dates": date_strs}).encode()
            if date_strs
            else json.dumps({"_scheduled": True}).encode()
        )
        boto3.client("lambda", region_name="us-west-2").invoke(
            FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
            InvocationType="Event",
            Payload=payload,
        )
        return {"ok": True, "message": "Scan started"}
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
    completion_status, incomplete_dates = _scan_completion_status(state)

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
            "pending_partial_rescan_dates": state.get("pending_partial_rescan_dates", []),
            "pending_partial_rescan_at": state.get("pending_partial_rescan_at"),
            "incomplete_dates":     incomplete_dates,
            "scan_completion_status": completion_status,
            "scan_started_at":     state.get("scan_started_at"),
            "scan_started_kind":   state.get("scan_started_kind"),
            "last_scanned":        state.get("last_scanned"),
            "last_scan_started_at": state.get("last_scan_started_at"),
            "last_scan_kind":      state.get("last_scan_kind"),
            "recent_scan_history": state.get("recent_scan_history", []),
            "pending_full_scan":   bool(state.get("pending_full_scan")),
            "scan_interval_hours": state.get("scan_interval_hours", 1.0),
            "rec_url":             BASE_URL,
            "auto_book_slots":     state.get("auto_book_slots", []),
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
    save_state(state)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "mine": len(state['my_reservations'])})}


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


def _schedule_self_probe(interval_hours: float) -> None:
    """Sleep for the configured interval then async-invoke this Lambda to continue the chain."""
    if not _is_within_scan_window():
        return
    fn = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
    if not fn:
        return
    sleep_secs = max(1, round(interval_hours * 3600))
    time.sleep(sleep_secs)
    boto3.client("lambda", region_name="us-west-2").invoke(
        FunctionName=fn,
        InvocationType="Event",
        Payload=json.dumps({"_self_probe": True}).encode(),
    )


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
    save_state(state)
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({
            "ok": True,
            "scan_interval_hours": requested,
        }),
    }


# ── Targeted daily probe (diagnostic) ────────────────────────────────────────

def _run_targeted_daily_scan() -> None:
    """Probe scan: weekends at 9 AM plus the newly published day for all times."""
    state = load_state()
    if _auto_watch_upcoming_weekends(state):
        save_state(state)

    today = date.today()
    new_day = today + timedelta(days=14)
    times_by_date: dict[str, list[str]] = {new_day.isoformat(): SLOT_TIMES[:]}

    for day_offset in range(16):
        target = today + timedelta(days=day_offset)
        if target.weekday() >= 5:
            times_by_date.setdefault(target.isoformat(), ["9:00 AM"])

    history_targets = _history_targets_from_map(times_by_date)
    started_at = _utc_now_iso()

    t0 = time.monotonic()
    print(
        "Targeted probe: "
        + ", ".join(f"{d} for {times_by_date[d]}" for d in sorted(times_by_date))
        + "…"
    )
    try:
        new_avail, _ = _api_scan(target_times_by_date=times_by_date)
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
        save_state(state)
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


# ── Legacy async worker ───────────────────────────────────────────────────────

def _run_async_worker() -> None:
    targets = load_check_dates()
    results = asyncio.run(main(targets=targets, target_time=DEFAULT_TIME_FILTER))
    if not results:
        msg = f"Pickleball scan complete: No {DEFAULT_TIME_FILTER} slots available."
    else:
        lines = [
            f"{day} {s['time']} -> {', '.join(s['courts']) or '(could not read courts)'}"
            for day, slots in results.items()
            for s in slots
        ]
        msg = f"Pickleball {DEFAULT_TIME_FILTER} available:\n" + "\n".join(lines)
    notify(msg)


# ── Lambda handler ────────────────────────────────────────────────────────────

def handler(event, context):
    # Internal: legacy async worker
    if event.get("_async_worker"):
        _run_async_worker()
        return

    # Internal: targeted daily probe (8 AM & 9 AM, 14 days out)
    if event.get("_targeted_daily_scan"):
        _run_targeted_daily_scan()
        return

    # Internal: direct scheduled invocation (for testing)
    if event.get("_scheduled"):
        _run_full_refresh_worker(force=True)
        return

    # Internal: targeted rescan of specific dates (missed-slot retry)
    if event.get("_rescan_dates"):
        _run_rescan_dates(
            event["_rescan_dates"],
            delay_seconds=event.get("_delay_seconds", 0),
        )
        return

    # EventBridge tick (fires every 15 min); honor configured scan_interval_hours.
    # Also handles self-probe chain for sub-15-min intervals.
    if event.get("source") == "aws.events" or event.get("_self_probe"):
        state = load_state()
        interval = float(state.get("scan_interval_hours") or 1.0)
        if not event.get("_self_probe") and not _should_run_scheduled_tick(state, datetime.now(tz=timezone.utc)):
            print(f"Skipping tick: interval={interval} hr.")
            return
        # Sub-15-min: only scan watched slots (fast). At normal cadence (≥15 min),
        # run the broader targeted scan for new-day discovery.
        if interval < 0.25:
            _run_scheduled_worker()
        else:
            _run_targeted_daily_scan()
        if interval < 0.25:
            _schedule_self_probe(interval)
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
        body = get_body(event)
        date_strs = body.get("dates") or []
        if date_strs:
            try:
                for d in date_strs:
                    date.fromisoformat(d)
            except ValueError as exc:
                return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": str(exc)})}
            _run_rescan_dates(date_strs)
        else:
            _run_full_refresh_worker(force=True)
        return handle_state(event)

    if path in ("/scan", "/prod/scan"):
        params = parse_query_params(event)
        mode   = (params.get("mode") or "async").strip().lower()

        if mode == "sync":
            if not _is_lambda_url_request(event):
                return _build_sync_redirect(event)
            try:
                targets     = parse_requested_dates(event)
                target_time = parse_time_filter(event, default=None)
            except ValueError as exc:
                return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": str(exc)})}
            results = asyncio.run(main(targets=targets, target_time=target_time))
            payload = build_scan_payload(targets=targets, target_time=target_time, results=results)
            return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps(payload)}

        lambda_client = boto3.client("lambda", region_name="us-west-2")
        lambda_client.invoke(
            FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
            InvocationType="Event",
            Payload=json.dumps({"_async_worker": True}).encode(),
        )
        return {
            "statusCode": 202,
            "headers": CORS_HEADERS,
            "body": json.dumps({"message": "Scan started. You will receive an SMS when complete."}),
        }

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
