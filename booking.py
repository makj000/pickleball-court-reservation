from state import _utc_now_iso
from notify import notify


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
    if not booked_slots:
        return
    lines = [f"{b['date']} {b['time']} Court {b['court']}" for b in booked_slots]
    msg = "Auto-booked pickleball slot(s):\n" + "\n".join(lines)
    notify(msg, subject="Pickleball auto-booking confirmed")
