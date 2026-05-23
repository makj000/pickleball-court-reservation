import json
from datetime import date, timedelta

from config import APP_VERSION, CORS_HEADERS, COURT_PREFERENCE, SLOT_TIMES
from state import (
    _has_future_watched_slots, _load_telegram_usage, _normalize_court_number,
    _normalize_slot_records, _normalize_time_availability, _utc_now_iso,
    load_state, save_state,
)
from http_utils import get_body
from rec_api import sync_rec_my_reservations
from scheduler import (
    ALLOWED_SCAN_INTERVALS, _clear_queued_scheduled_probe, _queue_next_scheduled_probe,
)


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
            "scan_interval_hours": state.get("scan_interval_hours", 1.0),
            "queued_scheduled_probe_at": state.get("queued_scheduled_probe_at"),
            "queued_publish_probe_date": state.get("queued_publish_probe_date"),
            "rec_url":             BASE_URL,
            "auto_book_slots":     state.get("auto_book_slots", []),
            "my_reservations_synced_at": state.get("my_reservations_synced_at"),
            "my_reservations_source": state.get("my_reservations_source"),
            "auto_watch_weekends_enabled": bool(state.get("auto_watch_weekends_enabled", True)),
            "auto_watch_weekends_8am_enabled": bool(state.get("auto_watch_weekends_8am_enabled", False)),
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
            date.fromisoformat(slot_date)
            slot_time = s["time"]
            if slot_time not in SLOT_TIMES:
                raise ValueError(f"Invalid time: {slot_time}")
            if slot_date < today_str:
                continue
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
