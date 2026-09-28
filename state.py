from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from config import (
    BOT_HISTORY_PREFIX, BOT_MAX_HISTORY_TURNS, COURT_PREFERENCE, PT, SCAN_HISTORY_MAX,
    SCAN_LOCK_TTL_SECONDS, SLOT_TIMES, SQS_DELAY_MAX_SECONDS, SQS_STALE_GRACE_SECONDS,
    STATE_BUCKET, STATE_KEY, TARGET_COURTS, TELEGRAM_USAGE_KEY, TELEGRAM_USAGE_MAX,
    WEEKEND_TARGET_TIMES, WORK_QUEUE_URL, _time_text_to_hhmm,
)


def _empty_state() -> dict:
    return {
        "watched_slots":       [],
        "watched_slots_updated_at": None,
        "my_reservations":     [],
        "my_reservations_synced_at": None,
        "my_reservations_source": None,
        "friend_reservations": [],
        "friend_reservations_updated_at": None,
        "availability":        {},
        "notified_slots":      [],
        "last_scanned":        None,
        "last_scan_started_at": None,
        "last_scan_kind":      None,
        "recent_scan_history": [],
        "scan_started_kind":   None,
        "scan_interval_hours": 1.0,
        "queued_scheduled_probe_at": None,
        "queued_scheduled_probe_token": None,
        "queued_publish_probe_date": None,
        "auto_book_slots":     [],
        "app_booking_log":     [],
        "booking_history":     [],
        "weekend_target_prefilled_dates": [],
        "auto_book_failures":  [],
        "seen_open_days":      {},
        "cached_jwt":              None,
        "cached_jwt_expires_at":   None,
        "release_probe_session_date": None,
        "last_release_probe_session": None,
        "release_probe_log":       [],
    }


def _empty_court_availability(default=None) -> dict[str, bool | None]:
    return {court: default for court in TARGET_COURTS}


def _normalize_court_number(value) -> str | None:
    import re
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


def _auto_book_slot_is_too_close(slot_date: str, slot_time: str, *, now: datetime | None = None) -> bool:
    hhmm = _time_text_to_hhmm(slot_time)
    if not hhmm:
        return False
    try:
        slot_day = date.fromisoformat(slot_date)
    except ValueError:
        return False
    hour, minute = (int(part) for part in hhmm.split(":"))
    slot_start = datetime(
        slot_day.year,
        slot_day.month,
        slot_day.day,
        hour,
        minute,
        tzinfo=PT,
    )
    now_pt = now or datetime.now(tz=PT)
    return now_pt > slot_start - timedelta(hours=32)


