"""Unit tests for pure functions that require no external dependencies."""
import os
import sys
from datetime import date, datetime, timezone

os.environ.setdefault("EMAIL", "x")
os.environ.setdefault("PASSWORD", "x")
os.environ.setdefault("SCANS_ENABLED", "1")
os.environ.setdefault("AUTO_BOOKING_ENABLED", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from config import _time_text_to_hhmm, _HHMM_TO_TIME_TEXT, build_next_dates, SLOT_TIMES
from state import (
    _normalize_court_number, _normalize_time_availability, _empty_court_availability,
    _normalize_notified_slots, _sanitize_chat_history, _auto_book_slot_is_too_close,
)
from http_utils import get_path, get_method, summarize_results
from message_format import with_weekday_dates
from notify import ordinal, _alert_lines_for_open_targets, _ordered_open_courts
from scheduler import _effective_scan_interval_hours, _should_run_scheduled_tick
from scanner import _auto_book_priority_key
from booking import _apply_booked_slots
from calendar_sync import _calendar_delete_body, _calendar_event_body, _calendar_event_id


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


def test_scanner_paired_court_order_prefers_4_before_5_after_6():
    import scanner as scanner_mod

    assert scanner_mod._paired_court_order([]) == ["6", "4", "5"]
    assert scanner_mod._paired_court_order(["6"]) == ["4", "5"]


def test_scheduler_paired_court_order_prefers_4_before_5_after_6():
    import scheduler as scheduler_mod

    assert scheduler_mod._paired_court_order([]) == ["6", "4", "5"]
    assert scheduler_mod._paired_court_order(["6"]) == ["4", "5"]


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

    monkeypatch.setattr(state_mod, "date", type("FixedDate", (date,), {
        "today": classmethod(lambda cls: cls(2026, 7, 1)),
    }))
    monkeypatch.setattr(state_mod, "_auto_book_slot_is_too_close", lambda slot_date, slot_time, now=None: slot_time == "9:00 AM")
    normalized = state_mod._normalize_state({
        "auto_book_slots": [
            {"date": "2026-07-14", "time": "9:00 AM"},
            {"date": "2026-07-14", "time": "10:00 AM"},
        ]
    })
    assert normalized["auto_book_slots"] == [{"date": "2026-07-14", "time": "10:00 AM"}]


def test_normalize_state_clamps_auto_book_count_any_day(monkeypatch):
    import state as state_mod

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 30)

    monkeypatch.setattr(state_mod, "date", FixedDate)
    monkeypatch.setattr(state_mod, "_auto_book_slot_is_too_close", lambda slot_date, slot_time, now=None: False)
    normalized = state_mod._normalize_state({
        "auto_book_slots": [
            {"date": "2026-06-06", "time": "9:00 AM", "count": 5},  # Saturday, over max -> clamped to 3
            {"date": "2026-06-08", "time": "9:00 AM", "count": 2},  # Monday, weekday -> count honored too
        ]
    })
    assert normalized["auto_book_slots"] == [
        {"date": "2026-06-06", "time": "9:00 AM", "count": 3},
        {"date": "2026-06-08", "time": "9:00 AM", "count": 2},
    ]


def test_normalize_state_preserves_release_probe_fields():
    import state as state_mod

    normalized = state_mod._normalize_state({
        "release_probe_session_date": "2026-05-30",
        "last_release_probe_session": "2026-05-30T15:58:14.000Z",
        "release_probe_log": [
            {
                "ts": "2026-05-30T15:58:14.000Z",
                "phase": "burst",
                "result": "open",
                "targets": [
                    {
                        "date": "2026-06-13",
                        "times": ["9:00 AM"],
                        "courts": {
                            "9:00 AM": {"6": True, "4": False, "5": False}
                        },
                    }
                ],
            }
        ],
    })
    assert normalized["release_probe_session_date"] == "2026-05-30"
    assert normalized["last_release_probe_session"] == "2026-05-30T15:58:14.000Z"
    assert normalized["release_probe_log"] == [
        {
            "ts": "2026-05-30T15:58:14.000Z",
            "phase": "burst",
            "result": "open",
            "targets": [
                {
                    "date": "2026-06-13",
                    "times": ["9:00 AM"],
                    "courts": {
                        "9:00 AM": {"6": True, "4": False, "5": False}
                    },
                }
            ],
        }
    ]


def test_weekend_target_checked_times_reads_auto_book_slots_only_for_that_date():
    import state as state_mod

    state = {
        "auto_book_slots": [
            {"date": "2026-06-13", "time": "9:00 AM"},
            {"date": "2026-06-13", "time": "10:00 AM"},
            {"date": "2026-06-20", "time": "8:00 AM"},  # different date, must not leak in
        ],
    }
    assert state_mod._weekend_target_checked_times(state, "2026-06-13") == ("9:00 AM", "10:00 AM")
    assert state_mod._weekend_target_checked_times(state, "2026-06-20") == ("8:00 AM",)
    assert state_mod._weekend_target_checked_times(state, "2026-06-27") == ()


def test_set_weekend_target_defaults_writes_auto_book_and_watch_all_courts():
    import state as state_mod

    state = {"auto_book_slots": [], "watched_slots": [], "weekend_target_prefilled_dates": []}
    state_mod._set_weekend_target_defaults(
        state, "2026-06-13", auto_book_times=["8:00 AM", "9:00 AM"], watch_times=["10:00 AM"],
    )
    assert sorted((s["time"] for s in state["auto_book_slots"]), key=str) == ["8:00 AM", "9:00 AM"]
    assert {s["court"] for s in state["watched_slots"]} == {"6", "4", "5"}
    assert all(s["time"] == "10:00 AM" for s in state["watched_slots"])
    assert state_mod._is_weekend_target_prefilled(state, "2026-06-13") is True
    assert state_mod._is_weekend_target_prefilled(state, "2026-06-20") is False


def test_normalize_state_keeps_friend_reservations_and_removes_overlap_with_mine():
    import state as state_mod

    normalized = state_mod._normalize_state({
        "my_reservations": [
            {"date": "2026-06-01", "time": "9:00 AM", "court": "6"},
        ],
        "friend_reservations": [
            {"date": "2026-06-01", "time": "9:00 AM", "court": "6"},
            {"date": "2026-06-01", "time": "10:00 AM", "court": "4"},
        ],
    })
    assert normalized["my_reservations"] == [
        {"date": "2026-06-01", "time": "9:00 AM", "court": "6"},
    ]
    assert normalized["friend_reservations"] == [
        {"date": "2026-06-01", "time": "10:00 AM", "court": "4"},
    ]


def test_normalize_state_preserves_my_reservation_account_metadata():
    import state as state_mod

    normalized = state_mod._normalize_state({
        "my_reservations": [
            {
                "date": "2026-07-15",
                "time": "8:00 AM",
                "court": "6",
                "account_index": 3,
                "account_email": "player2@example.com",
                "booking_id": "booking-3",
                "reservation_id": "reservation-3",
                "facility_rental_id": "facility-3",
                "invoice_number": "invoice-3",
                "transaction_number": "transaction-3",
            },
        ],
    })

    assert normalized["my_reservations"] == [
        {
            "date": "2026-07-15",
            "time": "8:00 AM",
            "court": "6",
            "account_index": 3,
            "account_email": "player2@example.com",
            "booking_id": "booking-3",
            "reservation_id": "reservation-3",
            "facility_rental_id": "facility-3",
            "invoice_number": "invoice-3",
            "transaction_number": "transaction-3",
        },
    ]


def test_normalize_state_strips_past_reservation_artifacts(monkeypatch):
    import state as state_mod

    monkeypatch.setattr(state_mod, "date", type("FixedDate", (date,), {
        "today": classmethod(lambda cls: cls(2026, 10, 14)),
    }))
    normalized = state_mod._normalize_state({
        "my_reservations": [
            {
                "date": "2026-10-13",
                "time": "8:00 AM",
                "court": "6",
                "confirmation_invoice_screenshot_url": "https://example.com/past.png",
                "confirmation_invoice_pdf_url": "https://example.com/past.pdf",
            },
            {
                "date": "2026-10-14",
                "time": "8:00 AM",
                "court": "4",
                "confirmation_invoice_screenshot_url": "https://example.com/today.png",
            },
        ],
    })

    past, today_slot = normalized["my_reservations"]
    assert "confirmation_invoice_screenshot_url" not in past
    assert "confirmation_invoice_pdf_url" not in past
    assert today_slot["confirmation_invoice_screenshot_url"] == "https://example.com/today.png"


def test_reservation_artifact_s3_key_only_accepts_owned_prefix(monkeypatch):
    import state as state_mod

    monkeypatch.setattr(state_mod, "STATE_BUCKET", "pickleball-monitor-kma")

    assert state_mod._reservation_artifact_s3_key(
        "https://pickleball-monitor-kma.s3.us-west-2.amazonaws.com/img/reservation-artifacts/a.png"
    ) == "img/reservation-artifacts/a.png"
    assert state_mod._reservation_artifact_s3_key(
        "https://pickleball-monitor-kma.s3.us-west-2.amazonaws.com/reservation-artifacts/legacy.png"
    ) == "reservation-artifacts/legacy.png"
    assert state_mod._reservation_artifact_s3_key(
        "https://pickleball-monitor-kma.s3.us-west-2.amazonaws.com/index.html"
    ) is None
    assert state_mod._reservation_artifact_s3_key(
        "https://other-bucket.s3.us-west-2.amazonaws.com/reservation-artifacts/a.png"
    ) is None


