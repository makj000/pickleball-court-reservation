from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from config import (
    PT, SLOT_TIMES, SQS_STALE_GRACE_SECONDS,
    _RELEASE_BURST_UNTIL_S, _RELEASE_END_S, _RELEASE_POST_INTERVAL_S, _RELEASE_PRE_INTERVAL_S,
)
from state import (
    _active_scan_started_at, _attach_history_results, _auto_watch_on_new_day_openings,
    _auto_watch_upcoming_weekends, _enqueue_work, _history_targets_from_map, _new_day_iso,
    _normalize_time_availability, _parse_utc_iso, _record_newly_open_dates, _record_scan_history,
    _utc_now_iso, load_state, save_state,
)
from notify import _alert_lines_for_open_targets, notify, send_telegram
from rec_api import _cache_jwt, _firebase_login, _get_cached_jwt, sync_rec_my_reservations
from scanner import _api_scan, _api_fetch_availability
from booking import _apply_booked_slots, _notify_booked_slots


_INTERVAL_15S  = round(15 / 3600, 6)
_INTERVAL_1MIN = round(1  / 60,   6)
_INTERVAL_5MIN = round(5  / 60,   6)
ALLOWED_SCAN_INTERVALS = (_INTERVAL_15S, _INTERVAL_1MIN, _INTERVAL_5MIN, 0.25, 0.5, 1.0, 2.0, 3.0)


def _new_day_from_pt_now(now_pt: datetime | None = None) -> date:
    return ((now_pt or datetime.now(tz=PT)).date() + timedelta(days=14))


def _future_watched_time_map(state: dict) -> dict[str, list[str]]:
    today_str = date.today().isoformat()
    times_by_date: dict[str, list[str]] = {}
    for slot in state.get("watched_slots", []):
        d_str = slot.get("date", "")
        t_str = slot.get("time", "")
        if d_str >= today_str and t_str in SLOT_TIMES:
            times_by_date.setdefault(d_str, [])
            if t_str not in times_by_date[d_str]:
                times_by_date[d_str].append(t_str)
    return times_by_date


def _auto_book_time_map(auto_book_slots: list[dict]) -> dict[str, list[str]]:
    today_str = date.today().isoformat()
    times_by_date: dict[str, list[str]] = {}
    for slot in auto_book_slots or []:
        d_str = slot.get("date", "")
        t_str = slot.get("time", "")
        if d_str >= today_str and t_str in SLOT_TIMES:
            times_by_date.setdefault(d_str, [])
            if t_str not in times_by_date[d_str]:
                times_by_date[d_str].append(t_str)
    return times_by_date


def _watched_and_auto_book_targets(state: dict) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}
    for d_str, times in _future_watched_time_map(state).items():
        targets.setdefault(d_str, [])
        for t in times:
            if t not in targets[d_str]:
                targets[d_str].append(t)
    for d_str, times in _auto_book_time_map(state.get("auto_book_slots") or []).items():
        targets.setdefault(d_str, [])
        for t in times:
            if t not in targets[d_str]:
                targets[d_str].append(t)
    return targets


def _is_within_scan_window() -> bool:
    now = datetime.now(tz=PT)
    return 8 <= now.hour < 22


def _is_publish_detection_window() -> bool:
    now = datetime.now(tz=PT)
    return now.hour == 8 and now.minute < 10


def _scheduled_scan_targets(state: dict) -> tuple[dict[str, list[str]], bool]:
    targets = _watched_and_auto_book_targets(state)
    if not targets:
        return {}, False
    new_day_str = _new_day_iso()
    raw_seen = state.get("seen_open_days") or {}
    seen_open_keys = set(raw_seen.keys() if isinstance(raw_seen, dict) else raw_seen)
    special_new_day_scan = new_day_str not in seen_open_keys
    if special_new_day_scan:
        new_day = date.fromisoformat(new_day_str)
        new_day_times = ["8:00 AM", "9:00 AM"] if new_day.weekday() >= 5 else SLOT_TIMES[:]
        targets.setdefault(new_day_str, [])
        for t in new_day_times:
            if t not in targets[new_day_str]:
                targets[new_day_str].append(t)
    return targets, special_new_day_scan


