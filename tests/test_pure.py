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
    _normalize_notified_slots, _sanitize_chat_history,
)
from http_utils import get_path, get_method, summarize_results
from notify import ordinal, _ordered_open_courts
from scheduler import _should_run_scheduled_tick
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