def test_sync_rec_my_reservations_preserves_invoice_artifacts(monkeypatch):
    import rec_api as rec_api_mod

    monkeypatch.setattr(rec_api_mod, "_configured_rec_accounts", lambda: [{"index": 1}])
    monkeypatch.setattr(rec_api_mod, "fetch_rec_my_reservations", lambda: [{
        "date": "2026-10-25",
        "time": "9:00 AM",
        "court": "6",
        "booking_id": "booking-9",
    }])
    monkeypatch.setattr(rec_api_mod, "record_booking_history_from_reservations", lambda state: False)
    state = {
        "my_reservations": [{
            "date": "2026-10-25",
            "time": "9:00 AM",
            "court": "6",
            "booking_id": "booking-9",
            "invoice_number": "INV-9",
            "confirmation_invoice_screenshot_url": "https://example.com/invoice.jpg",
        }],
    }

    assert rec_api_mod.sync_rec_my_reservations(state) is True
    assert state["my_reservations"][0]["invoice_number"] == "INV-9"
    assert state["my_reservations"][0]["confirmation_invoice_screenshot_url"] == "https://example.com/invoice.jpg"


def test_state_route_exposes_reservation_accounts(monkeypatch):
    import json
    import routes as routes_mod

    monkeypatch.setattr(routes_mod, "date", type("FixedDate", (date,), {
        "today": classmethod(lambda cls: cls(2026, 7, 15)),
    }))
    monkeypatch.setattr(routes_mod, "_load_telegram_usage", lambda: [])
    monkeypatch.setattr(
        routes_mod,
        "load_state",
        lambda: {
            "my_reservations": [
                {
                    "date": "2026-07-15",
                    "time": "8:00 AM",
                    "court": "6",
                    "account_index": 3,
                    "account_email": "player2@example.com",
                    "facility_rental_id": "rental-3",
                    "invoice_number": "invoice-3",
                    "transaction_number": "transaction-3",
                    "confirmation_invoice_screenshot_url": "https://example.com/invoice.png",
                    "confirmation_invoice_pdf_url": "https://example.com/invoice.pdf",
                    "confirmation_email_screenshot_url": "https://example.com/confirmation.png",
                },
            ],
        },
    )

    response = routes_mod.handle_state({})
    body = json.loads(response["body"])
    slot = next(
        s for s in body["grid"][0]["slots"]
        if s["time"] == "8:00 AM" and s["court"] == "6"
    )

    assert slot["mine"] is True
    assert slot["reservation_accounts"] == [
        {
            "index": 3,
            "email": "player2@example.com",
            "label": "player2",
            "facility_rental_id": "rental-3",
            "invoice_number": "invoice-3",
            "transaction_number": "transaction-3",
            "confirmation_invoice_screenshot_url": "https://example.com/invoice.png",
            "confirmation_invoice_pdf_url": "https://example.com/invoice.pdf",
            "confirmation_email_screenshot_url": "https://example.com/confirmation.png",
        },
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


def test_alert_lines_skip_satisfied_auto_book_count():
    state = {
        "auto_book_slots": [{"date": "2026-07-25", "time": "5:00 PM", "count": 2}],
        "my_reservations": [
            {"date": "2026-07-25", "time": "5:00 PM", "court": "6"},
            {"date": "2026-07-25", "time": "5:00 PM", "court": "5"},
        ],
        "watched_slots": [],
    }
    scanned = {
        "2026-07-25": {
            "5:00 PM": {"6": False, "4": True, "5": False},
        },
    }

    assert _alert_lines_for_open_targets(state, scanned) == []


def test_alert_lines_only_reports_remaining_auto_book_courts():
    state = {
        "auto_book_slots": [{"date": "2026-07-25", "time": "5:00 PM", "count": 2}],
        "my_reservations": [
            {"date": "2026-07-25", "time": "5:00 PM", "court": "6"},
        ],
        "watched_slots": [],
    }
    scanned = {
        "2026-07-25": {
            "5:00 PM": {"6": False, "4": True, "5": True},
        },
    }

    assert _alert_lines_for_open_targets(state, scanned) == [
        "2026-07-25 5:00 PM: open Court 4 (auto-book)"
    ]


def test_apply_booked_slots_clears_completed_auto_book_count():
    state = {
        "auto_book_slots": [{"date": "2026-07-25", "time": "5:00 PM", "count": 2}],
        "my_reservations": [
            {"date": "2026-07-25", "time": "5:00 PM", "court": "6"},
        ],
    }

    _apply_booked_slots(state, [{"date": "2026-07-25", "time": "5:00 PM", "court": "4"}])

    assert state["auto_book_slots"] == []


def test_apply_booked_slots_keeps_incomplete_auto_book_count():
    state = {
        "auto_book_slots": [{"date": "2026-07-25", "time": "5:00 PM", "count": 3}],
        "my_reservations": [
            {"date": "2026-07-25", "time": "5:00 PM", "court": "6"},
        ],
    }

    _apply_booked_slots(state, [{"date": "2026-07-25", "time": "5:00 PM", "court": "4"}])

    assert state["auto_book_slots"] == [{"date": "2026-07-25", "time": "5:00 PM", "count": 3}]


def test_apply_booked_slots_clears_already_satisfied_auto_book_count_without_new_booking():
    state = {
        "auto_book_slots": [{"date": "2026-07-25", "time": "5:00 PM", "count": 2}],
        "my_reservations": [
            {"date": "2026-07-25", "time": "5:00 PM", "court": "6"},
            {"date": "2026-07-25", "time": "5:00 PM", "court": "5"},
        ],
    }

    _apply_booked_slots(state, [])

    assert state["auto_book_slots"] == []


def test_auto_book_priority_prefers_release_day_and_9am():
    preferred_date = date(2026, 6, 13)
    items = [
        ("2026-06-13", "8:00 AM"),
        ("2026-06-13", "9:00 AM"),
        ("2026-06-13", "10:00 AM"),
        ("2026-06-14", "9:00 AM"),
    ]
    result = sorted(
        items,
        key=lambda item: _auto_book_priority_key(item[0], item[1], preferred_date=preferred_date),
    )
    assert result == [
        ("2026-06-13", "9:00 AM"),
        ("2026-06-13", "8:00 AM"),
        ("2026-06-13", "10:00 AM"),
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
                "10:00 AM": {"6": True, "4": False, "5": False},
            },
            "2026-06-14": {
                "8:00 AM": {"6": True, "4": False, "5": False},
                "9:00 AM": {"6": True, "4": False, "5": False},
                "10:00 AM": {"6": True, "4": False, "5": False},
            },
            "2026-06-15": {
                "8:00 AM": {"6": True, "4": False, "5": False},
                "9:00 AM": {"6": True, "4": False, "5": False},
                "10:00 AM": {"6": True, "4": False, "5": False},
            },
        }

    booked_calls = []

    monkeypatch.setattr(scanner_mod, "load_state", lambda: state)
    monkeypatch.setattr(scanner_mod, "save_state", fake_save_state)
    monkeypatch.setattr(scanner_mod, "_api_fetch_availability", fake_fetch_availability)
    monkeypatch.setattr(scanner_mod, "_recent_booking_count", lambda state: 0)
    monkeypatch.setattr(
        scanner_mod,
        "book_slot_api",
        lambda jwt, slot_date, time_text, court, transaction_log=None: booked_calls.append(
            (slot_date.isoformat(), time_text, court)
        ) or True,
    )
    monkeypatch.setattr(scanner_mod, "send_telegram", lambda msg: None)

    new_avail, booked = scanner_mod._api_scan(
        auto_book_slots=[
            {"date": "2026-06-13", "time": "8:00 AM"},
            {"date": "2026-06-13", "time": "9:00 AM"},
            {"date": "2026-06-13", "time": "10:00 AM"},
            {"date": "2026-06-14", "time": "8:00 AM"},
            {"date": "2026-06-14", "time": "9:00 AM"},
            {"date": "2026-06-14", "time": "10:00 AM"},
            {"date": "2026-06-15", "time": "8:00 AM"},
            {"date": "2026-06-15", "time": "9:00 AM"},
            {"date": "2026-06-15", "time": "10:00 AM"},
        ],
        jwt="test-jwt",
    )

    assert booked == [
        {"date": "2026-06-13", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-13", "time": "8:00 AM", "court": "6"},
        {"date": "2026-06-13", "time": "10:00 AM", "court": "6"},
        {"date": "2026-06-14", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-14", "time": "8:00 AM", "court": "6"},
        {"date": "2026-06-14", "time": "10:00 AM", "court": "6"},
        {"date": "2026-06-15", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-15", "time": "8:00 AM", "court": "6"},
        {"date": "2026-06-15", "time": "10:00 AM", "court": "6"},
    ]
    assert booked_calls == [
        ("2026-06-13", "9:00 AM", "6"),
        ("2026-06-13", "8:00 AM", "6"),
        ("2026-06-13", "10:00 AM", "6"),
        ("2026-06-14", "9:00 AM", "6"),
        ("2026-06-14", "8:00 AM", "6"),
        ("2026-06-14", "10:00 AM", "6"),
        ("2026-06-15", "9:00 AM", "6"),
        ("2026-06-15", "8:00 AM", "6"),
        ("2026-06-15", "10:00 AM", "6"),
    ]
    assert [entry["app_booking_log"][0] for entry in saved_states] == [
        {"date": "2026-06-13", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-13", "time": "8:00 AM", "court": "6"},
        {"date": "2026-06-13", "time": "10:00 AM", "court": "6"},
        {"date": "2026-06-14", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-14", "time": "8:00 AM", "court": "6"},
        {"date": "2026-06-14", "time": "10:00 AM", "court": "6"},
        {"date": "2026-06-15", "time": "9:00 AM", "court": "6"},
        {"date": "2026-06-15", "time": "8:00 AM", "court": "6"},
        {"date": "2026-06-15", "time": "10:00 AM", "court": "6"},
    ]


def test_weekend_auto_book_uses_main_account_for_saturday_targets(monkeypatch):
    import copy
    import scanner as scanner_mod

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 30)

    state = {"my_reservations": [], "app_booking_log": [], "auto_book_failures": []}
    saved_states = []
    booked_calls = []

    def fake_save_state(value):
        saved_states.append(copy.deepcopy(value))

    monkeypatch.setattr(scanner_mod, "date", FixedDate)
    monkeypatch.setattr(scanner_mod, "load_state", lambda: state)
    monkeypatch.setattr(scanner_mod, "save_state", fake_save_state)
    monkeypatch.setattr(scanner_mod, "_recent_booking_count", lambda state: 0)
    monkeypatch.setattr(
        scanner_mod,
        "_api_fetch_availability",
        lambda target_times_by_date=None: {
            "2026-06-13": {
                "8:00 AM": {"6": True, "4": True, "5": True},
                "9:00 AM": {"6": True, "4": True, "5": True},
            },
        },
    )
    monkeypatch.setattr(
        scanner_mod,
        "_rec_booking_sessions",
        lambda state: [
            {"account_index": 1, "jwt": "jwt-1", "participant_user_id": "user-1"},
            {"account_index": 2, "jwt": "jwt-2", "participant_user_id": "user-2"},
        ],
    )
    monkeypatch.setattr(scanner_mod, "send_telegram", lambda msg: None)

    def fake_book_slot(jwt, slot_date, time_text, court, transaction_log=None, participant_user_id=None):
        booked_calls.append((jwt, participant_user_id, slot_date.isoformat(), time_text, court))
        return True

    monkeypatch.setattr(scanner_mod, "book_slot_api", fake_book_slot)

    _, booked = scanner_mod._api_scan(
        auto_book_slots=[
            {"date": "2026-06-13", "time": "8:00 AM"},
            {"date": "2026-06-13", "time": "9:00 AM"},
        ],
    )

    assert booked == [
        {"date": "2026-06-13", "time": "9:00 AM", "court": "6", "account_index": 1},
        {"date": "2026-06-13", "time": "8:00 AM", "court": "6", "account_index": 1},
    ]
    assert booked_calls == [
        ("jwt-1", "user-1", "2026-06-13", "9:00 AM", "6"),
        ("jwt-1", "user-1", "2026-06-13", "8:00 AM", "6"),
    ]
    assert len(saved_states[-1]["app_booking_log"]) == 2


def test_auto_book_records_payment_identifiers(monkeypatch):
    import copy
    import scanner as scanner_mod

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 30)

    state = {"my_reservations": [], "app_booking_log": [], "auto_book_failures": []}
    saved_states = []

    def fake_save_state(value):
        saved_states.append(copy.deepcopy(value))

    def fake_book_slot(jwt, slot_date, time_text, court, transaction_log=None, participant_user_id=None):
        transaction_log["identifiers"] = {
            "order_id": "order-123",
            "invoice_number": "inv-456",
            "transaction_number": "txn-789",
            "facility_rental_id": "rental-012",
            "payment_id": "pay-abc",
            "payment_intent_id": "pi-def",
        }
        return True

    monkeypatch.setattr(scanner_mod, "date", FixedDate)
    monkeypatch.setattr(scanner_mod, "load_state", lambda: state)
    monkeypatch.setattr(scanner_mod, "save_state", fake_save_state)
    monkeypatch.setattr(scanner_mod, "_recent_booking_count", lambda state: 0)
    monkeypatch.setattr(
        scanner_mod,
        "_api_fetch_availability",
        lambda target_times_by_date=None: {
            "2026-06-13": {"9:00 AM": {"6": True, "4": False, "5": False}},
        },
    )
    monkeypatch.setattr(scanner_mod, "book_slot_api", fake_book_slot)
    monkeypatch.setattr(scanner_mod, "send_telegram", lambda msg: None)

    _, booked = scanner_mod._api_scan(
        auto_book_slots=[{"date": "2026-06-13", "time": "9:00 AM"}],
        jwt="test-jwt",
    )

    assert booked == [{
        "date": "2026-06-13",
        "time": "9:00 AM",
        "court": "6",
        "order_id": "order-123",
        "invoice_number": "inv-456",
        "transaction_number": "txn-789",
        "facility_rental_id": "rental-012",
        "payment_id": "pay-abc",
        "payment_intent_id": "pi-def",
    }]
    assert saved_states[-1]["app_booking_log"][0]["invoice_number"] == "inv-456"
    assert saved_states[-1]["app_booking_log"][0]["transaction_number"] == "txn-789"


def test_booking_payment_identifiers_extracts_live_and_synced_ids():
    import rec_api as rec_api_mod

    live = rec_api_mod._booking_payment_identifiers({
        "reservation": {"response": {"body": {"data": {
            "id": "order-live",
            "invoiceNumber": "inv-live",
            "facilityRentalId": "rental-live",
        }}}},
        "payment": {
            "response": {
                "body": {
                    "included": {
                        "payments": [{
                            "id": "pay-live",
                            "gatewayData": {"paymentIntentId": "pi-live", "chargeId": "ch-live"},
                        }],
                    },
                },
            },
        },
    })
    synced = rec_api_mod._booking_payment_identifiers({
        "booking": {
            "response": {
                "body": {
                    "data": {
                        "orderId": "order-sync",
                        "invoice_number": "inv-sync",
                        "facility_rental_id": "rental-sync",
                        "paymentId": "pay-sync",
                        "transactionId": "txn-sync",
                    },
                },
            },
        },
    })

    assert live == {
        "order_id": "order-live",
        "invoice_number": "inv-live",
        "payment_id": "pay-live",
        "payment_intent_id": "pi-live",
        "transaction_number": "ch-live",
        "facility_rental_id": "rental-live",
    }
    assert synced == {
        "order_id": "order-sync",
        "invoice_number": "inv-sync",
        "payment_id": "pay-sync",
        "transaction_number": "txn-sync",
        "facility_rental_id": "rental-sync",
    }


def test_weekend_auto_book_honors_per_slot_count(monkeypatch):
    import copy
    import scanner as scanner_mod

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 30)

    state = {"my_reservations": [], "app_booking_log": [], "auto_book_failures": []}
    saved_states = []
    booked_calls = []

    def fake_save_state(value):
        saved_states.append(copy.deepcopy(value))

    monkeypatch.setattr(scanner_mod, "date", FixedDate)
    monkeypatch.setattr(scanner_mod, "load_state", lambda: state)
    monkeypatch.setattr(scanner_mod, "save_state", fake_save_state)
    monkeypatch.setattr(scanner_mod, "_recent_booking_count", lambda state: 0)
    monkeypatch.setattr(
        scanner_mod,
        "_api_fetch_availability",
        lambda target_times_by_date=None: {
            "2026-06-13": {
                "9:00 AM": {"6": True, "4": True, "5": True},
            },
        },
    )
    monkeypatch.setattr(
        scanner_mod,
        "_rec_booking_sessions",
        lambda state: [
            {"account_index": 1, "jwt": "jwt-1", "participant_user_id": "user-1"},
            {"account_index": 2, "jwt": "jwt-2", "participant_user_id": "user-2"},
            {"account_index": 3, "jwt": "jwt-3", "participant_user_id": "user-3"},
        ],
    )
    monkeypatch.setattr(scanner_mod, "send_telegram", lambda msg: None)

    def fake_book_slot(jwt, slot_date, time_text, court, transaction_log=None, participant_user_id=None):
        booked_calls.append((jwt, court))
        return True

    monkeypatch.setattr(scanner_mod, "book_slot_api", fake_book_slot)

    # 2026-06-13 is a Saturday; count=2 should book 2 courts via 2 distinct accounts.
    _, booked = scanner_mod._api_scan(
        auto_book_slots=[{"date": "2026-06-13", "time": "9:00 AM", "count": 2}],
    )

    assert len(booked) == 2
    assert {b["account_index"] for b in booked} == {1, 2}
    assert {c[0] for c in booked_calls} == {"jwt-1", "jwt-2"}


def test_auto_book_count_includes_existing_slot_reservations(monkeypatch):
    import copy
    import scanner as scanner_mod

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 21)

    state = {
        "my_reservations": [
            {"date": "2026-08-01", "time": "6:00 PM", "court": "6", "account_index": 1},
            {"date": "2026-08-01", "time": "6:00 PM", "court": "5", "account_index": 2},
        ],
        "app_booking_log": [],
        "auto_book_failures": [],
    }
    saved_states = []
    detailed_log = []

    def fake_save_state(value):
        saved_states.append(copy.deepcopy(value))

    monkeypatch.setattr(scanner_mod, "date", FixedDate)
    monkeypatch.setattr(scanner_mod, "load_state", lambda: state)
    monkeypatch.setattr(scanner_mod, "save_state", fake_save_state)
    monkeypatch.setattr(scanner_mod, "_recent_booking_count", lambda state: 0)
    monkeypatch.setattr(
        scanner_mod,
        "_api_fetch_availability",
        lambda target_times_by_date=None: {
            "2026-08-01": {
                "6:00 PM": {"6": False, "4": True, "5": False},
            },
        },
    )
    monkeypatch.setattr(
        scanner_mod,
        "_rec_booking_sessions",
        lambda state: [
            {"account_index": 1, "jwt": "jwt-1", "participant_user_id": "user-1"},
            {"account_index": 2, "jwt": "jwt-2", "participant_user_id": "user-2"},
            {"account_index": 3, "jwt": "jwt-3", "participant_user_id": "user-3"},
        ],
    )
    monkeypatch.setattr(scanner_mod, "book_slot_api", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not book")))
    monkeypatch.setattr(scanner_mod, "send_telegram", lambda msg: None)

    _, booked = scanner_mod._api_scan(
        auto_book_slots=[{"date": "2026-08-01", "time": "6:00 PM", "count": 2}],
        detailed_log=detailed_log,
    )

    assert booked == []
    assert detailed_log[0]["result"] == "already_reserved"
    assert detailed_log[0]["reserved_count"] == 2
    assert detailed_log[0]["target_count"] == 2


def test_auto_book_only_fills_remaining_slot_count(monkeypatch):
    import copy
    import scanner as scanner_mod

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 21)

    state = {
        "my_reservations": [
            {"date": "2026-08-01", "time": "6:00 PM", "court": "6", "account_index": 1},
            {"date": "2026-08-01", "time": "6:00 PM", "court": "5", "account_index": 2},
        ],
        "app_booking_log": [],
        "auto_book_failures": [],
    }
    saved_states = []
    booked_calls = []

    def fake_save_state(value):
        saved_states.append(copy.deepcopy(value))

    monkeypatch.setattr(scanner_mod, "date", FixedDate)
    monkeypatch.setattr(scanner_mod, "load_state", lambda: state)
    monkeypatch.setattr(scanner_mod, "save_state", fake_save_state)
    monkeypatch.setattr(scanner_mod, "_recent_booking_count", lambda state: 0)
    monkeypatch.setattr(
        scanner_mod,
        "_api_fetch_availability",
        lambda target_times_by_date=None: {
            "2026-08-01": {
                "6:00 PM": {"6": True, "4": True, "5": True},
            },
        },
    )
    monkeypatch.setattr(
        scanner_mod,
        "_rec_booking_sessions",
        lambda state: [
            {"account_index": 1, "jwt": "jwt-1", "participant_user_id": "user-1"},
            {"account_index": 2, "jwt": "jwt-2", "participant_user_id": "user-2"},
            {"account_index": 3, "jwt": "jwt-3", "participant_user_id": "user-3"},
        ],
    )
    monkeypatch.setattr(scanner_mod, "send_telegram", lambda msg: None)

    def fake_book_slot(jwt, slot_date, time_text, court, transaction_log=None, participant_user_id=None):
        booked_calls.append((jwt, court))
        return True

    monkeypatch.setattr(scanner_mod, "book_slot_api", fake_book_slot)

    _, booked = scanner_mod._api_scan(
        auto_book_slots=[{"date": "2026-08-01", "time": "6:00 PM", "count": 3}],
    )

    assert booked == [{"date": "2026-08-01", "time": "6:00 PM", "court": "4", "account_index": 3}]
    assert booked_calls == [("jwt-3", "4")]


def test_release_probe_window_does_not_call_api_scan(monkeypatch):
    import copy
    import scheduler as scheduler_mod
    from config import PT

    class FixedDateTime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            if cls.calls <= 2:
                value = datetime(2026, 5, 30, 8, 0, 0, tzinfo=PT)
            else:
                value = datetime(2026, 5, 30, 8, 2, 1, tzinfo=PT)
            return value.astimezone(tz) if tz is not None else value

    state = {
        "my_reservations": [],
        "auto_book_slots": [
            {"date": "2026-06-13", "time": "8:00 AM"},
            {"date": "2026-06-13", "time": "9:00 AM"},
        ],
        "release_probe_log": [],
        "app_booking_log": [],
    }
    api_scan_calls = []
    book_calls = []

    def fake_save_state(value):
        snapshot = copy.deepcopy(value)
        state.clear()
        state.update(snapshot)

    monkeypatch.setattr(scheduler_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduler_mod, "load_state", lambda: state)
    monkeypatch.setattr(scheduler_mod, "save_state", fake_save_state)
    monkeypatch.setattr(
        scheduler_mod,
        "_rec_booking_sessions",
        lambda st: [
            {"account_index": 1, "jwt": "jwt-1", "participant_user_id": "user-1"},
            {"account_index": 2, "jwt": "jwt-2", "participant_user_id": "user-2"},
        ],
    )
    def fake_book_slot(jwt, slot_date, time_text, court, transaction_log=None, participant_user_id=None):
        book_calls.append((jwt, participant_user_id, slot_date.isoformat(), time_text, court))
        return False

    monkeypatch.setattr(scheduler_mod, "book_slot_api", fake_book_slot)
    monkeypatch.setattr(scheduler_mod, "_apply_booked_slots", lambda st, booked: None)
    monkeypatch.setattr(scheduler_mod, "_notify_booked_slots", lambda booked: None)
    monkeypatch.setattr(scheduler_mod, "_enqueue_work", lambda *args, **kwargs: False)
    monkeypatch.setattr(scheduler_mod, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scheduler_mod.time, "sleep", lambda seconds: None)

    def fake_api_scan(*args, **kwargs):
        api_scan_calls.append(kwargs)
        return ({"2026-06-13": {"9:00 AM": {"6": False, "4": False, "5": False}}}, [])

    monkeypatch.setattr(scheduler_mod, "_api_scan", fake_api_scan)

    scheduler_mod._run_release_probe_session()

    assert api_scan_calls == []
    assert book_calls
    # Both 8:00 AM and 9:00 AM are checked, so both pre-authenticated accounts
    # get paired one-per-time and tried in parallel.
    assert {call[0] for call in book_calls} == {"jwt-1", "jwt-2"}


def test_queue_release_probe_allows_late_eventbridge_tick(monkeypatch):
    import copy
    import config as config_mod
    import scheduler as scheduler_mod
    from config import PT

    state = {
        "auto_book_slots": [{"date": "2026-07-25", "time": "8:00 AM"}],
        "watched_slots": [],
    }
    saved_states = []
    enqueued = []

    def fake_save_state(value):
        snapshot = copy.deepcopy(value)
        state.clear()
        state.update(snapshot)
        saved_states.append(snapshot)

    monkeypatch.setattr(config_mod, "WORK_QUEUE_URL", "https://sqs.example/queue")
    monkeypatch.setattr(scheduler_mod, "save_state", fake_save_state)
    monkeypatch.setattr(scheduler_mod, "_rec_booking_sessions", lambda st: [{"jwt": "jwt"}])
    monkeypatch.setattr(
        scheduler_mod,
        "_enqueue_work",
        lambda kind, payload=None, delay_seconds=0: enqueued.append((kind, payload, delay_seconds)) or True,
    )

    queued = scheduler_mod._queue_release_probe_session_if_needed(
        state,
        now_pt=datetime(2026, 5, 30, 7, 46, tzinfo=PT),
    )

    assert queued is True
    assert state["release_probe_session_date"] == "2026-05-30"
    assert enqueued == [("release_probe_session", {}, 780)]
    assert saved_states


def test_queue_release_probe_dedupes_within_window(monkeypatch):
    import config as config_mod
    import scheduler as scheduler_mod
    from config import PT

    state = {
        "release_probe_session_date": "2026-05-30",
        "auto_book_slots": [{"date": "2026-07-25", "time": "8:00 AM"}],
    }

    monkeypatch.setattr(config_mod, "WORK_QUEUE_URL", "https://sqs.example/queue")
    monkeypatch.setattr(scheduler_mod, "save_state", lambda value: None)
    monkeypatch.setattr(scheduler_mod, "_rec_booking_sessions", lambda st: [{"jwt": "jwt"}])
    monkeypatch.setattr(
        scheduler_mod,
        "_enqueue_work",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not enqueue twice")),
    )

    queued = scheduler_mod._queue_release_probe_session_if_needed(
        state,
        now_pt=datetime(2026, 5, 30, 7, 50, tzinfo=PT),
    )

    assert queued is True


def test_forced_full_refresh_clears_auto_book_failures(monkeypatch):
    import copy
    import scheduler as scheduler_mod

    state = {
        "availability": {},
        "watched_slots": [],
        "auto_book_slots": [],
        "auto_book_failures": [
            {"date": "2026-07-25", "time": "9:00 AM", "error": "old failure"}
        ],
    }
    saved_states = []

    def fake_save_state(value):
        snapshot = copy.deepcopy(value)
        state.clear()
        state.update(snapshot)
        saved_states.append(snapshot)

    monkeypatch.setattr(scheduler_mod, "load_state", lambda: state)
    monkeypatch.setattr(scheduler_mod, "save_state", fake_save_state)
    monkeypatch.setattr(scheduler_mod, "_full_scan_targets", lambda: {"2026-07-25": ["9:00 AM"]})
    monkeypatch.setattr(scheduler_mod, "_api_scan", lambda **kwargs: ({"2026-07-25": {"9:00 AM": {"6": False, "4": False, "5": False}}}, []))
    monkeypatch.setattr(scheduler_mod, "_alert_lines_for_open_targets", lambda st, avail: [])
    monkeypatch.setattr(scheduler_mod, "sync_rec_my_reservations", lambda st: None)
    monkeypatch.setattr(scheduler_mod, "_apply_booked_slots", lambda st, slots: None)
    monkeypatch.setattr(scheduler_mod, "_notify_booked_slots", lambda slots: None)

    scheduler_mod._run_full_refresh_worker(force=True)

    assert saved_states[0]["auto_book_failures"] == []
    assert state["auto_book_failures"] == []


def test_one_off_probe_blind_books_exact_target(monkeypatch):
    import copy
    import scheduler as scheduler_mod

    state = {
        "availability": {},
        "recent_scan_history": [],
        "my_reservations": [],
        "auto_book_slots": [],
        "app_booking_log": [],
    }
    book_calls = []

    def fake_save_state(value):
        snapshot = copy.deepcopy(value)
        state.clear()
        state.update(snapshot)

    monkeypatch.setattr(scheduler_mod, "load_state", lambda: state)
    monkeypatch.setattr(scheduler_mod, "save_state", fake_save_state)
    monkeypatch.setattr(
        scheduler_mod,
        "_api_scan",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-off should not scan")),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "_rec_booking_sessions",
        lambda st: [
            {"account_index": 1, "jwt": "jwt-1", "participant_user_id": "user-1"},
            {"account_index": 2, "jwt": "jwt-2", "participant_user_id": "user-2"},
        ],
    )
    monkeypatch.setattr(scheduler_mod, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scheduler_mod, "_apply_booked_slots", lambda st, booked: None)
    monkeypatch.setattr(scheduler_mod, "_notify_booked_slots", lambda booked: None)

    def fake_book_slot(jwt, target_day, target_time, court, transaction_log=None, participant_user_id=None):
        book_calls.append((jwt, participant_user_id, target_day.isoformat(), target_time, court))
        return True

    monkeypatch.setattr(scheduler_mod, "book_slot_api", fake_book_slot)

    scheduler_mod._run_one_off_probe("2026-07-10", "9:00 AM", max_bookings=2, courts=["6", "5"])

    assert book_calls == [
        ("jwt-1", "user-1", "2026-07-10", "9:00 AM", "6"),
        ("jwt-2", "user-2", "2026-07-10", "9:00 AM", "5"),
    ]
    assert state["last_scan_kind"] == "one_off_probe"
    assert len(state["app_booking_log"]) == 2


def test_book_slot_api_reservation_body_uses_site_activity_ids(monkeypatch):
    import rec_api as rec_api_mod

    bodies = []

    def fake_rec_api(url, method="GET", body=None, jwt=""):
        bodies.append(body)
        return 409, {"type": "E_CONFLICT"}

    monkeypatch.setattr(rec_api_mod, "_rec_api", fake_rec_api)

    assert rec_api_mod.book_slot_api(
        "jwt", date(2026, 10, 25), "8:00 AM", "6", participant_user_id="user-1",
    ) is False
    assert bodies == [{
        "customerUserId": "user-1",
        "siteActivityIds": ["81a3ab35-8551-4d1c-9f68-711908b482cc"],
        "from": {"date": "2026-10-25", "time": "08:00:00"},
        "participantUserId": "user-1",
        "to": {"date": "2026-10-25", "time": "09:00:00"},
    }]


def test_book_slot_api_payment_body_includes_cart_version(monkeypatch):
    import rec_api as rec_api_mod

    calls = []

    def fake_rec_api(url, method="GET", body=None, jwt=""):
        calls.append((url, body))
        if url.endswith("/reservations"):
            return 201, {"data": {
                "id": "order-id",
                "total": 400,
                "maxCreditAdjustmentAllowed": 0,
                "updatedAt": "2026-10-11T15:00:03.433Z",
                "items": [
                    {"id": "item-1", "updatedAt": "2026-10-11T15:00:03.500Z"},
                    {"id": "item-2", "updatedAt": "2026-10-11T15:00:03.100Z"},
                ],
            }}
        return 200, {"data": {"status": "paid"}}

    monkeypatch.setattr(rec_api_mod, "_rec_api", fake_rec_api)

    assert rec_api_mod.book_slot_api(
        "jwt", date(2026, 10, 25), "8:00 AM", "6", participant_user_id="user-1",
    ) is True
    pay_body = calls[1][1]["data"]
    assert pay_body["version"] == 1791730803500
    assert pay_body["auditAffectedOrderItemIds"] == ["item-1", "item-2"]


def test_book_slot_api_retries_without_customer_user_id_on_invalid_body(monkeypatch):
    import rec_api as rec_api_mod

    bodies = []

    def fake_rec_api(url, method="GET", body=None, jwt=""):
        if url.endswith("/reservations"):
            bodies.append(body)
            if "customerUserId" in body:
                return 422, {"type": "E_INVALID_BODY"}
            return 201, {"data": {"id": "order-id", "total": 400, "maxCreditAdjustmentAllowed": 0}}
        return 200, {"data": {"status": "paid"}}

    monkeypatch.setattr(rec_api_mod, "_rec_api", fake_rec_api)

    transaction_log = {}
    assert rec_api_mod.book_slot_api(
        "jwt", date(2026, 10, 25), "8:00 AM", "6",
        transaction_log=transaction_log, participant_user_id="user-1",
    ) is True
    assert len(bodies) == 2
    assert "customerUserId" not in bodies[1]
    assert bodies[1]["siteActivityIds"] == bodies[0]["siteActivityIds"]
    assert transaction_log["reservation"]["response"]["status"] == 422
    assert transaction_log["reservation_fallback"]["response"]["status"] == 201


def test_book_slot_api_trusts_paid_payment_status(monkeypatch):
    import rec_api as rec_api_mod

    api_calls = []

    def fake_rec_api(url, method="GET", body=None, jwt=""):
        api_calls.append((url, method, body))
        if url.endswith("/reservations"):
            return 201, {
                "data": {
                    "id": "order-id",
                    "total": 400,
                    "maxCreditAdjustmentAllowed": 0,
                }
            }
        return 200, {"data": {"status": "paid"}}

    monkeypatch.setattr(rec_api_mod, "_rec_api", fake_rec_api)
    monkeypatch.setattr(
        rec_api_mod,
        "fetch_rec_my_reservations",
        lambda jwt=None: (_ for _ in ()).throw(AssertionError("should not check reservations immediately")),
    )

    transaction_log = {}
    assert rec_api_mod.book_slot_api(
        "jwt",
        date(2026, 6, 21),
        "9:00 AM",
        "5",
        transaction_log=transaction_log,
    ) is True
    assert len(api_calls) == 2
    assert transaction_log["reservation"]["response"]["status"] == 201
    assert transaction_log["payment"]["response"]["body"] == {"data": {"status": "paid"}}
    assert transaction_log["verification"]["confirmed"] is True
    assert transaction_log["verification"]["source"] == "payment"
    assert transaction_log["verification"]["attempts"] == []


def test_book_slot_api_trusts_successful_stripe_confirm(monkeypatch):
    import rec_api as rec_api_mod

    def fake_rec_api(url, method="GET", body=None, jwt=""):
        if url.endswith("/reservations"):
            return 201, {
                "data": {
                    "id": "order-id",
                    "total": 400,
                    "maxCreditAdjustmentAllowed": 0,
                }
            }
        return 200, {
            "data": {"status": "pending"},
            "included": {
                "payments": [{
                    "gatewayData": {
                        "paymentIntentId": "pi_test",
                        "clientSecret": "secret",
                        "paymentMethods": [{"id": "pm_test"}],
                    },
                }],
            },
        }

    monkeypatch.setattr(rec_api_mod, "_rec_api", fake_rec_api)
    monkeypatch.setattr(rec_api_mod, "_stripe_confirm_payment_intent", lambda pi, cs, pm: (200, {"status": "succeeded"}))
    monkeypatch.setattr(
        rec_api_mod,
        "fetch_rec_my_reservations",
        lambda jwt=None: (_ for _ in ()).throw(AssertionError("should not check reservations immediately")),
    )

    transaction_log = {}
    assert rec_api_mod.book_slot_api(
        "jwt",
        date(2026, 6, 21),
        "9:00 AM",
        "5",
        transaction_log=transaction_log,
    ) is True
    assert transaction_log["stripe_confirm"]["body"]["status"] == "succeeded"
    assert transaction_log["verification"]["confirmed"] is True
    assert transaction_log["verification"]["stripe_confirmed"] is True


def test_book_slot_api_confirms_with_saved_card_when_gateway_data_lists_no_methods(monkeypatch):
    import rec_api as rec_api_mod

    confirms = []

    def fake_rec_api(url, method="GET", body=None, jwt=""):
        if url.endswith("/reservations"):
            return 201, {"data": {"id": "order-id", "total": 400, "maxCreditAdjustmentAllowed": 0}}
        if url.endswith("/payment-methods"):
            assert "user-1" in url
            return 200, {"data": [
                {"id": "old", "archivedAt": "2026-01-01T00:00:00Z", "referenceId": "pm_archived"},
                {"id": "cur", "archivedAt": None, "referenceId": "pm_saved"},
            ]}
        return 200, {
            "data": {"status": "pending", "creatorUserId": "user-1"},
            "included": {"payments": [{"gatewayData": {
                "paymentIntentId": "pi_test", "clientSecret": "secret", "paymentMethods": [],
            }}]},
        }

    def fake_confirm(pi, cs, pm):
        confirms.append((pi, cs, pm))
        return 200, {"status": "succeeded"}

    monkeypatch.setattr(rec_api_mod, "_rec_api", fake_rec_api)
    monkeypatch.setattr(rec_api_mod, "STRIPE_PUBLISHABLE_KEY", "pk_test")
    monkeypatch.setattr(rec_api_mod, "_stripe_confirm_payment_intent", fake_confirm)

    transaction_log = {}
    assert rec_api_mod.book_slot_api(
        "jwt", date(2026, 10, 25), "8:00 AM", "6",
        transaction_log=transaction_log, participant_user_id="user-1",
    ) is True
    assert confirms == [("pi_test", "secret", "pm_saved")]
    assert transaction_log["verification"]["stripe_confirmed"] is True


def test_book_slot_api_pays_remaining_amount_when_total_includes_existing_items(monkeypatch):
    import rec_api as rec_api_mod

    api_calls = []

    def fake_rec_api(url, method="GET", body=None, jwt=""):
        api_calls.append((url, method, body))
        if url.endswith("/reservations"):
            return 201, {
                "data": {
                    "id": "order-id",
                    "total": 800,
                    "totalAmountRemaining": 400,
                    "maxCreditAdjustmentAllowed": 800,
                }
            }
        return 200, {"data": {"status": "succeeded"}}

    monkeypatch.setattr(rec_api_mod, "_rec_api", fake_rec_api)

    assert rec_api_mod.book_slot_api(
        "jwt",
        date(2026, 9, 26),
        "9:00 AM",
        "6",
        participant_user_id="user-1",
    ) is True

    payment_body = api_calls[1][2]
    assert payment_body == {
        "data": {
            "payments": [
                {"paymentMethodType": "organization-credit", "amountCents": 400},
            ]
        }
    }


def test_book_slot_api_rejects_unconfirmed_payment(monkeypatch):
    import rec_api as rec_api_mod

    def fake_rec_api(url, method="GET", body=None, jwt=""):
        if url.endswith("/reservations"):
            return 201, {
                "data": {
                    "id": "order-id",
                    "total": 400,
                    "maxCreditAdjustmentAllowed": 0,
                }
            }
        return 200, {"data": {"status": "declined"}}

    monkeypatch.setattr(rec_api_mod, "_rec_api", fake_rec_api)
    monkeypatch.setattr(
        rec_api_mod,
        "fetch_rec_my_reservations",
        lambda jwt=None: (_ for _ in ()).throw(AssertionError("should not check reservations immediately")),
    )

    transaction_log = {}
    assert rec_api_mod.book_slot_api(
        "jwt",
        date(2026, 6, 21),
        "9:00 AM",
        "5",
        transaction_log=transaction_log,
    ) is False
    assert transaction_log["verification"]["confirmed"] is False
    assert transaction_log["verification"]["payment_status"] == "declined"
    assert transaction_log["verification"]["attempts"] == []


def test_handle_force_book_scans_auto_book_slots_and_applies_bookings(monkeypatch):
    import json
    import routes as routes_mod

    state = {
        "auto_book_slots": [{"date": "2026-08-25", "time": "9:00 AM", "count": 1}],
        "availability": {},
        "my_reservations": [],
    }
    saved = []
    scan_calls = []
    applied = []
    notified = []

    monkeypatch.setattr(routes_mod, "AUTO_BOOKING_ENABLED", True)
    monkeypatch.setattr(routes_mod, "date", type("FixedDate", (date,), {
        "today": classmethod(lambda cls: cls(2026, 7, 19)),
    }))
    monkeypatch.setattr(routes_mod, "_auto_book_time_map", lambda slots: {"2026-08-25": ["9:00 AM"]})
    monkeypatch.setattr(routes_mod, "load_state", lambda: state)
    monkeypatch.setattr(routes_mod, "save_state", lambda value: saved.append(value.copy()))

    def fake_api_scan(target_times_by_date=None, auto_book_slots=None, detailed_log=None):
        scan_calls.append((target_times_by_date, auto_book_slots))
        if detailed_log is not None:
            detailed_log.append({"date": "2026-08-25", "time": "9:00 AM", "result": "booked"})
        return (
            {"2026-08-25": {"9:00 AM": {"6": False, "4": True, "5": False}}},
            [{"date": "2026-08-25", "time": "9:00 AM", "court": "6"}],
        )

    monkeypatch.setattr(routes_mod, "_api_scan", fake_api_scan)
    monkeypatch.setattr(routes_mod, "_apply_booked_slots", lambda st, slots: applied.extend(slots))
    monkeypatch.setattr(routes_mod, "_notify_booked_slots", lambda slots: notified.extend(slots))
    monkeypatch.setattr(routes_mod, "sync_rec_my_reservations", lambda st: True)

    response = routes_mod.handle_force_book({})
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert scan_calls == [
        (
            {"2026-08-25": ["9:00 AM"]},
            [{"date": "2026-08-25", "time": "9:00 AM", "count": 1}],
        )
    ]
    assert applied == [{"date": "2026-08-25", "time": "9:00 AM", "court": "6"}]
    assert notified == applied
    assert body["booked_slots"] == applied
    assert body["attempt_log"] == [{"date": "2026-08-25", "time": "9:00 AM", "result": "booked"}]
    assert saved[-1]["availability"]["2026-08-25"]["9:00 AM"] == {"6": False, "4": True, "5": False}


def test_handle_cancel_reservation_calls_rec_cancel_and_syncs(monkeypatch):
    import json
    import routes as routes_mod

    state = {
        "my_reservations": [
            {
                "date": "2026-07-25",
                "time": "9:00 AM",
                "court": "6",
                "account_index": 2,
                "account_email": "two@example.com",
                "booking_id": "booking-2",
                "reservation_id": "reservation-2",
                "facility_rental_id": "facility-2",
            }
        ],
    }
    saved = []
    cancel_calls = []
    sync_calls = []
    calendar_deletes = []

    def fake_sync(st):
        sync_calls.append(len(sync_calls) + 1)
        if len(sync_calls) == 2:
            st["my_reservations"] = []
            st["my_reservations_synced_at"] = "2026-07-19T12:00:00.000Z"
        return True

    def fake_cancel(jwt, booking_id, transaction_log=None):
        cancel_calls.append((jwt, booking_id))
        if transaction_log is not None:
            transaction_log["cancel"] = {"response": {"status": 200}}
        return True, {"data": {"id": booking_id, "status": "canceled"}}

    monkeypatch.setattr(routes_mod, "load_state", lambda: state)
    monkeypatch.setattr(routes_mod, "save_state", lambda value: saved.append(json.loads(json.dumps(value))))
    monkeypatch.setattr(routes_mod, "sync_rec_my_reservations", fake_sync)
    monkeypatch.setattr(
        routes_mod,
        "_configured_rec_accounts",
        lambda: [{"index": 2, "email": "two@example.com", "password": "pw"}],
    )
    monkeypatch.setattr(routes_mod, "_get_cached_jwt", lambda st, account_index: "jwt-2")
    monkeypatch.setattr(routes_mod, "cancel_booking_api", fake_cancel)
    monkeypatch.setattr(routes_mod, "enqueue_calendar_event_delete", lambda slot: calendar_deletes.append(slot.copy()))

    response = routes_mod.handle_cancel_reservation({
        "body": json.dumps({
            "date": "2026-07-25",
            "time": "9:00 AM",
            "court": "6",
            "account_index": 2,
            "booking_id": "booking-2",
        })
    })
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert cancel_calls == [("jwt-2", "booking-2")]
    assert sync_calls == [1, 2]
    assert body["ok"] is True
    assert body["slot"]["booking_id"] == "booking-2"
    assert calendar_deletes == [{
        "date": "2026-07-25",
        "time": "9:00 AM",
        "court": "6",
        "account_index": 2,
        "account_email": "two@example.com",
        "booking_id": "booking-2",
        "reservation_id": "reservation-2",
        "facility_rental_id": "facility-2",
    }]
    assert saved[-1]["my_reservations"] == []


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
    assert body["action"] == "create"
    assert body["summary"] == "Pickleball Court 6"
    assert body["start_iso"] == "2026-06-01T09:00:00-07:00"
    assert body["end_iso"] == "2026-06-01T10:00:00-07:00"
    assert body["time_zone"] == "America/Los_Angeles"


def test_calendar_delete_body_targets_same_source_id():
    slot = {"date": "2026-06-01", "time": "9:00 AM", "court": "6"}
    body = _calendar_delete_body(slot)
    assert body["action"] == "delete"
    assert body["source_id"] == _calendar_event_id(slot)
    assert body["start_iso"] == "2026-06-01T09:00:00-07:00"
    assert body["end_iso"] == "2026-06-01T10:00:00-07:00"


def test_calendar_event_body_invites_configured_attendees(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_ATTENDEES", "player1@example.com")
    slot = {"date": "2026-06-01", "time": "9:00 AM", "court": "6"}
    body = _calendar_event_body(slot)
    assert body["attendees"] == ["player1@example.com"]


def test_calendar_event_body_includes_apps_script_secret(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_SECRET", "test-secret")
    slot = {"date": "2026-06-01", "time": "9:00 AM", "court": "6"}
    body = _calendar_event_body(slot)
    assert body["secret"] == "test-secret"


# ── scheduler ─────────────────────────────────────────────────────────────────

def test_release_probe_session_blind_books_weekend_target_and_persists_log(monkeypatch):
    import copy
    import scheduler as scheduler_mod
    from config import PT

    class FixedDateTime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            if cls.calls <= 2:
                value = datetime(2026, 5, 30, 7, 58, 0, tzinfo=PT)
            else:
                value = datetime(2026, 5, 30, 8, 2, 1, tzinfo=PT)
            return value.astimezone(tz) if tz is not None else value

    state = {
        "cached_jwt": "jwt",
        "cached_jwt_expires_at": "2026-05-30T16:30:00.000Z",
        "my_reservations": [],
        "auto_book_slots": [
            {"date": "2026-06-13", "time": "8:00 AM"},
            {"date": "2026-06-13", "time": "9:00 AM"},
        ],
        "release_probe_log": [],
        "app_booking_log": [],
    }
    saved_states = []
    book_calls = []

    def fake_save_state(value):
        snapshot = copy.deepcopy(value)
        state.clear()
        state.update(snapshot)
        saved_states.append(snapshot)

    monkeypatch.setattr(scheduler_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduler_mod, "load_state", lambda: state)
    monkeypatch.setattr(scheduler_mod, "save_state", fake_save_state)
    monkeypatch.setattr(
        scheduler_mod,
        "_rec_booking_sessions",
        lambda st: [
            {"account_index": 1, "jwt": "jwt-1", "participant_user_id": "user-1"},
            {"account_index": 2, "jwt": "jwt-2", "participant_user_id": "user-2"},
        ],
    )
    monkeypatch.setattr(scheduler_mod, "_get_cached_jwt", lambda value: "jwt")
    monkeypatch.setattr(scheduler_mod, "_firebase_login", lambda: (_ for _ in ()).throw(AssertionError("login should not run")))
    monkeypatch.setattr(scheduler_mod, "_apply_booked_slots", lambda st, booked: None)
    monkeypatch.setattr(scheduler_mod, "_notify_booked_slots", lambda booked: None)
    monkeypatch.setattr(scheduler_mod, "_enqueue_work", lambda *args, **kwargs: False)
    monkeypatch.setattr(scheduler_mod, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scheduler_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(scheduler_mod, "_api_scan", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("probe scan should not run")))

    def fake_book_slot(jwt, slot_date, time_text, court, transaction_log=None, participant_user_id=None):
        book_calls.append((jwt, participant_user_id, slot_date.isoformat(), time_text, court))
        transaction_log["payment"] = {"response": {"status": 200}}
        transaction_log["verification"] = {"confirmed": True}
        state["my_reservations"].append({
            "date": slot_date.isoformat(),
            "time": time_text,
            "court": court,
        })
        return True

    monkeypatch.setattr(
        scheduler_mod,
        "book_slot_api",
        fake_book_slot,
    )

    scheduler_mod._run_release_probe_session()

    assert set(book_calls) == {
        ("jwt-1", "user-1", "2026-06-13", "8:00 AM", "6"),
        ("jwt-2", "user-2", "2026-06-13", "9:00 AM", "6"),
    }
    assert saved_states[-1]["last_release_probe_session"] is not None
    booked_entries = [
        entry for entry in saved_states[-1]["release_probe_log"]
        if entry.get("phase") == "burst" and entry.get("result") == "booked"
    ]
    assert sorted(entry["booked"][0] for entry in booked_entries) == [
        "2026-06-13 8:00 AM Court 6",
        "2026-06-13 9:00 AM Court 6",
    ]
    assert any(
        entry.get("phase") == "burst"
        and entry.get("booked") == [
            "2026-06-13 8:00 AM Court 6",
        ]
        and entry.get("booking_attempts")
        and entry["booking_attempts"][0]["transaction"]["verification"]["confirmed"] is True
        and entry.get("targets")
        for entry in saved_states[-1]["release_probe_log"]
    )


def test_release_probe_session_blind_books_entire_two_minute_window(monkeypatch):
    import copy
    import scheduler as scheduler_mod
    from config import PT

    class FixedDateTime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            if cls.calls == 1:
                value = datetime(2026, 5, 30, 7, 58, 0, tzinfo=PT)
            elif cls.calls == 2:
                value = datetime(2026, 5, 30, 8, 0, 10, tzinfo=PT)
            else:
                value = datetime(2026, 5, 30, 8, 2, 1, tzinfo=PT)
            return value.astimezone(tz) if tz is not None else value

    state = {
        "cached_jwt": "jwt",
        "cached_jwt_expires_at": "2026-05-30T16:30:00.000Z",
        "my_reservations": [],
        "auto_book_slots": [
            {"date": "2026-06-13", "time": "8:00 AM"},
            {"date": "2026-06-13", "time": "9:00 AM"},
        ],
        "release_probe_log": [],
        "app_booking_log": [],
        "auto_book_failures": [],
    }
    saved_states = []
    book_calls = []

    def fake_save_state(value):
        snapshot = copy.deepcopy(value)
        state.clear()
        state.update(snapshot)
        saved_states.append(snapshot)

    monkeypatch.setattr(scheduler_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduler_mod, "load_state", lambda: state)
    monkeypatch.setattr(scheduler_mod, "save_state", fake_save_state)
    monkeypatch.setattr(
        scheduler_mod,
        "_rec_booking_sessions",
        lambda st: [{"account_index": 1, "jwt": "jwt-1", "participant_user_id": "user-1"}],
    )
    monkeypatch.setattr(scheduler_mod, "_get_cached_jwt", lambda value: "jwt")
    monkeypatch.setattr(scheduler_mod, "_firebase_login", lambda: (_ for _ in ()).throw(AssertionError("login should not run")))
    monkeypatch.setattr(scheduler_mod, "_apply_booked_slots", lambda st, booked: None)
    monkeypatch.setattr(scheduler_mod, "_notify_booked_slots", lambda booked: None)
    monkeypatch.setattr(scheduler_mod, "_enqueue_work", lambda *args, **kwargs: False)
    monkeypatch.setattr(scheduler_mod, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scheduler_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(scheduler_mod, "_api_scan", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("probe scan should not run")))

    def fake_book_slot(jwt, slot_date, time_text, court, transaction_log=None, participant_user_id=None):
        book_calls.append((slot_date.isoformat(), time_text, court))
        transaction_log["reservation"] = {"response": {"status": 409}}
        return False

    monkeypatch.setattr(scheduler_mod, "book_slot_api", fake_book_slot)

    scheduler_mod._run_release_probe_session()

    assert len(book_calls) == 24
    assert book_calls[:6] == [
        ("2026-06-13", "8:00 AM", "6"),
        ("2026-06-13", "9:00 AM", "6"),
        ("2026-06-13", "8:00 AM", "4"),
        ("2026-06-13", "9:00 AM", "4"),
        ("2026-06-13", "8:00 AM", "5"),
        ("2026-06-13", "9:00 AM", "5"),
    ]
    assert book_calls[6:12] == [
        ("2026-06-13", "8:00 AM", "6"),
        ("2026-06-13", "9:00 AM", "6"),
        ("2026-06-13", "8:00 AM", "4"),
        ("2026-06-13", "9:00 AM", "4"),
        ("2026-06-13", "8:00 AM", "5"),
        ("2026-06-13", "9:00 AM", "5"),
    ]
    burst_failures = [
        entry for entry in saved_states[-1]["release_probe_log"]
        if entry.get("phase") == "burst" and entry.get("result") == "failed"
    ]
    assert len(burst_failures) == 4


def test_weekend_late_probe_uses_blind_booking(monkeypatch):
    import copy
    import scheduler as scheduler_mod

    state = {
        "my_reservations": [],
        "app_booking_log": [],
        "auto_book_failures": [],
    }
    saved_states = []
    book_calls = []

    def fake_save_state(value):
        snapshot = copy.deepcopy(value)
        state.clear()
        state.update(snapshot)
        saved_states.append(snapshot)

    monkeypatch.setattr(scheduler_mod, "load_state", lambda: state)
    monkeypatch.setattr(scheduler_mod, "save_state", fake_save_state)
    monkeypatch.setattr(
        scheduler_mod,
        "_rec_booking_sessions",
        lambda st: [{"account_index": 1, "jwt": "jwt-1", "participant_user_id": "user-1"}],
    )
    monkeypatch.setattr(scheduler_mod, "_apply_booked_slots", lambda st, booked: None)
    monkeypatch.setattr(scheduler_mod, "_notify_booked_slots", lambda booked: None)
    monkeypatch.setattr(scheduler_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(scheduler_mod, "_api_scan", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("late retry should not scan")))

    def fake_book_slot(jwt, slot_date, time_text, court, transaction_log=None, participant_user_id=None):
        book_calls.append((jwt, participant_user_id, slot_date.isoformat(), time_text, court))
        return len(book_calls) == 1

    monkeypatch.setattr(scheduler_mod, "book_slot_api", fake_book_slot)

    scheduler_mod._run_weekend_late_probes("2026-06-13", "9:00 AM")

    assert book_calls == [("jwt-1", "user-1", "2026-06-13", "9:00 AM", "6")]
    assert saved_states[-1]["app_booking_log"][0]["date"] == "2026-06-13"


def test_release_probe_session_skips_weekday_before_login(monkeypatch):
    import scheduler as scheduler_mod
    from config import PT

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 6, 8, 7, 58, 0, tzinfo=PT)
            return value.astimezone(tz) if tz is not None else value

    monkeypatch.setattr(scheduler_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(
        scheduler_mod,
        "load_state",
        lambda: (_ for _ in ()).throw(AssertionError("state should not be loaded")),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "_firebase_login",
        lambda: (_ for _ in ()).throw(AssertionError("login should not run")),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "send_telegram",
        lambda text: (_ for _ in ()).throw(AssertionError("message should not be sent")),
    )

    scheduler_mod._run_release_probe_session()


def test_release_probe_session_skips_when_no_weekend_targets_checked(monkeypatch):
    """Empty auto_book_slots for the target weekend must never fall back to a
    hardcoded default — it means the user decided not to book that weekend."""
    import scheduler as scheduler_mod
    from config import PT

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 5, 30, 7, 58, 0, tzinfo=PT)
            return value.astimezone(tz) if tz is not None else value

    state = {
        "auto_book_slots": [{"date": "2026-06-20", "time": "9:00 AM"}],  # a different date
        "release_probe_log": [],
    }

    monkeypatch.setattr(scheduler_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduler_mod, "load_state", lambda: state)
    monkeypatch.setattr(
        scheduler_mod,
        "_rec_booking_sessions",
        lambda st: (_ for _ in ()).throw(AssertionError("should not log in")),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "book_slot_api",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not book anything")),
    )

    scheduler_mod._run_release_probe_session()


def test_booking_agent_weekday_report_is_short_and_skips_model(monkeypatch):
    import booking_agent as booking_agent_mod
    from config import PT

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 6, 8, 8, 30, 0, tzinfo=PT)
            return value.astimezone(tz) if tz is not None else value

    messages = []
    monkeypatch.setattr(booking_agent_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(booking_agent_mod, "load_state", lambda: {"app_booking_log": []})
    monkeypatch.setattr(booking_agent_mod, "send_telegram", messages.append)
    monkeypatch.setattr(
        booking_agent_mod.anthropic,
        "Anthropic",
        lambda: (_ for _ in ()).throw(AssertionError("model should not run")),
    )

    assert booking_agent_mod.run_agent("report") is True
    assert messages == []


def test_booking_agent_prep_skips_model_when_nothing_actionable(monkeypatch):
    """Mon-Thu: candidates/imminent_target are provably always empty (see
    ScheduleExpression comment in infra/pickleball-monitor.yaml), so the prep
    phase must not spend a model call."""
    import booking_agent as booking_agent_mod
    from config import PT

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 6, 9, 7, 30, 0, tzinfo=PT)  # a Tuesday
            return value.astimezone(tz) if tz is not None else value

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 6, 9)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(booking_agent_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(booking_agent_mod, "date", FixedDate)
    monkeypatch.setattr(booking_agent_mod, "load_state", lambda: {
        "auto_book_slots": [], "watched_slots": [], "weekend_target_prefilled_dates": [],
        "booking_history": [],
    })
    monkeypatch.setattr(
        booking_agent_mod.anthropic,
        "Anthropic",
        lambda: (_ for _ in ()).throw(AssertionError("model should not run")),
    )

    assert booking_agent_mod.run_agent("prep") is True


def test_saturday_of_returns_the_weekends_saturday():
    import booking_agent as booking_agent_mod

    saturday = date(2026, 6, 13)
    sunday = date(2026, 6, 14)
    assert booking_agent_mod._saturday_of(saturday) == saturday
    assert booking_agent_mod._saturday_of(sunday) == saturday


def test_weekend_is_holiday_adjacent_true_for_july_4th_weekend():
    import booking_agent as booking_agent_mod

    # 2026-07-04 is a Saturday.
    assert booking_agent_mod._weekend_is_holiday_adjacent(date(2026, 7, 4)) is True


def test_weekend_is_holiday_adjacent_false_for_ordinary_weekend():
    import booking_agent as booking_agent_mod

    assert booking_agent_mod._weekend_is_holiday_adjacent(date(2026, 6, 13)) is False


def test_get_prep_context_flags_candidates_and_imminent_target(monkeypatch):
    import booking_agent as booking_agent_mod
    from config import PT

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 6, 13, 7, 30, 0, tzinfo=PT)  # a Saturday
            return value.astimezone(tz) if tz is not None else value

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 6, 13)

    state = {
        "auto_book_slots": [{"date": "2026-06-28", "time": "9:00 AM"}],  # +15 days, a Sunday
        "watched_slots": [],
        "weekend_target_prefilled_dates": ["2026-07-04"],  # +21 days, already prefilled
        "booking_history": [],
    }

    monkeypatch.setattr(booking_agent_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(booking_agent_mod, "date", FixedDate)
    monkeypatch.setattr(booking_agent_mod, "load_state", lambda: state)

    context = booking_agent_mod._get_prep_context()

    by_date = {c["date"]: c for c in context["candidates"]}
    assert by_date["2026-07-04"]["already_prefilled"] is True
    assert by_date["2026-07-04"]["is_holiday_adjacent"] is True  # July 4th weekend
    assert by_date["2026-07-05"]["already_prefilled"] is False

    assert context["imminent_target"]["date"] == "2026-06-28"
    assert context["imminent_target"]["checked_auto_book_times"] == ["9:00 AM"]
    assert context["imminent_target"]["nothing_queued"] is False


def test_set_weekend_targets_tool_writes_and_refuses_to_overwrite(monkeypatch):
    import booking_agent as booking_agent_mod

    state = {"auto_book_slots": [], "watched_slots": [], "weekend_target_prefilled_dates": []}
    monkeypatch.setattr(booking_agent_mod, "load_state", lambda: state)
    monkeypatch.setattr(booking_agent_mod, "save_state", lambda s: state.update(s))

    result = booking_agent_mod._set_weekend_targets_tool({
        "date": "2026-06-13",
        "auto_book_times": ["8:00 AM", "9:00 AM", "10:00 AM"],
        "watch_times": [],
    })
    assert result["ok"] is True
    assert len(state["auto_book_slots"]) == 3

    # Second call for the same date must be a no-op, even with different args.
    result2 = booking_agent_mod._set_weekend_targets_tool({
        "date": "2026-06-13",
        "auto_book_times": [],
        "watch_times": ["8:00 AM", "9:00 AM", "10:00 AM"],
    })
    assert result2["ok"] is False
    assert len(state["auto_book_slots"]) == 3  # unchanged


def test_weekday_report_includes_booking_made_today():
    import booking_agent as booking_agent_mod
    from config import PT

    now_pt = datetime(2026, 6, 8, 8, 30, 0, tzinfo=PT)
    state = {
        "app_booking_log": [
            {
                "booked_at": "2026-06-08T15:01:00+00:00",
                "date": "2026-06-22",
                "time": "9:00 AM",
                "court": "6",
            },
            {
                "booked_at": "2026-06-07T15:01:00+00:00",
                "date": "2026-06-21",
                "time": "9:00 AM",
                "court": "4",
            },
        ]
    }

    assert booking_agent_mod._weekday_report_text(state, now_pt) == (
        "Booked: Court 6 2026-06-22 9:00 AM."
    )


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


def test_should_run_scheduled_tick_daily_fires_only_at_10pm_pt():
    from config import PT

    state = {"scan_interval_hours": 24.0}
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 22, 0, tzinfo=PT)) is True
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 22, 15, tzinfo=PT)) is False
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 9, 0, tzinfo=PT)) is False
    # Same instant expressed in UTC must still resolve to 10 PM PT.
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 22, 0, tzinfo=PT).astimezone(timezone.utc)) is True


