from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

from config import PT
from state import _enqueue_work

APPS_SCRIPT_URL_ENV = "GOOGLE_APPS_SCRIPT_CALENDAR_URL"
APPS_SCRIPT_SECRET_ENV = "GOOGLE_APPS_SCRIPT_SECRET"
ATTENDEES_ENV = "GOOGLE_CALENDAR_ATTENDEES"
REQUEST_TIMEOUT = 10


def _calendar_configured() -> bool:
    return bool(os.environ.get(APPS_SCRIPT_URL_ENV) and os.environ.get(APPS_SCRIPT_SECRET_ENV))


def _attendees() -> list[str]:
    return [
        email.strip()
        for email in os.environ.get(ATTENDEES_ENV, "").split(",")
        if email.strip()
    ]


def _calendar_event_id(slot: dict) -> str:
    key = f"{slot['date']}|{slot['time']}|{slot['court']}"
    return "pb" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]


def _slot_start_end(slot: dict) -> tuple[datetime, datetime]:
    start = datetime.strptime(
        f"{slot['date']} {slot['time']}", "%Y-%m-%d %I:%M %p"
    ).replace(tzinfo=PT)
    return start, start + timedelta(hours=1)


def _calendar_event_body(slot: dict) -> dict:
    start, end = _slot_start_end(slot)
    source_id = _calendar_event_id(slot)
    return {
        "secret": os.environ.get(APPS_SCRIPT_SECRET_ENV, ""),
        "source_id": source_id,
        "date": slot["date"],
        "time": slot["time"],
        "court": str(slot["court"]),
        "summary": f"Pickleball Court {slot['court']}",
        "location": "Foster City Pickleball Courts",
        "description": f"Auto-created after successful rec.us booking.\nSource ID: {source_id}",
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "time_zone": "America/Los_Angeles",
        "attendees": _attendees(),
    }


def enqueue_calendar_event(slot: dict) -> None:
    if not _calendar_configured():
        return
    try:
        queued = _enqueue_work("calendar_event", {"slot": slot})
        if not queued:
            print(f"Google Calendar work queue is not configured; event was not queued for {slot}.")
    except Exception as exc:
        print(f"Google Calendar enqueue failed for {slot}: {exc}")


def create_calendar_event(slot: dict) -> bool:
    if not _calendar_configured():
        print("Google Apps Script Calendar webhook is not configured; skipping event creation.")
        return False

    payload = json.dumps(_calendar_event_body(slot)).encode("utf-8")
    req = Request(
        os.environ[APPS_SCRIPT_URL_ENV],
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        response_body = resp.read().decode("utf-8")

    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid Apps Script response: {response_body[:200]}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Apps Script Calendar error: {result}")

    duplicate = "duplicate " if result.get("duplicate") else ""
    print(f"Google Calendar {duplicate}event handled for {slot}.")
    return True


def handle_calendar_event_work(slot: dict) -> None:
    try:
        create_calendar_event(slot)
    except Exception as exc:
        print(f"Google Calendar event creation failed for {slot}: {exc}")
