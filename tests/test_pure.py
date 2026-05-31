"""Unit tests for pure functions that require no external dependencies."""
import os
import sys
from datetime import date, datetime, timezone

os.environ.setdefault("EMAIL", "x")
os.environ.setdefault("PASSWORD", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from config import _time_text_to_hhmm, _HHMM_TO_TIME_TEXT, build_next_dates, SLOT_TIMES
from state import (
    _normalize_court_number, _normalize_time_availability, _empty_court_availability,
    _normalize_notified_slots, _sanitize_chat_history, _auto_book_slot_is_too_close,
)
from http_utils import get_path, get_method, summarize_results
from message_format import with_weekday_dates
from notify import ordinal, _ordered_open_courts
from scheduler import _should_run_scheduled_tick
from scanner import _auto_book_priority_key
from calendar_sync import _calendar_event_body, _calendar_event_id


# ── config ────────────────────────────────────────────────────────────────────

def test_time_text_to_hhmm():
    assert _time_text_to_hhmm("9:00 AM") == "09:00"
    assert _time_text_to_hhmm("8:00 AM") == "08:00"
    assert _time_text_to_hhmm("4:00 PM") == "16:00"
    assert _time_text_to_hhmm("invalid") == ""


def test_hhmm_to_time_text_roundtrip():
    for t in SLOT_TIMES:
        hhmm = _time_text_to_hhmm(t)
        assert _HHMM_TO_TIME_TEXT.get(hhmm) == t


def test_build_next_dates():
    start = date(2026, 1, 1)
    result = build_next_dates(3, start=start)
    assert result == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]


def test_build_next_dates_default_start():
    result = build_next_dates(1)
    assert result == [date.today()]


# ── state ─────────────────────────────────────────────────────────────────────

def test_normalize_court_number_valid():
    assert _normalize_court_number("6") == "6"
    assert _normalize_court_number("4") == "4"
    assert _normalize_court_number("5") == "5"


def test_normalize_court_number_string_prefix():
    assert _normalize_court_number("Court 6") == "6"
    assert _normalize_court_number("court 4") == "4"


def test_normalize_court_number_invalid():
    assert _normalize_court_number("9") is None
    assert _normalize_court_number(None) is None
    assert _normalize_court_number("abc") is None


def test_normalize_time_availability_dict():
    result = _normalize_time_availability({"6": True, "4": False, "5": None})
    assert result["6"] is True
    assert result["4"] is False
    assert result["5"] is None


def test_normalize_time_availability_false():
    result = _normalize_time_availability(False)
    assert all(v is False for v in result.values())


def test_normalize_time_availability_true():
    result = _normalize_time_availability(True)
    assert all(v is None for v in result.values())


def test_normalize_notified_slots_three_parts():
    result = _normalize_notified_slots(["2026-06-01|9:00 AM|6"])
    assert result == ["2026-06-01|9:00 AM|6"]


def test_normalize_notified_slots_deduplication():
    result = _normalize_notified_slots(["2026-06-01|9:00 AM|6", "2026-06-01|9:00 AM|6"])
    assert len(result) == 1


def test_auto_book_slot_is_too_close_uses_32_hour_cutoff():
    now = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    assert not _auto_book_slot_is_too_close("2026-06-02", "9:00 AM", now=now)
    assert _auto_book_slot_is_too_close("2026-06-02", "8:00 AM", now=now)


def test_sanitize_chat_history_drops_orphan_tool_result():
    history = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}"}]},
        {"role": "user", "content": "hello"},
    ]
    assert _sanitize_chat_history(history) == [{"role": "user", "content": "hello"}]


def test_sanitize_chat_history_keeps_matched_tool_result():
    history = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "scan_now", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}"}]},
    ]
    assert _sanitize_chat_history(history) == history


