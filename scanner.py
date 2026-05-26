from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.request import Request, urlopen
import json

from config import COURT_PREFERENCE, COURT_SITE_IDS, PT, SLOT_TIMES, TARGET_COURTS, _HHMM_TO_TIME_TEXT
from state import _preferred_open_court, _utc_now_iso, load_state, save_state
from rec_api import _firebase_login, _get_cached_jwt, book_slot_api
from notify import send_telegram

_REC_API_BASE = "https://api.rec.us/v1/sites"
_API_TIMEOUT  = 10


def _fetch_one_court_raw(court_num: str, site_id: str) -> tuple[str, dict[str, dict]]:
    """Fetch 14-day availability for one court from the rec.us API."""
    url = f"{_REC_API_BASE}/{site_id}/availability"
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=_API_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    date_map = data.get("data") if isinstance(data, dict) else {}
    return court_num, date_map or {}


def _api_fetch_availability(
    target_times_by_date: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, dict[str, bool | None]]]:
    """Fetch availability for all courts in parallel via the rec.us REST API.

    Returns {date_iso: {time_text: {court_num: True|False|None}}}.
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

    for court_num, date_map in raw.items():
        for date_str, times_dict in date_map.items():
            for time_key in times_dict:
                hhmm = time_key[:5]
                time_text = _HHMM_TO_TIME_TEXT.get(hhmm)
                if time_text is None:
                    continue
                result.setdefault(date_str, {}).setdefault(
                    time_text, {c: False for c in TARGET_COURTS}
                )[court_num] = True

    return result


_DAY_CAP = 2       # max sessions per calendar date
_RATE_CAP = 4      # max app-initiated bookings per rate window
_RATE_WINDOW_HOURS = 1  # daytime rolling window


def _rate_window_start_utc() -> datetime:
    """Return the start of the current booking rate window (UTC).

    Daytime 8am–11pm PT: rolling 1-hour window.
    Nighttime 11pm–8am PT: since the most recent 11pm PT.
    """
    now_pt = datetime.now(tz=PT)
    hour = now_pt.hour
    if 8 <= hour < 23:
        return datetime.now(tz=timezone.utc) - timedelta(hours=_RATE_WINDOW_HOURS)
    night_start_pt = now_pt.replace(hour=23, minute=0, second=0, microsecond=0)
    if hour < 8:
        night_start_pt -= timedelta(days=1)
    return night_start_pt.astimezone(timezone.utc)


def _recent_booking_count(state: dict) -> int:
    window_start = _rate_window_start_utc().isoformat()
    return sum(
        1 for e in (state.get("app_booking_log") or [])
        if isinstance(e, dict) and e.get("booked_at", "") >= window_start
    )


def _api_scan(
    target_times_by_date: dict[str, list[str]] | None = None,
    auto_book_slots: list[dict] | None = None,
    jwt: str | None = None,
) -> tuple[dict[str, dict[str, dict[str, bool | None]]], list[dict]]:
    """Scan via HTTP API and book any newly open auto-book slots.

    Returns (availability, booked_slots).
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

    state_obj = load_state()
    if jwt is None:
        try:
            jwt = _get_cached_jwt(state_obj) or _firebase_login()
        except Exception as exc:
            failures = list(state_obj.get("auto_book_failures") or [])
            failures.insert(0, {"failed_at": _utc_now_iso(), "date": None, "time": None, "error": f"Login failed: {exc}"})
            state_obj["auto_book_failures"] = failures
            save_state(state_obj)
            try:
                send_telegram(f"❌ Auto-book login failed: {exc}")
            except Exception:
                pass
            raise

    for date_str, time_text, court_avail in to_book:
        # Per-day cap: count existing reservations + already booked this session
        sessions_on_day = (
            sum(1 for r in (state_obj.get("my_reservations") or []) if r["date"] == date_str)
            + sum(1 for b in booked if b["date"] == date_str)
        )
        if sessions_on_day >= _DAY_CAP:
            print(f"  Skipping {date_str} {time_text}: day cap reached ({sessions_on_day}/{_DAY_CAP}).")
            continue

        # Rate limit: cap total app-initiated bookings within the current time window
        recent = _recent_booking_count(state_obj)
        if recent >= _RATE_CAP:
            msg = f"⚠️ Booking rate limit reached ({recent} in window). Halting auto-book."
            print(msg)
            try:
                send_telegram(msg)
            except Exception:
                pass
            break

        open_courts = [c for c in COURT_PREFERENCE if court_avail.get(c) is True]
        try:
            send_telegram(f"🎯 Trying to book {date_str} {time_text} (courts: {', '.join(open_courts)})")
        except Exception:
            pass
        booked_court: str | None = None
        for attempt in range(1, 6):
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
                    break
            if booked_court:
                break
            if attempt < 5:
                print(f"  All courts failed (attempt {attempt}/5), retrying…")
        if booked_court:
            booked.append({"date": date_str, "time": time_text, "court": booked_court})
            if date_str in new_avail and time_text in new_avail[date_str]:
                new_avail[date_str][time_text][booked_court] = False
            log = list(state_obj.get("app_booking_log") or [])
            log.insert(0, {"booked_at": _utc_now_iso(), "date": date_str, "time": time_text, "court": booked_court})
            state_obj["app_booking_log"] = log
            save_state(state_obj)
        else:
            failures = list(state_obj.get("auto_book_failures") or [])
            failures.insert(0, {"failed_at": _utc_now_iso(), "date": date_str, "time": time_text, "error": "Failed after 5 attempts"})
            state_obj["auto_book_failures"] = failures
            save_state(state_obj)
            try:
                send_telegram(f"❌ Failed to book {date_str} {time_text} after 5 attempts")
            except Exception:
                pass
    return new_avail, booked