def _normalize_slot_records(
    slots,
    *,
    expand_legacy: bool,
    default_court: str | None = None,
    preserve_fields: tuple[str, ...] = (),
) -> list[dict]:
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
            record = {"date": slot_date, "time": slot_time, "court": court_num}
            for field in preserve_fields:
                if slot.get(field) is not None:
                    record[field] = slot.get(field)
            normalized.append(record)
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
    normalized["watched_slots_updated_at"] = state.get("watched_slots_updated_at")
    normalized["last_scanned"] = state.get("last_scanned")
    normalized["last_scan_started_at"] = state.get("last_scan_started_at")
    normalized["last_scan_kind"] = state.get("last_scan_kind") or None
    recent_scan_history = []
    history_cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    for entry in (state.get("recent_scan_history") or [])[:SCAN_HISTORY_MAX]:
        if not isinstance(entry, dict):
            continue
        cmp_ts = entry.get("completed_at") or entry.get("started_at") or ""
        if cmp_ts and cmp_ts < history_cutoff:
            continue
        targets = []
        for target in (entry.get("targets") or []):
            if not isinstance(target, dict):
                continue
            target_date = target.get("date")
            times = [t for t in (target.get("times") or []) if t in SLOT_TIMES]
            if not target_date or not times:
                continue
            raw_courts = target.get("courts") if isinstance(target.get("courts"), dict) else {}
            courts: dict[str, dict[str, bool | None]] = {}
            for time_text in times:
                court_avail = raw_courts.get(time_text)
                if isinstance(court_avail, dict):
                    normalized_courts: dict[str, bool | None] = {}
                    for court in COURT_PREFERENCE:
                        raw = court_avail.get(court)
                        normalized_courts[court] = None if raw is None else bool(raw)
                    courts[time_text] = normalized_courts
            target_entry = {"date": target_date, "times": times}
            if courts:
                target_entry["courts"] = courts
            targets.append(target_entry)
        recent_scan_history.append({
            "kind": entry.get("kind") or None,
            "started_at": entry.get("started_at"),
            "completed_at": entry.get("completed_at"),
            "status": entry.get("status") or "completed",
            "error": entry.get("error"),
            "targets": targets,
        })
    normalized["recent_scan_history"] = recent_scan_history
    normalized["scan_interval_hours"] = state.get("scan_interval_hours", 1.0)
    normalized["queued_scheduled_probe_at"] = state.get("queued_scheduled_probe_at")
    normalized["queued_scheduled_probe_token"] = state.get("queued_scheduled_probe_token")
    normalized["queued_publish_probe_date"] = state.get("queued_publish_probe_date")
    normalized["release_probe_session_date"] = state.get("release_probe_session_date")
    normalized["last_release_probe_session"] = state.get("last_release_probe_session")
    if state.get("scan_started_at"):
        normalized["scan_started_at"] = state["scan_started_at"]
        normalized["scan_started_kind"] = state.get("scan_started_kind") or None

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
        preserve_fields=("account_index", "account_email", "booking_id", "reservation_id", "facility_rental_id"),
    )
    normalized["my_reservations_synced_at"] = state.get("my_reservations_synced_at")
    normalized["my_reservations_source"] = state.get("my_reservations_source")
    normalized["friend_reservations"] = _normalize_slot_records(
        state.get("friend_reservations"),
        expand_legacy=False,
        default_court=COURT_PREFERENCE[0],
    )
    normalized["friend_reservations_updated_at"] = state.get("friend_reservations_updated_at")
    mine_keys = {(s["date"], s["time"], s["court"]) for s in normalized["my_reservations"]}
    normalized["friend_reservations"] = [
        s for s in normalized["friend_reservations"]
        if (s["date"], s["time"], s["court"]) not in mine_keys
    ]
    normalized["notified_slots"] = _normalize_notified_slots(state.get("notified_slots"))
    today_str = date.today().isoformat()
    raw_seen = state.get("seen_open_days") or {}
    if isinstance(raw_seen, list):
        raw_seen = {d: None for d in raw_seen}
    normalized["seen_open_days"] = {
        d: ts for d, ts in raw_seen.items()
        if isinstance(d, str) and d >= today_str
    }
    auto_book_slots = []
    seen_ab: set[tuple[str, str]] = set()
    for slot in (state.get("auto_book_slots") or []):
        if not isinstance(slot, dict):
            continue
        slot_date = slot.get("date")
        slot_time = slot.get("time")
        if not slot_date or not slot_time:
            continue
        if slot_date < today_str or slot_time not in SLOT_TIMES:
            continue
        if _auto_book_slot_is_too_close(slot_date, slot_time):
            continue
        key = (slot_date, slot_time)
        if key in seen_ab:
            continue
        seen_ab.add(key)
        try:
            count = max(1, min(3, int(slot.get("count") or 1)))
        except (TypeError, ValueError):
            count = 1
        entry = {"date": slot_date, "time": slot_time}
        if count > 1:
            entry["count"] = count
        auto_book_slots.append(entry)
    normalized["auto_book_slots"] = auto_book_slots
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=25)).isoformat()
    normalized["app_booking_log"] = [
        e for e in (state.get("app_booking_log") or [])
        if isinstance(e, dict) and e.get("booked_at", "") >= cutoff
    ]
    normalized["booking_history"] = _normalize_booking_history(state.get("booking_history"))
    normalized["weekend_target_prefilled_dates"] = sorted({
        d for d in (state.get("weekend_target_prefilled_dates") or [])
        if isinstance(d, str) and d >= today_str
    })
    normalized["release_probe_log"] = [
        e for e in (state.get("release_probe_log") or [])
        if isinstance(e, dict)
    ][-500:]
    cutoff_48h = (datetime.now(tz=timezone.utc) - timedelta(hours=48)).isoformat()
    normalized["auto_book_failures"] = [
        e for e in (state.get("auto_book_failures") or [])
        if isinstance(e, dict) and e.get("failed_at", "") >= cutoff_48h
    ][:20]
    return normalized