def test_normalize_state_strips_too_close_auto_book_slots(monkeypatch):
    import state as state_mod

    monkeypatch.setattr(state_mod, "_auto_book_slot_is_too_close", lambda slot_date, slot_time, now=None: slot_time == "9:00 AM")
    normalized = state_mod._normalize_state({
        "auto_book_slots": [
            {"date": "2026-06-01", "time": "9:00 AM"},
            {"date": "2026-06-01", "time": "10:00 AM"},
        ]
    })
    assert normalized["auto_book_slots"] == [{"date": "2026-06-01", "time": "10:00 AM"}]


def test_normalize_state_preserves_release_probe_fields():
    import state as state_mod

    normalized = state_mod._normalize_state({
        "release_probe_session_date": "2026-05-30",
        "last_release_probe_session": "2026-05-30T15:58:14.000Z",
        "release_probe_log": [
            {"ts": "2026-05-30T15:58:14.000Z", "phase": "burst", "result": "open"}
        ],
    })
    assert normalized["release_probe_session_date"] == "2026-05-30"
    assert normalized["last_release_probe_session"] == "2026-05-30T15:58:14.000Z"
    assert normalized["release_probe_log"] == [
        {"ts": "2026-05-30T15:58:14.000Z", "phase": "burst", "result": "open"}
    ]


# ── http_utils ────────────────────────────────────────────────────────────────

def test_get_path_strips_stage():
    event = {"requestContext": {"http": {"path": "/prod/scan"}, "stage": "prod"}}
    assert get_path(event) == "/scan"

def test_get_path_default():
    assert get_path({}) == "/"

def test_get_method_http_v2():
    event = {"requestContext": {"http": {"method": "PUT"}}}
    assert get_method(event) == "PUT"

def test_get_method_default():
    assert get_method({}) == "GET"

def test_summarize_results_empty():
    result = summarize_results({})
    assert result["days_with_availability"] == 0
    assert result["slots_found"] == 0

def test_summarize_results():
    data = {"Monday": [{"time": "9:00 AM"}, {"time": "10:00 AM"}], "Tuesday": [{"time": "9:00 AM"}]}
    result = summarize_results(data)
    assert result["days_with_availability"] == 2
    assert result["slots_found"] == 3


# ── notify ────────────────────────────────────────────────────────────────────

def test_ordinal():
    assert ordinal(1) == "1st"
    assert ordinal(2) == "2nd"
    assert ordinal(3) == "3rd"
    assert ordinal(4) == "4th"
    assert ordinal(11) == "11th"
    assert ordinal(12) == "12th"
    assert ordinal(13) == "13th"
    assert ordinal(21) == "21st"


def test_ordered_open_courts_preference_order():
    avail = {"6": True, "4": True, "5": False}
    result = _ordered_open_courts(avail)
    assert result == ["6", "4"]


def test_ordered_open_courts_none_available():
    avail = {"6": False, "4": False, "5": False}
    assert _ordered_open_courts(avail) == []


def test_auto_book_priority_prefers_release_day_and_9am():
    preferred_date = date(2026, 6, 13)
    items = [
        ("2026-06-13", "8:00 AM"),
        ("2026-06-13", "9:00 AM"),
        ("2026-06-14", "9:00 AM"),
    ]
    result = sorted(
        items,
        key=lambda item: _auto_book_priority_key(item[0], item[1], preferred_date=preferred_date),
    )
    assert result == [
        ("2026-06-13", "9:00 AM"),
        ("2026-06-13", "8:00 AM"),
        ("2026-06-14", "9:00 AM"),
    ]


