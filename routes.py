import json
from datetime import date, timedelta

from config import (
    APP_VERSION, AUTO_BOOKING_DISABLED_MESSAGE, AUTO_BOOKING_ENABLED, CORS_HEADERS,
    COURT_PREFERENCE, SLOT_TIMES,
)
from state import (
    _auto_book_slot_is_too_close, _has_future_watched_slots, _load_telegram_usage,
    _normalize_court_number, _normalize_slot_records, _normalize_time_availability,
    _utc_now_iso, load_state, save_state,
)
from http_utils import get_body
from rec_api import _configured_rec_accounts, _firebase_login, _get_cached_jwt, _cache_jwt, cancel_booking_api, sync_rec_my_reservations
from scheduler import (
    ALLOWED_SCAN_INTERVALS, _auto_book_time_map, _clear_queued_scheduled_probe,
    _queue_next_scheduled_probe,
)
from scanner import _api_scan
from booking import _apply_booked_slots, _notify_booked_slots
from calendar_sync import enqueue_calendar_event_delete


def handle_state(event) -> dict:
    state = load_state()
    today = date.today()

    watched_set   = {(s["date"], s["time"], s["court"]) for s in state.get("watched_slots", [])}
    mine_by_slot: dict[tuple[str, str, str], list[dict]] = {}
    for reservation in state.get("my_reservations", []) or []:
        key = (reservation["date"], reservation["time"], reservation["court"])
        account_index = reservation.get("account_index")
        account_email = reservation.get("account_email")
        account_entry = {
            "index": account_index,
            "email": account_email,
            "label": account_email.split("@", 1)[0] if account_email else (
                f"Account {account_index}" if account_index else "Mine"
            ),
        }
        for field in ("booking_id", "reservation_id", "facility_rental_id"):
            if reservation.get(field):
                account_entry[field] = reservation.get(field)
        mine_by_slot.setdefault(key, []).append(account_entry)
    mine_set      = set(mine_by_slot)
    friend_set    = {(s["date"], s["time"], s["court"]) for s in state.get("friend_reservations", [])}
    auto_book_set   = {(ab["date"], ab["time"]) for ab in state.get("auto_book_slots", [])}
    auto_book_count = {
        (ab["date"], ab["time"]): int(ab.get("count") or 1)
        for ab in state.get("auto_book_slots", [])
    }

    grid = []
    for i in range(16):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        day_avail = state.get("availability", {}).get(d_str, {})
        slots = []
        for t in SLOT_TIMES:
            time_avail = _normalize_time_availability(day_avail.get(t))
            for court in COURT_PREFERENCE:
                key = (d_str, t, court)
                slots.append({
                    "time":        t,
                    "court":       court,
                    "available":   time_avail.get(court),
                    "watching":    key in watched_set,
                    "mine":        key in mine_set,
                    "reservation_accounts": mine_by_slot.get(key, []),
                    "friend":      key in friend_set,
                    "auto_booking": (d_str, t) in auto_book_set,
                    "auto_book_count": auto_book_count.get((d_str, t), 1),
                })
        grid.append({
            "date":  d_str,
            "label": d.strftime("%a, %b %-d"),
            "slots": slots,
        })

    from config import BASE_URL
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
            "release_probe_log": state.get("release_probe_log", []),
            "scan_interval_hours": state.get("scan_interval_hours", 1.0),
            "queued_scheduled_probe_at": state.get("queued_scheduled_probe_at"),
            "queued_publish_probe_date": state.get("queued_publish_probe_date"),
            "rec_url":             BASE_URL,
            "auto_book_slots":     state.get("auto_book_slots", []),
            "my_reservations_synced_at": state.get("my_reservations_synced_at"),
            "my_reservations_source": state.get("my_reservations_source"),
            "friend_reservations_updated_at": state.get("friend_reservations_updated_at"),
            "auto_watch_weekends_enabled": bool(state.get("auto_watch_weekends_enabled", True)),
            "auto_watch_weekends_8am_enabled": bool(state.get("auto_watch_weekends_8am_enabled", False)),
            "seen_open_days": state.get("seen_open_days") or {},
            "auto_book_failures": state.get("auto_book_failures") or [],
            "telegram_call_history": _load_telegram_usage(),
        }),
    }


