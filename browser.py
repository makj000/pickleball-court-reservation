import os
import re
from datetime import date

from playwright.async_api import async_playwright, TimeoutError as PwTimeout

from config import BASE_URL, COURT_RE, COURT_PREFERENCE, HEADLESS, SLOT_TIMES, TARGET_COURTS, TIME_RE
from state import (
    _empty_court_availability, _normalize_time_availability, _preferred_open_court,
    load_check_dates,
)
from notify import ordinal


_BLOCK_RESOURCE_TYPES = {"stylesheet", "image", "font", "media"}
_BLOCK_URL_PATTERNS = (
    "google-analytics.com", "googletagmanager.com", "analytics.google.com",
    "segment.io", "segment.com", "amplitude.com", "mixpanel.com", "hotjar.com",
    "intercom.io", "intercomcdn.com", "fullstory.com", "heap.io",
    "sentry.io", "datadog-browser-agent",
)

_SESSION_FILE = "/tmp/pl_session.json"


def _browser_args() -> list[str]:
    return ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]


def _user_agent() -> str:
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )


async def _block_non_essential(route, request):
    if request.resource_type in _BLOCK_RESOURCE_TYPES:
        await route.abort()
        return
    if any(p in request.url for p in _BLOCK_URL_PATTERNS):
        await route.abort()
        return
    await route.continue_()


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


async def select_date(page, target: date) -> None:
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


async def collect_all_slot_buttons(page, stop_when_found: set[str] | None = None) -> list[str]:
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
    from state import _normalize_court_number
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
    import re as _re
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
        btn = page.locator("button").filter(has_text=_re.compile(_re.escape(time_text)))
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


async def main(*, targets=None, target_time=None) -> dict[str, list[dict]]:
    """Browser-based full scan — used by the /scan endpoint."""
    import asyncio
    from config import load_check_dates as _lcd
    async with async_playwright() as pw:
        browser, context, page = await _new_page(pw)
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)

        all_results: dict[str, list[dict]] = {}
        targets = targets or _lcd()
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