def test_release_probe_auto_books_preferred_release_day_first(monkeypatch):
    import scanner as scanner_mod

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 30)

    monkeypatch.setattr(scanner_mod, "date", FixedDate)

    state = {"my_reservations": [], "app_booking_log": [], "auto_book_failures": []}
    saved_states = []

    def fake_save_state(value):
        saved_states.append({
            "app_booking_log": [
                {
                    "date": entry["date"],
                    "time": entry["time"],
                    "court": entry["court"],
                }
                for entry in (value.get("app_booking_log") or [])
            ],
        })

    def fake_fetch_availability(target_times_by_date=None):
        return {
            "2026-06-13": {
                "8:00 AM": {"6": True, "4": False, "5": False},
                "9:00 AM": {"6": True, "4": False, "5": False},
            },
            "2026-06-14": {
                "8:00 AM": {"6": True, "4": False, "5": False},
                "9:00 AM": {"6": True, "4": False, "5": False},
            },
            "2026-06-15": {
                "8:00 AM": {"6": True, "4": False, "5": False},
                "9:00 AM": {"6": True, "4": False, "5": False},
            },
        }

    booked_calls = []

    monkeypatch.setattr(scanner_mod, "load_state", lambda: state)
    monkeypatch.setattr(scanner_mod, "save_state", fake_save_state)
    monkeypatch.setattr(scanner_mod, "_api_fetch_availability", fake_fetch_availability)
    monkeypatch.setattr(scanner_mod, "_recent_booking_count", lambda state: 0)
    monkeypatch.setattr(scanner_mod, "book_slot_api", lambda jwt, slot_date, time_text, court: booked_calls.append(
        (slot_date.isoformat(), time_text, court)
    ) or True)
    monkeypatch.setattr(scanner_mod, "send_telegram", lambda msg: None)

    new_avail, booked = scanner_mod._api_scan(
        auto_book_slots=[
            {"date": "2026-06-13", "time": "8:00 AM"},
            {"date": "2026-06-13", "time": "9:00 AM"},
            {"date": "2026-06-14", "time": "8:00 AM"},
            {"date": "2026-06-14", "time": "9:00 AM"},
            {"date": "2026-06-15", "time": "8:00 AM"},
            {"date": "2026-06-15", "time": "9:00 AM"},
        ],
        jwt="test-jwt",
    )

    assert booked == [
        {"date": "2026-06-13", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-13", "time": "8:00 AM", "court": "6"},
        {"date": "2026-06-14", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-14", "time": "8:00 AM", "court": "6"},
        {"date": "2026-06-15", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-15", "time": "8:00 AM", "court": "6"},
    ]
    assert booked_calls == [
        ("2026-06-13", "9:00 AM", "6"),
        ("2026-06-13", "8:00 AM", "6"),
        ("2026-06-14", "9:00 AM", "6"),
        ("2026-06-14", "8:00 AM", "6"),
        ("2026-06-15", "9:00 AM", "6"),
        ("2026-06-15", "8:00 AM", "6"),
    ]
    assert [entry["app_booking_log"][0] for entry in saved_states] == [
        {"date": "2026-06-13", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-13", "time": "8:00 AM", "court": "6"},
        {"date": "2026-06-14", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-14", "time": "8:00 AM", "court": "6"},
        {"date": "2026-06-15", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-15", "time": "8:00 AM", "court": "6"},
    ]


def test_set_auto_book_ensures_release_day_9am_is_present_and_sorted(monkeypatch):
    import booking_agent as booking_agent_mod

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 30)

    state = {"auto_book_slots": []}
    saved_states = []

    monkeypatch.setattr(booking_agent_mod, "date", FixedDate)
    monkeypatch.setattr(booking_agent_mod, "load_state", lambda: state)
    monkeypatch.setattr(booking_agent_mod, "save_state", lambda value: saved_states.append(value.copy()))

    result = booking_agent_mod._set_auto_book([
        {"date": "2026-06-13", "time": "8:00 AM"},
    ])

    assert result["ok"] is True
    assert result["auto_book_slots"] == [
        {"date": "2026-06-13", "time": "9:00 AM"},
        {"date": "2026-06-13", "time": "8:00 AM"},
    ]


def test_with_weekday_dates_adds_weekday_to_iso_dates():
    message = "2026-06-01 9:00 AM and 2026-06-06 8:00 AM"
    assert with_weekday_dates(message) == (
        "2026-06-01 (Monday) 9:00 AM and 2026-06-06 (Saturday) 8:00 AM"
    )


def test_with_weekday_dates_leaves_invalid_dates_and_timestamps():
    message = "Bad 2026-02-31; stamp 2026-06-01T15:00:00Z"
    assert with_weekday_dates(message) == message


def test_with_weekday_dates_does_not_duplicate_existing_weekday():
    message = "2026-06-01 (Monday), 2026-06-01 Monday, Monday 2026-06-01"
    assert with_weekday_dates(message) == message


# ── calendar_sync ─────────────────────────────────────────────────────────────

def test_calendar_event_id_is_stable():
    slot = {"date": "2026-06-01", "time": "9:00 AM", "court": "6"}
    assert _calendar_event_id(slot) == _calendar_event_id(dict(slot))


def test_calendar_event_body_uses_one_hour_pt_slot():
    slot = {"date": "2026-06-01", "time": "9:00 AM", "court": "6"}
    body = _calendar_event_body(slot)
    assert body["summary"] == "Pickleball Court 6"
    assert body["start_iso"] == "2026-06-01T09:00:00-07:00"
    assert body["end_iso"] == "2026-06-01T10:00:00-07:00"
    assert body["time_zone"] == "America/Los_Angeles"


def test_calendar_event_body_invites_configured_attendees(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_ATTENDEES", "kejia.ma@gmail.com")
    slot = {"date": "2026-06-01", "time": "9:00 AM", "court": "6"}
    body = _calendar_event_body(slot)
    assert body["attendees"] == ["kejia.ma@gmail.com"]


def test_calendar_event_body_includes_apps_script_secret(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_SECRET", "test-secret")
    slot = {"date": "2026-06-01", "time": "9:00 AM", "court": "6"}
    body = _calendar_event_body(slot)
    assert body["secret"] == "test-secret"


# ── scheduler ─────────────────────────────────────────────────────────────────

def test_release_probe_session_targets_burst_day_and_persists_log(monkeypatch):
    import copy
    import scheduler as scheduler_mod
    from config import PT

    class FixedDateTime(datetime):
        _moments = [
            datetime(2026, 5, 30, 7, 58, 0, tzinfo=PT),
            datetime(2026, 5, 30, 7, 58, 15, tzinfo=PT),
            datetime(2026, 5, 30, 8, 0, 5, tzinfo=PT),
            datetime(2026, 5, 30, 8, 0, 20, tzinfo=PT),
            datetime(2026, 5, 30, 8, 1, 0, tzinfo=PT),
            datetime(2026, 5, 30, 8, 2, 0, tzinfo=PT),
        ]

        @classmethod
        def now(cls, tz=None):
            if cls._moments:
                value = cls._moments.pop(0)
            else:
                value = datetime(2026, 5, 30, 8, 2, 0, tzinfo=PT)
            return value.astimezone(tz) if tz is not None else value

    state = {
        "cached_jwt": "jwt",
        "cached_jwt_expires_at": "2026-05-30T16:30:00.000Z",
        "my_reservations": [],
        "auto_book_slots": [
            {"date": "2026-06-13", "time": "8:00 AM"},
            {"date": "2026-06-13", "time": "9:00 AM"},
            {"date": "2026-06-14", "time": "8:00 AM"},
            {"date": "2026-06-14", "time": "9:00 AM"},
        ],
        "release_probe_log": [],
        "app_booking_log": [],
    }
    saved_states = []
    api_scan_calls = []

    monkeypatch.setattr(scheduler_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduler_mod, "load_state", lambda: state)
    monkeypatch.setattr(scheduler_mod, "save_state", lambda value: saved_states.append(copy.deepcopy(value)))
    monkeypatch.setattr(scheduler_mod, "_get_cached_jwt", lambda value: "jwt")
    monkeypatch.setattr(scheduler_mod, "_firebase_login", lambda: (_ for _ in ()).throw(AssertionError("login should not run")))
    monkeypatch.setattr(scheduler_mod, "_apply_booked_slots", lambda st, booked: None)
    monkeypatch.setattr(scheduler_mod, "_notify_booked_slots", lambda booked: None)
    monkeypatch.setattr(scheduler_mod, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scheduler_mod.time, "sleep", lambda seconds: None)

    def fake_api_scan(target_times_by_date=None, auto_book_slots=None, jwt=None, detailed_log=None):
        targets = {date_str: list(times) for date_str, times in (target_times_by_date or {}).items()}
        api_scan_calls.append(
            {
                "targets": targets,
                "auto_book_slots": list(auto_book_slots or []),
                "jwt": jwt,
                "detailed_log": detailed_log,
            }
        )
        if targets == {"2026-06-13": ["9:00 AM"]}:
            if detailed_log is not None:
                detailed_log.append(
                    {
                        "date": "2026-06-13",
                        "time": "9:00 AM",
                        "open_courts": ["6"],
                        "attempts": [{"attempt": 1, "court": "6", "result": "booked"}],
                        "result": "booked",
                        "court": "6",
                    }
                )
            return (
                {
                    "2026-06-13": {
                        "9:00 AM": {"6": True, "4": False, "5": False},
                    }
                },
                [{"date": "2026-06-13", "time": "9:00 AM", "court": "6"}],
            )
        return ({}, [])

    monkeypatch.setattr(scheduler_mod, "_api_scan", fake_api_scan)

    scheduler_mod._run_release_probe_session()

    assert [call["targets"] for call in api_scan_calls[:3]] == [
        {"2026-06-13": ["9:00 AM", "8:00 AM"]},
        {"2026-06-13": ["9:00 AM"]},
        {"2026-06-13": ["9:00 AM", "8:00 AM"]},
    ]
    assert saved_states[-1]["last_release_probe_session"] is not None
    assert any(
        entry.get("phase") == "burst"
        and entry.get("booked") == ["2026-06-13 9:00 AM Court 6"]
        and entry.get("booking_attempts")
        for entry in saved_states[-1]["release_probe_log"]
    )


def test_prep_retry_helpers_schedule_and_notify(monkeypatch):
    import booking_agent as booking_agent_mod
    from config import PT

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 5, 23, 7, 30, tzinfo=PT)
            return value.astimezone(tz) if tz is not None else value

    state = {}
    saved_states = []
    enqueued = []
    messages = []

    def fake_save_state(value):
        snapshot = value.copy()
        state.clear()
        state.update(snapshot)
        saved_states.append(snapshot)

    monkeypatch.setattr(booking_agent_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(booking_agent_mod, "load_state", lambda: state)
    monkeypatch.setattr(booking_agent_mod, "save_state", fake_save_state)
    monkeypatch.setattr(
        booking_agent_mod,
        "_enqueue_work",
        lambda kind, payload=None, delay_seconds=0: enqueued.append((kind, payload, delay_seconds)) or True,
    )
    monkeypatch.setattr(booking_agent_mod, "send_telegram", lambda text: messages.append(text))

    retry_at = booking_agent_mod._schedule_prep_retry(attempt=2, reason="boom")
    booking_agent_mod._send_prep_failure("boom", retry_at)

    assert enqueued == [("booking_agent_prep_retry", {"attempt": 2}, 900)]
    assert state["booking_agent_prep_retry_attempt"] == 2
    assert state["booking_agent_prep_retry_scheduled_at"] == "2026-05-23T07:45:00-07:00"
    assert retry_at == "2026-05-23 07:45 AM PT"
    assert messages == ["❌ Prep agent failed: boom\nRetry scheduled: 2026-05-23 07:45 AM PT"]


def test_should_run_scheduled_tick_1h():
    state = {"scan_interval_hours": 1.0}
    # Should run at the top of any hour
    now = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    assert _should_run_scheduled_tick(state, now) is True
    # Should not run at :15
    now2 = datetime(2026, 5, 1, 10, 15, tzinfo=timezone.utc)
    assert _should_run_scheduled_tick(state, now2) is False


def test_should_run_scheduled_tick_30m():
    state = {"scan_interval_hours": 0.5}
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)) is True
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 10, 30, tzinfo=timezone.utc)) is True
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 10, 15, tzinfo=timezone.utc)) is False


def test_should_run_scheduled_tick_sub_15m():
    state = {"scan_interval_hours": 0.1}
    # Always true for sub-15min intervals
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 10, 7, tzinfo=timezone.utc)) is True
