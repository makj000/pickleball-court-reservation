from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.request import Request, urlopen
import json

from config import COURT_PREFERENCE, COURT_SITE_IDS, SLOT_TIMES, TARGET_COURTS, _HHMM_TO_TIME_TEXT
from state import _preferred_open_court, load_state
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
            for c in new_avail.get(date_str, {}).get(time_text, {}):
                new_avail[date_str][time_text][c] = False
        else:
            try:
                send_telegram(f"❌ Failed to book {date_str} {time_text} after 5 attempts")
            except Exception:
                pass
    return new_avail, booked