def _full_scan_targets() -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}
    today = date.today()
    for i in range(16):
        d_str = (today + timedelta(days=i)).isoformat()
        targets[d_str] = list(SLOT_TIMES)
    return targets


def _should_run_scheduled_tick(state: dict, now: datetime) -> bool:
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


def _scheduled_probe_delay_seconds(interval_hours: float) -> int:
    from config import SQS_DELAY_MAX_SECONDS
    return max(1, min(round(interval_hours * 3600), SQS_DELAY_MAX_SECONDS))


def _clear_queued_scheduled_probe(state: dict) -> None:
    state["queued_scheduled_probe_at"] = None
    state["queued_scheduled_probe_token"] = None


def _queued_scheduled_probe_is_current(state: dict, interval_hours: float) -> bool:
    token = state.get("queued_scheduled_probe_token")
    queued_at = _parse_utc_iso(state.get("queued_scheduled_probe_at"))
    if not token or not queued_at:
        return False
    delay = _scheduled_probe_delay_seconds(interval_hours)
    stale_after = max(delay * 2, SQS_STALE_GRACE_SECONDS)
    return queued_at >= datetime.now(tz=timezone.utc) - timedelta(seconds=stale_after)


def _queue_next_scheduled_probe(interval_hours: float, *, state: dict | None = None, force: bool = False) -> bool:
    if interval_hours >= 0.25 or not _is_within_scan_window():
        if state is not None and force:
            _clear_queued_scheduled_probe(state)
            save_state(state)
        return False

    state = state or load_state()
    if not force and _queued_scheduled_probe_is_current(state, interval_hours):
        print(f"Scheduled probe already queued for {state.get('queued_scheduled_probe_at')}.")
        return True
    if not _scheduled_scan_targets(state)[0]:
        _clear_queued_scheduled_probe(state)
        save_state(state)
        print("No future watched or auto-book slots; not queueing scheduled probe.")
        return False

    delay_seconds = _scheduled_probe_delay_seconds(interval_hours)
    token = str(time.time_ns())
    queued_at = (
        datetime.now(tz=timezone.utc) + timedelta(seconds=delay_seconds)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    state["queued_scheduled_probe_at"] = queued_at
    state["queued_scheduled_probe_token"] = token
    save_state(state)

    if _enqueue_work("scheduled_probe", {"token": token}, delay_seconds=delay_seconds):
        return True

    latest = load_state()
    if latest.get("queued_scheduled_probe_token") == token:
        _clear_queued_scheduled_probe(latest)
        save_state(latest)
    return False


def _run_full_refresh_worker(*, force: bool = False) -> None:
    state = load_state()
    active_scan = _active_scan_started_at(state)
    if active_scan:
        print(f"Another scan started at {active_scan.isoformat()}. Skipping.")
        return

    _auto_watch_upcoming_weekends(state)
    scan_targets = _full_scan_targets()
    history_targets = _history_targets_from_map(scan_targets)
    started_at = _utc_now_iso()

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
        _record_newly_open_dates(state, new_avail, started_at)
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

        sync_rec_my_reservations(state)
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
        _record_newly_open_dates(state, new_avail, started_at)
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

        sync_rec_my_reservations(state)
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


def _run_targeted_daily_scan() -> None:
    """Probe scan: weekends at 9 AM plus the newly published day for all times."""
    import time as _time
    state = load_state()
    active_scan = _active_scan_started_at(state)
    if active_scan:
        print(f"Targeted daily scan: release probe session running since {active_scan.isoformat()}, skipping.")
        return
    if _auto_watch_upcoming_weekends(state):
        save_state(state)

    today = date.today()
    new_day = today + timedelta(days=14)
    new_day_times = ["8:00 AM", "9:00 AM"] if new_day.weekday() >= 5 else SLOT_TIMES[:]
    times_by_date: dict[str, list[str]] = {new_day.isoformat(): new_day_times}

    for day_offset in range(16):
        target = today + timedelta(days=day_offset)
        if target.weekday() >= 5:
            times_by_date.setdefault(target.isoformat(), ["9:00 AM"])

    auto_book_slots = state.get("auto_book_slots") or []
    for date_str, times in _auto_book_time_map(auto_book_slots).items():
        existing = set(times_by_date.get(date_str, []))
        existing.update(times)
        times_by_date[date_str] = [t for t in SLOT_TIMES if t in existing]

    history_targets = _history_targets_from_map(times_by_date)
    started_at = _utc_now_iso()

    t0 = _time.monotonic()
    print(
        "Targeted probe: "
        + ", ".join(f"{d} for {times_by_date[d]}" for d in sorted(times_by_date))
        + "…"
    )
    try:
        new_avail, booked_slots = _api_scan(
            target_times_by_date=times_by_date,
            auto_book_slots=auto_book_slots,
        )
        elapsed = _time.monotonic() - t0

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
        _record_newly_open_dates(state, new_avail, started_at)
        sync_rec_my_reservations(state)
        _apply_booked_slots(state, booked_slots)
        save_state(state)
        _notify_booked_slots(booked_slots)
        if open_target_lines:
            msg = "Pickleball slot(s) now available:\n" + "\n".join(open_target_lines)
            notify(msg)
        else:
            print("No open watched or auto-book slots in this scan.")
    except Exception as exc:
        elapsed = _time.monotonic() - t0
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


def _run_queued_scheduled_probe(token: str) -> None:
    state = load_state()
    if not token or token != state.get("queued_scheduled_probe_token"):
        print("Skipping stale scheduled probe queue item.")
        return

    interval = float(state.get("scan_interval_hours") or 1.0)
    if interval >= 0.25 or not _is_within_scan_window():
        _clear_queued_scheduled_probe(state)
        save_state(state)
        print(f"Cleared queued scheduled probe; interval={interval} hr or outside scan window.")
        return

    _run_scheduled_worker()

    state = load_state()
    if token != state.get("queued_scheduled_probe_token"):
        print("Scheduled probe changed while this item was running; not queueing another.")
        return

    interval = float(state.get("scan_interval_hours") or 1.0)
    if interval < 0.25:
        _queue_next_scheduled_probe(interval, state=state, force=True)
    else:
        _clear_queued_scheduled_probe(state)
        save_state(state)


def _format_probe_session_summary(probe_log: list[dict], tz) -> str:
    """Format a detailed Telegram summary of the 8am probe session."""
    lines = [f"8am probe session: {len(probe_log)} probes"]
    for entry in probe_log:
        ts = entry.get("ts", "")
        try:
            from datetime import timezone as _tz
            dt = datetime.fromisoformat(ts).astimezone(tz)
            time_str = dt.strftime("%H:%M:%S")
        except Exception:
            time_str = ts[11:19] if len(ts) >= 19 else ts
        phase = entry.get("phase", "?")
        result = entry.get("result", "?")
        prefix = f"  {time_str} [{phase}] {result}"
        if entry.get("booked"):
            lines.append(f"{prefix} — {', '.join(entry['booked'])}")
        elif entry.get("open"):
            lines.append(f"{prefix} — open: {', '.join(entry['open'])}")
        elif result == "error":
            lines.append(f"{prefix}: {str(entry.get('error', ''))[:80]}")
        elif result == "already_reserved":
            lines.append(f"{prefix}: {entry.get('slot', '')}")
        else:
            lines.append(prefix)
        for attempt in entry.get("booking_attempts") or []:
            court = attempt.get("court", "?")
            a_result = attempt.get("result", "?")
            a_num = attempt.get("attempt", "")
            err = attempt.get("error", "")
            detail = f"    → Court {court} attempt {a_num}: {a_result}"
            if err:
                detail += f" ({err[:60]})"
            lines.append(detail)
    return "\n".join(lines)


def _run_release_probe_session() -> None:
    """Own the 7:58–8:02 AM slot-release window.

    Phases:
      pre  (7:58–8:00):   probe only the new weekend 9:00 AM / 8:00 AM target every 15s
      burst (8:00–8:00:30): back-to-back probes for the new weekend 9:00 AM slot
      post (8:00:30–8:02): probe only the new weekend 9:00 AM / 8:00 AM target every 15s
    """
    import time as _time

    now_pt = datetime.now(tz=PT)
    eight_am = now_pt.replace(hour=8, minute=0, second=0, microsecond=0)

    new_day = now_pt.date() + timedelta(days=14)
    new_day_targets: dict[str, list[str]] = {}
    if new_day.weekday() >= 5:
        new_day_targets[new_day.isoformat()] = ["9:00 AM", "8:00 AM"]
    burst_target: tuple[str, str] | None = (
        (new_day.isoformat(), "9:00 AM") if new_day.weekday() >= 5 else None
    )
    burst_done = False

    state = load_state()
    try:
        jwt = _get_cached_jwt(state) or _firebase_login()
        if not state.get("cached_jwt"):
            _cache_jwt(state, jwt)
            save_state(state)
        print(f"Release probe session: logged in, JWT expires {state.get('cached_jwt_expires_at', '?')}")
        if burst_target:
            print(f"  Burst target: {burst_target[0]} {burst_target[1]}")
    except Exception as exc:
        try:
            send_telegram(f"❌ Release probe login failed: {exc}")
        except Exception:
            pass
        return

    state = load_state()
    if _active_scan_started_at(state):
        print("Release probe session: another scan in progress, skipping.")
        return
    started_at = _utc_now_iso()
    state["scan_started_at"] = started_at
    state["scan_started_kind"] = "release_probe"
    save_state(state)

    probe_log: list[dict] = []

    def _one_probe(phase: str, targets_override: dict | None = None) -> list[dict]:
        ts = _utc_now_iso()
        st = load_state()
        auto_book_slots = st.get("auto_book_slots") or []
        targets = targets_override if targets_override is not None else new_day_targets
        booking_attempts: list[dict] = []
        if not targets:
            probe_log.append({"ts": ts, "phase": phase, "result": "no_targets", "booking_attempts": booking_attempts})
            print(f"  [{phase}] no targets")
            return []
        try:
            new_avail, booked = _api_scan(
                target_times_by_date=targets,
                auto_book_slots=auto_book_slots,
                jwt=jwt,
                detailed_log=booking_attempts,
            )
            st = load_state()
            avail = st.get("availability", {})
            for d, tm in new_avail.items():
                day = avail.get(d, {})
                for t, courts in tm.items():
                    day[t] = _normalize_time_availability(courts)
                avail[d] = day
            st["availability"] = avail
            open_slots = [
                f"{d} {t}"
                for d, tm in new_avail.items()
                for t, courts in tm.items()
                if any(v is True for v in courts.values())
            ]
            entry: dict = {
                "ts": ts,
                "phase": phase,
                "result": "booked" if booked else ("open" if open_slots else "empty"),
                "booking_attempts": booking_attempts,
            }
            if booked:
                entry["booked"] = [f"{b['date']} {b['time']} Court {b['court']}" for b in booked]
            if open_slots:
                entry["open"] = open_slots
            probe_log.append(entry)
            print(
                f"  [{phase}] {entry['result']}"
                + (f" — {entry.get('booked') or entry.get('open')}" if entry["result"] != "empty" else "")
            )
            _apply_booked_slots(st, booked)
            save_state(st)
            _notify_booked_slots(booked)
            return booked
        except Exception as exc:
            probe_log.append({
                "ts": ts,
                "phase": phase,
                "result": "error",
                "error": str(exc),
                "booking_attempts": booking_attempts,
            })
            print(f"  [{phase}] error: {exc}")
            return []

    def _burst_slot_already_reserved(burst_date: str, burst_time: str) -> bool:
        st = load_state()
        return any(
            r.get("date") == burst_date and r.get("time") == burst_time
            for r in (st.get("my_reservations") or [])
        )

    try:
        while True:
            now = datetime.now(tz=PT)
            secs = (now - eight_am).total_seconds()

            if secs >= _RELEASE_END_S:
                break

            in_burst_window = 0 <= secs < _RELEASE_BURST_UNTIL_S

            if in_burst_window and burst_target and not burst_done:
                burst_date, burst_time = burst_target
                if _burst_slot_already_reserved(burst_date, burst_time):
                    burst_done = True
                    probe_log.append({
                        "ts": _utc_now_iso(), "phase": "burst",
                        "result": "already_reserved",
                        "slot": f"{burst_date} {burst_time}",
                    })
                    print(f"  [burst] {burst_date} {burst_time} already reserved — stopping burst")
                else:
                    booked = _one_probe("burst", targets_override={burst_date: [burst_time]})
                    if any(b.get("date") == burst_date and b.get("time") == burst_time for b in booked):
                        burst_done = True
                    continue

            phase = "pre" if secs < 0 else "post"
            interval = _RELEASE_PRE_INTERVAL_S if secs < 0 else _RELEASE_POST_INTERVAL_S
            t0 = _time.monotonic()
            _one_probe(phase)
            elapsed = _time.monotonic() - t0
            _time.sleep(max(0.0, interval - elapsed))

    finally:
        state = load_state()
        if state.get("scan_started_at") == started_at:
            state.pop("scan_started_at", None)
            state.pop("scan_started_kind", None)
        existing = state.get("release_probe_log") or []
        state["release_probe_log"] = (existing + probe_log)[-500:]
        state["last_release_probe_session"] = started_at
        save_state(state)
        print(f"8am session done: {len(probe_log)} probes")
        try:
            send_telegram(_format_probe_session_summary(probe_log, PT))
        except Exception as exc:
            print(f"Failed to send probe session summary: {exc}")


def _queue_release_probe_session_if_needed(state: dict, now_pt: datetime | None = None) -> bool:
    """At 7:45 AM on weekends: cache JWT now, then queue the probe session to start at ~7:58 AM."""
    from config import WORK_QUEUE_URL
    now_pt = now_pt or datetime.now(tz=PT)
    if now_pt.hour != 7 or now_pt.minute != 45:
        return False
    if now_pt.weekday() >= 5:
        # Pre-cache JWT at 7:45 AM so the probe session skips login entirely.
        try:
            if not _get_cached_jwt(state):
                jwt = _firebase_login()
                _cache_jwt(state, jwt)
                save_state(state)
                print("Pre-cached JWT at 7:45 AM for 8 AM probe session.")
        except Exception as exc:
            print(f"7:45 AM JWT pre-cache failed: {exc}")
    if not WORK_QUEUE_URL:
        print("Work queue not configured; cannot queue release probe session.")
        return False
    today_str = now_pt.date().isoformat()
    if now_pt.weekday() < 5:
        print("Skipping release probe session: weekday.")
        return False
    if state.get("release_probe_session_date") == today_str:
        print(f"Release probe session already queued for {today_str}.")
        return True
    if not _watched_and_auto_book_targets(state):
        print("No watched/auto-book slots — skipping release probe session.")
        return False
    state["release_probe_session_date"] = today_str
    save_state(state)
    queued = bool(_enqueue_work("release_probe_session", {}, delay_seconds=780))
    if queued:
        print(f"Queued release probe session for ~7:58 AM PT ({today_str}).")
    return queued


def _run_queue_work(message: dict) -> None:
    kind = message.get("kind")
    if kind == "scheduled_probe":
        _run_queued_scheduled_probe(str(message.get("token") or ""))
    elif kind == "release_probe_session":
        _run_release_probe_session()
    elif kind == "booking_agent_prep_retry":
        from booking_agent import run_agent
        attempt = int(message.get("attempt") or 1)
        run_agent("prep", retry_attempt=attempt)
    elif kind == "calendar_event":
        from calendar_sync import handle_calendar_event_work
        handle_calendar_event_work(message.get("slot") or {})
    else:
        print(f"Ignoring unknown queue work kind: {kind}")


def _is_sqs_event(event: dict) -> bool:
    records = (event or {}).get("Records") or []
    return bool(records) and all(record.get("eventSource") == "aws:sqs" for record in records)


def _handle_queue_event(event: dict) -> None:
    import json
    for record in event.get("Records") or []:
        try:
            message = json.loads(record.get("body") or "{}")
        except json.JSONDecodeError:
            print("Ignoring queue item with invalid JSON body.")
            continue
        _run_queue_work(message)
