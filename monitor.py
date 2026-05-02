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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

APP_DIR = Path(__file__).parent
CONFIG_FILE = APP_DIR / "config.json"
load_dotenv(APP_DIR / ".env")

PT = ZoneInfo("America/Los_Angeles")

# ── Credentials ───────────────────────────────────────────────────────────────
EMAIL    = os.environ["EMAIL"]
PASSWORD = os.environ["PASSWORD"]
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


BASE_URL      = load_config()["base_url"]
SYNC_SCAN_URL = load_config().get("sync_scan_url", "")
HEADLESS      = True
DEFAULT_TIME_FILTER = "8:00 AM"
NOTIFY_NUMBER = "+14154380400"
STATE_BUCKET  = os.environ.get("STATE_BUCKET", "")
STATE_KEY     = "state.json"
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


# ── SMS ───────────────────────────────────────────────────────────────────────

def send_sms(to: str, message: str) -> None:
    sns = boto3.client("sns", region_name="us-west-2")
    sns.publish(PhoneNumber=to, Message=message)


def ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


# ── S3 State ──────────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "watched_slots":       [],   # [{"date": "YYYY-MM-DD", "time": "H:MM AM", "court": "6"}]
        "my_reservations":     [],   # [{"date": "YYYY-MM-DD", "time": "H:MM AM", "court": "6"}]
        "availability":        {},   # {"YYYY-MM-DD": {"H:MM AM": {"6": true, "4": false, "5": true}}}
        "notified_slots":      [],   # ["YYYY-MM-DD|H:MM AM|6"] — already SMS'd, avoids repeat texts
        "last_scanned":        None,
        "scan_interval_hours": 1.0,
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
    normalized["last_scanned"] = state.get("last_scanned")
    normalized["scan_interval_hours"] = state.get("scan_interval_hours", 1.0)
    if state.get("scan_started_at"):
        normalized["scan_started_at"] = state["scan_started_at"]

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
    return normalized


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
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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


# ── Login ─────────────────────────────────────────────────────────────────────

async def login(page) -> None:
    print("Logging in…")
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
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

async def collect_all_slot_buttons(page) -> list[str]:
    seen: set[str] = set()
    slots: list[str] = []

    async def harvest():
        # all_inner_texts() grabs all button text atomically — no index race condition
        texts = await page.locator("button").all_inner_texts()
        for text in texts:
            first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
            if TIME_RE.match(first_line) and first_line not in seen:
                seen.add(first_line)
                slots.append(first_line)

    await harvest()

    for _ in range(20):
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


async def scan_day_quick(page, target: date) -> dict[str, dict[str, bool | None]]:
    """Build state booleans from the same slot scan path used by /scan."""
    slots = await scan_day(page, target, target_time=None)
    return _slots_to_availability(slots)


# ── Browser session helpers ───────────────────────────────────────────────────

def _browser_args() -> list[str]:
    return ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]


def _user_agent() -> str:
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )


async def _new_page(pw):
    browser = await pw.chromium.launch(headless=HEADLESS, args=_browser_args())
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=_user_agent(),
    )
    return browser, await context.new_page()


# ── Full scan (existing /scan endpoint) ───────────────────────────────────────

async def main(*, targets=None, target_time=None) -> dict[str, list[dict]]:
    async with async_playwright() as pw:
        browser, page = await _new_page(pw)
        await login(page)

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


# ── Quick scan (for scheduled worker) ────────────────────────────────────────

async def _scan_dates_quick(targets: list[date]) -> dict[str, dict[str, dict[str, bool | None]]]:
    """Returns {date_iso: {time: {court: available}}}."""
    async with async_playwright() as pw:
        browser, page = await _new_page(pw)
        await login(page)
        results: dict[str, dict[str, dict[str, bool | None]]] = {}
        for target in targets:
            print(f"  Quick scan {target.isoformat()}…")
            try:
                results[target.isoformat()] = await scan_day_quick(page, target)
            except Exception as exc:
                print(f"  Error scanning {target.isoformat()}: {exc}")
                results[target.isoformat()] = {t: _empty_court_availability(None) for t in SLOT_TIMES}
        await browser.close()
    return results


# ── Scheduled worker ──────────────────────────────────────────────────────────

def _is_within_scan_window() -> bool:
    return 8 <= datetime.now(tz=PT).hour <= 22  # 8 AM – 10 PM PT inclusive


