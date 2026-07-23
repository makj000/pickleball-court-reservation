from state import _utc_now_iso
from notify import notify
from calendar_sync import enqueue_calendar_event


def _apply_booked_slots(state: dict, booked_slots: list[dict]) -> None:
    """Update state after successful bookings."""
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
    state["auto_book_slots"] = _remaining_auto_book_slots(state)
    if booked_slots:
        state["my_reservations_synced_at"] = _utc_now_iso()
        state["my_reservations_source"] = "auto-book"


def _remaining_auto_book_slots(state: dict) -> list[dict]:
    reserved_count: dict[tuple[str, str], int] = {}
    for reservation in state.get("my_reservations") or []:
        key = (reservation.get("date"), reservation.get("time"))
        reserved_count[key] = reserved_count.get(key, 0) + 1

    remaining = []
    for slot in state.get("auto_book_slots") or []:
        key = (slot.get("date"), slot.get("time"))
        try:
            target_count = max(1, min(3, int(slot.get("count") or 1)))
        except (TypeError, ValueError):
            target_count = 1
        if reserved_count.get(key, 0) < target_count:
            remaining.append(slot)
    return remaining


def _notify_booked_slots(booked_slots: list[dict]) -> None:
    if not booked_slots:
        return
    lines = [f"{b['date']} {b['time']} Court {b['court']}" for b in booked_slots]
    msg = "Auto-booked pickleball slot(s):\n" + "\n".join(lines)
    notify(msg, subject="Pickleball auto-booking confirmed")
    for booked_slot in booked_slots:
        enqueue_calendar_event(booked_slot)