def _normalize_booking_history(history) -> list[dict]:
    normalized: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        slot_date = entry.get("date")
        slot_time = entry.get("time")
        court = _normalize_court_number(entry.get("court"))
        if not slot_date or slot_time not in SLOT_TIMES or not court:
            continue
        account = str(entry.get("account_index") or "")
        key = (slot_date, slot_time, court, account)
        if key in seen:
            continue
        seen.add(key)
        record = {
            "date": slot_date,
            "weekday": entry.get("weekday") or _weekday_name(slot_date),
            "time": slot_time,
            "court": court,
            "source": entry.get("source") or "rec_us_sync",
            "first_seen_at": entry.get("first_seen_at") or entry.get("last_seen_at") or _utc_now_iso(),
            "last_seen_at": entry.get("last_seen_at") or entry.get("first_seen_at") or _utc_now_iso(),
        }
        if account:
            record["account_index"] = account
        normalized.append(record)
    return sorted(normalized, key=lambda e: (e["date"], e["time"], e["court"]))[-1000:]


def _weekday_name(date_str: str) -> str:
    try:
        return date.fromisoformat(date_str).strftime("%A")
    except ValueError:
        return ""


def record_booking_history_from_reservations(state: dict) -> bool:
    existing: dict[tuple[str, str, str, str], dict] = {}
    for entry in _normalize_booking_history(state.get("booking_history")):
        key = (
            entry["date"],
            entry["time"],
            entry["court"],
            str(entry.get("account_index") or ""),
        )
        existing[key] = entry

    changed = False
    now = _utc_now_iso()
    for slot in state.get("my_reservations") or []:
        if not isinstance(slot, dict):
            continue
        slot_date = slot.get("date")
        slot_time = slot.get("time")
        court = _normalize_court_number(slot.get("court"))
        if not slot_date or slot_time not in SLOT_TIMES or not court:
            continue
        account = str(slot.get("account_index") or "")
        key = (slot_date, slot_time, court, account)
        if key in existing:
            existing[key]["last_seen_at"] = now
            changed = True
        else:
            record = {
                "date": slot_date,
                "weekday": _weekday_name(slot_date),
                "time": slot_time,
                "court": court,
                "source": "rec_us_sync",
                "first_seen_at": now,
                "last_seen_at": now,
            }
            if account:
                record["account_index"] = account
            existing[key] = record
            changed = True

    state["booking_history"] = sorted(existing.values(), key=lambda e: (e["date"], e["time"], e["court"]))[-1000:]
    return changed


def _history_targets_from_map(targets_by_date: dict[str, list[str]]) -> list[dict]:
    normalized_targets = []
    for date_str, times in sorted(targets_by_date.items()):
        ordered_times = [t for t in SLOT_TIMES if t in set(times)]
        if ordered_times:
            normalized_targets.append({"date": date_str, "times": ordered_times})
    return normalized_targets


def _attach_history_results(targets: list[dict], new_avail: dict) -> list[dict]:
    enriched = []
    for target in targets:
        date_str = target.get("date")
        times = target.get("times") or []
        new_target = {"date": date_str, "times": list(times)}
        day = (new_avail or {}).get(date_str) or {}
        courts: dict[str, dict[str, bool | None]] = {}
        for time_text in times:
            court_avail = day.get(time_text)
            if not isinstance(court_avail, dict):
                continue
            courts[time_text] = {
                c: (None if court_avail.get(c) is None else bool(court_avail.get(c)))
                for c in COURT_PREFERENCE
            }
        if courts:
            new_target["courts"] = courts
        enriched.append(new_target)
    return enriched