def test_effective_scan_interval_speeds_up_when_checklist_nonempty():
    empty_state = {"scan_interval_hours": 24.0, "watched_slots": [], "auto_book_slots": []}
    assert _effective_scan_interval_hours(empty_state) == 24.0

    watched_state = {
        "scan_interval_hours": 24.0,
        "watched_slots": [{"date": "2026-10-17", "time": "9:00 AM", "court": "6"}],
        "auto_book_slots": [],
    }
    assert _effective_scan_interval_hours(watched_state) == 2.0

    auto_book_state = {
        "scan_interval_hours": 24.0,
        "watched_slots": [],
        "auto_book_slots": [{"date": "2026-10-17", "time": "9:00 AM"}],
    }
    assert _effective_scan_interval_hours(auto_book_state) == 2.0

    # A manual setting already faster than the active ceiling is left alone.
    fast_state = {
        "scan_interval_hours": 1.0,
        "watched_slots": [{"date": "2026-10-17", "time": "9:00 AM", "court": "6"}],
        "auto_book_slots": [],
    }
    assert _effective_scan_interval_hours(fast_state) == 1.0


def test_should_run_scheduled_tick_speeds_up_to_2h_when_watching():
    from config import PT

    state = {
        "scan_interval_hours": 24.0,
        "watched_slots": [{"date": "2026-10-17", "time": "9:00 AM", "court": "6"}],
        "auto_book_slots": [],
    }
    # Would only fire at 10 PM PT if still daily; with something watched it
    # must follow the every-2-hours cadence instead.
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 8, 0, tzinfo=PT)) is True
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 9, 0, tzinfo=PT)) is False
    assert _should_run_scheduled_tick(state, datetime(2026, 5, 1, 10, 0, tzinfo=PT)) is True


def test_booking_history_records_synced_reservations():
    import state as state_mod

    state = {
        "my_reservations": [
            {"date": "2026-08-15", "time": "9:00 AM", "court": "6", "account_index": 1},
            {"date": "2026-08-15", "time": "9:00 AM", "court": "6", "account_index": 1},
        ],
    }

    assert state_mod.record_booking_history_from_reservations(state) is True
    assert state["booking_history"] == [
        {
            "date": "2026-08-15",
            "weekday": "Saturday",
            "time": "9:00 AM",
            "court": "6",
            "source": "rec_us_sync",
            "first_seen_at": state["booking_history"][0]["first_seen_at"],
            "last_seen_at": state["booking_history"][0]["last_seen_at"],
            "account_index": "1",
        }
    ]
