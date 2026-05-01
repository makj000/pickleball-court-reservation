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
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
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
HEADLESS      = True
DEFAULT_TIME_FILTER = "8:00 AM"
NOTIFY_NUMBER = "+14154380400"
STATE_BUCKET  = os.environ.get("STATE_BUCKET", "")
STATE_KEY     = "state.json"

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, PUT, OPTIONS",
    "Content-Type": "application/json",
}

SLOT_TIMES = [
    "8:00 AM", "9:00 AM", "10:00 AM", "11:00 AM",
    "4:00 PM", "5:00 PM", "6:00 PM",
]

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*(AM|PM)", re.IGNORECASE)

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
        "summary": summary,
        "days": days,
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
        "watched_slots":   [],   # [{"date": "YYYY-MM-DD", "time": "H:MM AM"}]
        "my_reservations": [],   # [{"date": "YYYY-MM-DD", "time": "H:MM AM"}]
        "availability":    {},   # {"YYYY-MM-DD": {"H:MM AM": true/false}}
        "notified_slots":  [],   # ["YYYY-MM-DD|H:MM AM"] — already SMS'd, avoids repeat texts
        "last_scanned":    None,
    }


def load_state() -> dict:
    if not STATE_BUCKET:
        return _empty_state()
    s3 = boto3.client("s3", region_name="us-west-2")
    try:
        obj = s3.get_object(Bucket=STATE_BUCKET, Key=STATE_KEY)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return _empty_state()
        raise


def save_state(state: dict) -> None:
    if not STATE_BUCKET:
        return
    s3 = boto3.client("s3", region_name="us-west-2")
    s3.put_object(
        Bucket=STATE_BUCKET,
        Key=STATE_KEY,
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


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


async def get_courts_from_modal(page) -> list[str]:
    courts: list[str] = []
    try:
        dialog = page.locator('[role="dialog"]').first
        await dialog.wait_for(timeout=5_000)
        await page.wait_for_timeout(300)
        raw = await dialog.inner_text()
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        court_re = re.compile(r"^court\s+\d+", re.IGNORECASE)
        for line in lines:
            if court_re.match(line):
                courts.append(line)
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
    return courts


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
            continue
        courts = await get_courts_from_modal(page)
        results.append({"time": time_text, "courts": courts})
    print(f"{len(results)} slot(s): {', '.join(s['time'] for s in results)}")
    return results


async def scan_day_quick(page, target: date) -> dict[str, bool]:
    """Scan with modal checks. Returns {time: available} — True only if courts are found."""
    await select_date(page, target)
    slot_times = set(await collect_all_slot_buttons(page))
    results: dict[str, bool] = {}
    for t in SLOT_TIMES:
        if t not in slot_times:
            results[t] = False
            continue
        btn = page.locator("button").filter(has_text=re.compile(re.escape(t)))
        try:
            await btn.first.click(timeout=4_000)
        except PwTimeout:
            results[t] = False
            continue
        courts = await get_courts_from_modal(page)
        results[t] = len(courts) > 0
    return results


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

async def _scan_dates_quick(targets: list[date]) -> dict[str, dict[str, bool]]:
    """Returns {date_iso: {time: available}} without clicking into modals."""
    async with async_playwright() as pw:
        browser, page = await _new_page(pw)
        await login(page)
        results: dict[str, dict[str, bool]] = {}
        for target in targets:
            print(f"  Quick scan {target.isoformat()}…")
            try:
                results[target.isoformat()] = await scan_day_quick(page, target)
            except Exception as exc:
                print(f"  Error scanning {target.isoformat()}: {exc}")
                results[target.isoformat()] = {t: False for t in SLOT_TIMES}
        await browser.close()
    return results


# ── Scheduled worker ──────────────────────────────────────────────────────────

def _is_within_scan_window() -> bool:
    return 8 <= datetime.now(tz=PT).hour <= 22  # 8 AM – 10 PM PT inclusive


def _run_scheduled_worker() -> None:
    """Hourly scan of all 15 days in the window."""
    if not _is_within_scan_window():
        print("Outside scan window (8 AM – 10 PM PT). Skipping.")
        return

    state = load_state()
    today = date.today()
    targets = [today + timedelta(days=i) for i in range(15)]

    print(f"Scanning {len(targets)} date(s)…")
    new_avail = asyncio.run(_scan_dates_quick(targets))

    availability = state.get("availability", {})
    availability.update(new_avail)
    state["availability"] = availability
    state["last_scanned"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    # SMS dedup: only notify once per slot opening
    notified = set(state.get("notified_slots", []))
    newly_open = []
    still_open = set()

    for slot in state.get("watched_slots", []):
        key = f"{slot['date']}|{slot['time']}"
        is_open = availability.get(slot["date"], {}).get(slot["time"], False)
        if is_open:
            still_open.add(key)
            if key not in notified:
                newly_open.append(f"{slot['date']} {slot['time']}")
                notified.add(key)

    # Clear notifications for slots that are no longer open (so we re-notify if they open again)
    state["notified_slots"] = list(notified & still_open)
    save_state(state)

    if newly_open:
        msg = "Pickleball slot(s) now available:\n" + "\n".join(newly_open)
        send_sms(NOTIFY_NUMBER, msg)
        print(f"SMS sent: {msg}")
    else:
        print("No newly open watched slots.")


# ── Auth ──────────────────────────────────────────────────────────────────────

def _check_auth(event) -> bool:
    expected_user = os.environ.get("API_USERNAME", "test1")
    expected_pass = os.environ.get("API_PASSWORD", "clouderocks!")
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

    watched_set = {(s["date"], s["time"]) for s in state.get("watched_slots", [])}
    mine_set    = {(s["date"], s["time"]) for s in state.get("my_reservations", [])}

    grid = []
    for i in range(15):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        day_avail = state.get("availability", {}).get(d_str, {})
        slots = []
        for t in SLOT_TIMES:
            avail = day_avail.get(t)
            slots.append({
                "time":      t,
                "available": avail,
                "watching":  (d_str, t) in watched_set,
                "mine":      (d_str, t) in mine_set,
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
            "grid":         grid,
            "slot_times":   SLOT_TIMES,
            "last_scanned": state.get("last_scanned"),
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
        except (KeyError, ValueError):
            return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": f"Invalid slot: {s}"})}

    state = load_state()
    state["watched_slots"] = slots
    watched_keys = {f"{s['date']}|{s['time']}" for s in slots}
    state["notified_slots"] = [n for n in state.get("notified_slots", []) if n in watched_keys]
    save_state(state)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "watched": len(slots)})}



def handle_my_reservations(event) -> dict:
    body = get_body(event)
    slots = body.get("slots")
    if slots is None:
        return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "Missing slots"})}
    state = load_state()
    state["my_reservations"] = slots
    save_state(state)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "mine": len(slots)})}


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
        _run_scheduled_worker()
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

    if path in ("/scan", "/prod/scan"):
        params = parse_query_params(event)
        mode   = (params.get("mode") or "async").strip().lower()

        if mode == "sync":
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