def _record_scan_history(
    state: dict,
    *,
    kind: str,
    started_at: str | None,
    completed_at: str | None,
    status: str,
    targets: list[dict],
    error: str | None = None,
) -> None:
    history = list(state.get("recent_scan_history") or [])
    history.insert(0, {
        "kind": kind,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "error": error,
        "targets": targets,
    })
    state["recent_scan_history"] = history[:SCAN_HISTORY_MAX]


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
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _has_future_watched_slots(state: dict) -> bool:
    today_str = date.today().isoformat()
    return any(
        slot.get("date", "") >= today_str
        for slot in state.get("watched_slots", [])
    )


def _record_newly_open_dates(state: dict, new_avail: dict, ts: str | None = None) -> None:
    """Stamp seen_open_days with ts for any date that first appears as open in new_avail."""
    raw_seen = state.get("seen_open_days") or {}
    if isinstance(raw_seen, list):
        raw_seen = {d: None for d in raw_seen}
    stamp = ts or _utc_now_iso()
    today_str = date.today().isoformat()
    for date_str, time_map in new_avail.items():
        if date_str < today_str or date_str in raw_seen:
            continue
        any_open = any(
            v is True
            for court_map in time_map.values()
            for v in (court_map.values() if isinstance(court_map, dict) else [])
        )
        if any_open:
            raw_seen[date_str] = stamp
    state["seen_open_days"] = raw_seen


def _new_day_iso(today: date | None = None) -> str:
    return ((today or date.today()) + timedelta(days=14)).isoformat()


def _weekend_target_checked_times(state: dict, date_str: str) -> tuple[str, ...]:
    """Times in WEEKEND_TARGET_TIMES currently checked ($ auto-book) for date_str."""
    checked = {
        ab.get("time") for ab in (state.get("auto_book_slots") or [])
        if ab.get("date") == date_str
    }
    return tuple(t for t in WEEKEND_TARGET_TIMES if t in checked)


def _weekend_target_watched_times(state: dict, date_str: str) -> tuple[str, ...]:
    """Times in WEEKEND_TARGET_TIMES currently watched (\U0001f441️, any court) for date_str."""
    watched = {
        s.get("time") for s in (state.get("watched_slots") or [])
        if s.get("date") == date_str
    }
    return tuple(t for t in WEEKEND_TARGET_TIMES if t in watched)


def _is_weekend_target_prefilled(state: dict, date_str: str) -> bool:
    return date_str in set(state.get("weekend_target_prefilled_dates") or [])


def _mark_weekend_target_prefilled(state: dict, date_str: str) -> None:
    dates = set(state.get("weekend_target_prefilled_dates") or [])
    dates.add(date_str)
    state["weekend_target_prefilled_dates"] = sorted(dates)


def _set_weekend_target_defaults(
    state: dict, date_str: str, *, auto_book_times: list[str], watch_times: list[str],
) -> None:
    """Write default auto-book/watch state for date_str and mark it prefilled.

    Only ever called for a date that has not been touched yet (checked via
    _is_weekend_target_prefilled) so it never overwrites a user's own edits.
    """
    if auto_book_times:
        existing = {(ab["date"], ab["time"]) for ab in (state.get("auto_book_slots") or [])}
        additions = [
            {"date": date_str, "time": t}
            for t in auto_book_times
            if (date_str, t) not in existing
        ]
        state["auto_book_slots"] = (state.get("auto_book_slots") or []) + additions
    if watch_times:
        new_slots = [
            {"date": date_str, "time": t, "court": court}
            for t in watch_times
            for court in COURT_PREFERENCE
        ]
        state["watched_slots"] = _normalize_slot_records(
            (state.get("watched_slots") or []) + new_slots,
            expand_legacy=False,
        )
        state["watched_slots_updated_at"] = _utc_now_iso()
    _mark_weekend_target_prefilled(state, date_str)