def _run_scheduled_worker(*, force: bool = False) -> None:
    """Hourly scan of all 15 days in the window."""
    if not _is_within_scan_window():
        print("Outside scan window (8 AM – 10 PM PT). Skipping.")
        return

    state = load_state()
    active_scan = _active_scan_started_at(state)
    if active_scan:
        print(f"Another scheduled scan started at {active_scan.isoformat()}. Skipping.")
        return

    interval_hours = state.get("scan_interval_hours", 1.0)
    if not force and state.get("last_scanned"):
        last = _parse_utc_iso(state["last_scanned"])
        if last is None:
            last = datetime.now(tz=timezone.utc) - timedelta(hours=interval_hours)
        elapsed = (datetime.now(tz=timezone.utc) - last).total_seconds() / 3600
        if elapsed < interval_hours:
            print(f"Last scan {elapsed:.1f}h ago, interval {interval_hours}h. Skipping.")
            return

    today = date.today()
    targets = [today + timedelta(days=i) for i in range(15)]
    started_at = _utc_now_iso()

    state["scan_started_at"] = started_at
    save_state(state)

    print(f"Scanning {len(targets)} date(s)…")
    try:
        new_avail = asyncio.run(_scan_dates_quick(targets))

        state = load_state()
        availability = state.get("availability", {})
        availability.update(new_avail)
        state["availability"] = availability
        state["last_scanned"] = _utc_now_iso()

        newly_open = []
        for slot in state.get("watched_slots", []):
            is_open = (
                availability
                .get(slot["date"], {})
                .get(slot["time"], {})
                .get(slot["court"], False)
            )
            if is_open:
                newly_open.append(f"{slot['date']} {slot['time']} Court {slot['court']}")

        state["notified_slots"] = []
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
        save_state(state)
    except Exception:
        state = load_state()
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
            save_state(state)
        raise

    if newly_open:
        msg = "Pickleball slot(s) now available:\n" + "\n".join(newly_open)
        send_sms(NOTIFY_NUMBER, msg)
        print(f"SMS sent: {msg}")
    else:
        print("No newly open watched slots.")


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

    watched_set = {(s["date"], s["time"], s["court"]) for s in state.get("watched_slots", [])}
    mine_set    = {(s["date"], s["time"], s["court"]) for s in state.get("my_reservations", [])}

    grid = []
    for i in range(15):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        day_avail = state.get("availability", {}).get(d_str, {})
        slots = []
        for t in SLOT_TIMES:
            time_avail = _normalize_time_availability(day_avail.get(t))
            for court in COURT_PREFERENCE:
                slots.append({
                    "time":      t,
                    "court":     court,
                    "available": time_avail.get(court),
                    "watching":  (d_str, t, court) in watched_set,
                    "mine":      (d_str, t, court) in mine_set,
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
            "last_scanned":        state.get("last_scanned"),
            "scan_interval_hours": state.get("scan_interval_hours", 1.0),
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


def handle_scan_interval(event) -> dict:
    body = get_body(event)
    hours = body.get("hours")
    valid = [0.5, 1, 2, 3, 6, 24]
    if hours not in valid:
        return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": f"hours must be one of {valid}"})}

    state = load_state()
    state["scan_interval_hours"] = hours
    save_state(state)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "scan_interval_hours": hours})}


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
    send_sms(NOTIFY_NUMBER, msg)
    print(f"SMS sent to {NOTIFY_NUMBER}.")


# ── Lambda handler ────────────────────────────────────────────────────────────

def handler(event, context):
    # Internal: legacy async worker
    if event.get("_async_worker"):
        _run_async_worker()
        return

    # Internal: direct scheduled invocation (for testing)
    if event.get("_scheduled"):
        _run_scheduled_worker(force=True)
        return

    # EventBridge hourly trigger
    if event.get("source") == "aws.events":
        _run_scheduled_worker()
        return

    method = get_method(event)
    path   = get_path(event)

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

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

    if path == "/force-scan" and method == "POST":
        lambda_client = boto3.client("lambda", region_name="us-west-2")
        lambda_client.invoke(
            FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
            InvocationType="Event",
            Payload=json.dumps({"_scheduled": True}).encode(),
        )
        return {"statusCode": 202, "headers": CORS_HEADERS, "body": json.dumps({"message": "Scan started"})}

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
        send_sms(NOTIFY_NUMBER, msg)
        print(f"  SMS sent to {NOTIFY_NUMBER}.")