def handle_cancel_reservation(event) -> dict:
    body = get_body(event)
    slot_date = body.get("date")
    slot_time = body.get("time")
    court = _normalize_court_number(body.get("court"))
    booking_id = body.get("booking_id")
    account_index = body.get("account_index")
    try:
        date.fromisoformat(slot_date)
    except (TypeError, ValueError):
        return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "Invalid date"})}
    if slot_time not in SLOT_TIMES or court is None:
        return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "Invalid time or court"})}

    state = load_state()
    sync_rec_my_reservations(state)
    reservations = [
        r for r in (state.get("my_reservations") or [])
        if r.get("date") == slot_date and r.get("time") == slot_time and r.get("court") == court
    ]
    if account_index is not None:
        try:
            wanted_account = int(account_index)
        except (TypeError, ValueError):
            return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "Invalid account_index"})}
        reservations = [r for r in reservations if int(r.get("account_index") or 0) == wanted_account]
    if booking_id:
        reservations = [r for r in reservations if r.get("booking_id") == booking_id]
    if not reservations:
        save_state(state)
        return {"statusCode": 404, "headers": CORS_HEADERS, "body": json.dumps({"error": "Matching rec.us reservation not found after sync."})}

    reservation = reservations[0]
    booking_id = reservation.get("booking_id")
    account_index = int(reservation.get("account_index") or 1)
    if not booking_id:
        save_state(state)
        return {"statusCode": 409, "headers": CORS_HEADERS, "body": json.dumps({"error": "Reservation is missing rec.us booking_id; sync reservations and try again."})}

    account = next((a for a in _configured_rec_accounts() if int(a["index"]) == account_index), None)
    if account is None:
        return {"statusCode": 409, "headers": CORS_HEADERS, "body": json.dumps({"error": f"Account {account_index} is not configured."})}

    jwt = _get_cached_jwt(state, account_index)
    if not jwt:
        jwt = _firebase_login(account)
        _cache_jwt(state, jwt, account_index)

    transaction_log: dict = {}
    ok, rec_body = cancel_booking_api(jwt, str(booking_id), transaction_log)
    if not ok:
        save_state(state)
        status = transaction_log.get("cancel", {}).get("response", {}).get("status") or 502
        return {
            "statusCode": 502,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "error": f"rec.us cancellation failed [{status}]",
                "slot": reservation,
                "transaction_log": transaction_log,
            }),
        }

    sync_rec_my_reservations(state)
    save_state(state)
    enqueue_calendar_event_delete(reservation)
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({
            "ok": True,
            "slot": reservation,
            "rec_response": rec_body,
            "transaction_log": transaction_log,
            "my_reservations": state.get("my_reservations", []),
            "synced_at": state.get("my_reservations_synced_at"),
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
    my_keys = {(s["date"], s["time"], s["court"]) for s in state.get("my_reservations", [])}
    state["friend_reservations"] = [
        s for s in state.get("friend_reservations", [])
        if (s["date"], s["time"], s["court"]) not in my_keys
    ]
    state["my_reservations_synced_at"] = _utc_now_iso()
    state["my_reservations_source"] = "manual"
    save_state(state)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "mine": len(state['my_reservations'])})}


def handle_friend_reservations(event) -> dict:
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
    state["friend_reservations"] = _normalize_slot_records(slots, expand_legacy=False)
    friend_keys = {(s["date"], s["time"], s["court"]) for s in state.get("friend_reservations", [])}
    state["my_reservations"] = [
        s for s in state.get("my_reservations", [])
        if (s["date"], s["time"], s["court"]) not in friend_keys
    ]
    state["friend_reservations_updated_at"] = _utc_now_iso()
    save_state(state)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "friend": len(state['friend_reservations'])})}


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
            date.fromisoformat(slot_date)
            slot_time = s["time"]
            if slot_time not in SLOT_TIMES:
                raise ValueError(f"Invalid time: {slot_time}")
            if slot_date < today_str:
                continue
        except (KeyError, ValueError) as exc:
            return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": f"Invalid slot: {s} — {exc}"})}
        if _auto_book_slot_is_too_close(slot_date, slot_time):
            continue
        key = (slot_date, slot_time)
        if key in seen:
            continue
        seen.add(key)
        entry = {"date": slot_date, "time": slot_time}
        if s.get("count") is not None:
            entry["count"] = s.get("count")
        normalized.append(entry)

    state = load_state()
    state["auto_book_slots"] = normalized
    save_state(state)  # save_state normalizes: clamps count to 1-3
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "auto_book": len(normalized)})}


def handle_force_book(event) -> dict:
    if not AUTO_BOOKING_ENABLED:
        return {
            "statusCode": 503,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": AUTO_BOOKING_DISABLED_MESSAGE}),
        }

    state = load_state()
    auto_book_slots = state.get("auto_book_slots") or []
    targets = _auto_book_time_map(auto_book_slots)
    if not targets:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "No future auto-book slots are configured."}),
        }

    detailed_log: list[dict] = []
    try:
        availability, booked_slots = _api_scan(
            target_times_by_date=targets,
            auto_book_slots=auto_book_slots,
            detailed_log=detailed_log,
        )
    except Exception as exc:
        return {
            "statusCode": 502,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "error": f"Force booking failed: {exc}",
                "targets": targets,
                "attempt_log": detailed_log,
            }),
        }

    state = load_state()
    state_availability = state.get("availability", {})
    for date_str, day_avail in availability.items():
        day_state = state_availability.get(date_str, {})
        for time_text, court_avail in day_avail.items():
            day_state[time_text] = _normalize_time_availability(court_avail)
        state_availability[date_str] = day_state
    state["availability"] = state_availability
    if booked_slots:
        _apply_booked_slots(state, booked_slots)
    sync_rec_my_reservations(state)
    save_state(state)
    if booked_slots:
        _notify_booked_slots(booked_slots)

    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({
            "ok": True,
            "targets": targets,
            "booked_slots": booked_slots,
            "attempt_log": detailed_log,
        }),
    }


def handle_auto_watch_weekends(event) -> dict:
    from state import _auto_watch_upcoming_weekends
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


def handle_auto_watch_weekends_8am(event) -> dict:
    from state import _auto_watch_upcoming_weekends
    body = get_body(event)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"ok": False, "error": "enabled must be true or false"}),
        }
    state = load_state()
    state["auto_watch_weekends_8am_enabled"] = enabled
    if enabled:
        _auto_watch_upcoming_weekends(state)
    save_state(state)
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({"ok": True, "auto_watch_weekends_8am_enabled": enabled}),
    }


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