def _enqueue_work(kind: str, payload: dict | None = None, *, delay_seconds: int = 0) -> bool:
    if not WORK_QUEUE_URL:
        print(f"Work queue is not configured; cannot enqueue {kind}.")
        return False
    delay = max(0, min(int(delay_seconds), SQS_DELAY_MAX_SECONDS))
    body = {"kind": kind, **(payload or {})}
    boto3.client("sqs", region_name="us-west-2").send_message(
        QueueUrl=WORK_QUEUE_URL,
        DelaySeconds=delay,
        MessageBody=json.dumps(body),
    )
    print(f"Queued {kind}" + (f" in {delay}s." if delay else "."))
    return True


# ── Chat history (S3) ─────────────────────────────────────────────────────────

def _message_content_blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _chat_message_has_tool_use(message: dict) -> bool:
    return any(block.get("type") == "tool_use" for block in _message_content_blocks(message))


def _chat_message_has_tool_result(message: dict) -> bool:
    return any(block.get("type") == "tool_result" for block in _message_content_blocks(message))


def _sanitize_chat_history(history: list[dict]) -> list[dict]:
    sanitized: list[dict] = []
    for message in history:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        if role == "user" and _chat_message_has_tool_result(message):
            if not sanitized or sanitized[-1].get("role") != "assistant" or not _chat_message_has_tool_use(sanitized[-1]):
                continue
        sanitized.append(message)
    while sanitized and sanitized[0].get("role") == "user" and _chat_message_has_tool_result(sanitized[0]):
        sanitized.pop(0)
    return sanitized


def _load_chat_history(chat_id: str) -> list[dict]:
    if not STATE_BUCKET:
        return []
    s3 = boto3.client("s3", region_name="us-west-2")
    try:
        obj = s3.get_object(Bucket=STATE_BUCKET, Key=BOT_HISTORY_PREFIX + chat_id + ".json")
        data = json.loads(obj["Body"].read())
        return _sanitize_chat_history(data) if isinstance(data, list) else []
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return []
        raise


def _save_chat_history(chat_id: str, history: list[dict]) -> None:
    if not STATE_BUCKET:
        return
    trimmed = _sanitize_chat_history(history[-(BOT_MAX_HISTORY_TURNS * 2):])
    s3 = boto3.client("s3", region_name="us-west-2")
    s3.put_object(
        Bucket=STATE_BUCKET,
        Key=BOT_HISTORY_PREFIX + chat_id + ".json",
        Body=json.dumps(trimmed),
        ContentType="application/json",
    )


def _append_notification_to_history(chat_id: str, message: str) -> None:
    """Save a proactive bot notification to chat history so follow-up replies have context."""
    if not chat_id:
        return
    try:
        history = _load_chat_history(chat_id)
        if history and history[-1]["role"] == "assistant":
            prev = history[-1]["content"]
            if isinstance(prev, str):
                history[-1]["content"] = prev + "\n\n" + message
            else:
                history[-1]["content"] = list(prev) + [{"type": "text", "text": message}]
        else:
            history.append({"role": "assistant", "content": message})
        _save_chat_history(chat_id, history)
    except Exception as exc:
        print(f"Failed to save notification to history: {exc}")


def _load_telegram_usage() -> list[dict]:
    if not STATE_BUCKET:
        return []
    s3 = boto3.client("s3", region_name="us-west-2")
    try:
        obj = s3.get_object(Bucket=STATE_BUCKET, Key=TELEGRAM_USAGE_KEY)
        data = json.loads(obj["Body"].read())
        return data if isinstance(data, list) else []
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return []
        raise


def _record_telegram_usage(record: dict) -> None:
    if not STATE_BUCKET:
        return
    s3 = boto3.client("s3", region_name="us-west-2")
    try:
        obj = s3.get_object(Bucket=STATE_BUCKET, Key=TELEGRAM_USAGE_KEY)
        history = json.loads(obj["Body"].read())
        if not isinstance(history, list):
            history = []
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            history = []
        else:
            raise
    history.insert(0, record)
    s3.put_object(
        Bucket=STATE_BUCKET,
        Key=TELEGRAM_USAGE_KEY,
        Body=json.dumps(history[:TELEGRAM_USAGE_MAX]),
        ContentType="application/json",
    )
