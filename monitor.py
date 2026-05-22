#!/usr/bin/env python3
"""
rec.us Foster City reservation availability scanner.

Lambda entry point — all logic lives in the sub-modules:
  config.py, state.py, notify.py, http_utils.py, rec_api.py,
  browser.py, scanner.py, booking.py, scheduler.py, telegram.py, routes.py
"""

import asyncio
import json

from config import CORS_HEADERS, DEFAULT_TIME_FILTER, load_check_dates
from http_utils import (
    _build_sync_redirect, _check_auth, _is_lambda_url_request,
    get_method, get_path, parse_requested_dates, parse_time_filter,
    parse_query_params,
)
from state import load_state, save_state
from notify import notify
from rec_api import sync_rec_my_reservations
from scanner import _api_scan
from booking import _apply_booked_slots, _notify_booked_slots
from scheduler import (
    _auto_book_time_map, _handle_queue_event, _is_sqs_event,
    _queue_release_probe_session_if_needed, _queued_scheduled_probe_is_current,
    _queue_next_scheduled_probe, _clear_queued_scheduled_probe,
    _run_full_refresh_worker, _run_scheduled_worker, _run_targeted_daily_scan,
    _should_run_scheduled_tick,
)
from telegram import handle_telegram
from routes import (
    handle_auto_book, handle_auto_watch_weekends, handle_focus_newest_weekend,
    handle_my_reservations, handle_my_reservations_refresh, handle_scan_interval,
    handle_state, handle_watch,
)


def handler(event, context):
    if _is_sqs_event(event):
        _handle_queue_event(event)
        return

    if event.get("_targeted_daily_scan"):
        _run_targeted_daily_scan()
        return

    if event.get("_scheduled"):
        _run_full_refresh_worker(force=True)
        return

    if event.get("source") == "aws.events":
        state = load_state()
        interval = float(state.get("scan_interval_hours") or 1.0)
        _queue_release_probe_session_if_needed(state)
        state = load_state()
        interval = float(state.get("scan_interval_hours") or 1.0)
        from datetime import datetime, timezone
        if not _should_run_scheduled_tick(state, datetime.now(tz=timezone.utc)):
            print(f"Skipping tick: interval={interval} hr.")
            return
        if interval < 0.25:
            state = load_state()
            if _queued_scheduled_probe_is_current(state, interval):
                print(f"Skipping tick: scheduled probe already queued for {state.get('queued_scheduled_probe_at')}.")
                return
            _run_scheduled_worker()
            state = load_state()
            _queue_next_scheduled_probe(float(state.get("scan_interval_hours") or 1.0), state=state, force=True)
        else:
            if state.get("queued_scheduled_probe_token"):
                _clear_queued_scheduled_probe(state)
                save_state(state)
            _run_targeted_daily_scan()
        return

    method = get_method(event)
    path   = get_path(event)

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    if path == "/telegram" and method == "POST":
        return handle_telegram(event)

    if not _check_auth(event):
        return {
            "statusCode": 401,
            "headers": {**CORS_HEADERS, "WWW-Authenticate": 'Basic realm="pickleball"'},
            "body": json.dumps({"error": "Unauthorized"}),
        }

    if path == "/state" and method == "GET":
        return handle_state(event)

    if path == "/watch" and method == "PUT":
        return handle_watch(event)

    if path == "/my-reservations" and method == "GET":
        return handle_my_reservations_refresh(event)

    if path == "/my-reservations" and method == "PUT":
        return handle_my_reservations(event)

    if path == "/scan-interval" and method == "PUT":
        return handle_scan_interval(event)

    if path == "/auto-watch-weekends" and method == "PUT":
        return handle_auto_watch_weekends(event)

    if path == "/focus-newest-weekend" and method == "PUT":
        return handle_focus_newest_weekend(event)

    if path == "/auto-book" and method == "PUT":
        return handle_auto_book(event)

    if path == "/force-scan" and method == "POST":
        _run_full_refresh_worker(force=True)
        return handle_state(event)

    if path in ("/scan", "/prod/scan"):
        params = parse_query_params(event)
        mode   = (params.get("mode") or "sync").strip().lower()

        if mode != "sync":
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Async scans have been removed; use mode=sync."}),
            }
        if not _is_lambda_url_request(event):
            return _build_sync_redirect(event)
        try:
            targets     = parse_requested_dates(event)
            target_time = parse_time_filter(event, default=None)
        except ValueError as exc:
            return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": str(exc)})}
        state = load_state()
        auto_book_slots = state.get("auto_book_slots") or []
        booked_slots = []
        auto_book_avail = {}
        auto_book_targets = _auto_book_time_map(auto_book_slots)
        if auto_book_targets:
            auto_book_avail, booked_slots = _api_scan(
                target_times_by_date=auto_book_targets,
                auto_book_slots=auto_book_slots,
            )
        if auto_book_avail or booked_slots:
            state = load_state()
            if auto_book_avail:
                from state import _normalize_time_availability
                availability = state.get("availability", {})
                for date_str, day_avail in auto_book_avail.items():
                    day_state = availability.get(date_str, {})
                    for time_text, court_avail in day_avail.items():
                        day_state[time_text] = _normalize_time_availability(court_avail)
                    availability[date_str] = day_state
                state["availability"] = availability
            if booked_slots:
                _apply_booked_slots(state, booked_slots)
            save_state(state)
        if booked_slots:
            _notify_booked_slots(booked_slots)
        from browser import main as browser_main
        results = asyncio.run(browser_main(targets=targets, target_time=target_time))
        if booked_slots:
            _mark_booked_slots_in_scan_results(
                targets=targets,
                results=results,
                booked_slots=booked_slots,
            )
        payload = build_scan_payload(targets=targets, target_time=target_time, results=results)
        payload["booked_slots"] = booked_slots
        state = load_state()
        sync_rec_my_reservations(state)
        save_state(state)
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps(payload)}

    return {"statusCode": 404, "headers": CORS_HEADERS, "body": json.dumps({"error": "Not found"})}


if __name__ == "__main__":
    import asyncio
    from browser import main as browser_main
    targets = load_check_dates()
    results = asyncio.run(browser_main(targets=targets, target_time=DEFAULT_TIME_FILTER))
    if results:
        lines = [
            f"{day} {s['time']} -> {', '.join(s['courts']) or '(could not read courts)'}"
            for day, slots in results.items()
            for s in slots
        ]
        msg = f"Pickleball {DEFAULT_TIME_FILTER} available:\n" + "\n".join(lines)
        notify(msg)
