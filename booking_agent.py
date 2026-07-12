"""Booking agent: reporting for the 8am slot-release session.

report (8:30am PT, EventBridge rule): read probe log + reservations, send results.

The release probe session books the new day (today + 14) directly; no prep
phase is needed to pick targets.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import anthropic

from config import PT, REPORT_EMAIL
from notify import send_report_email, send_telegram
from state import load_state

MODEL = "claude-haiku-4-5"

# ── Tools ─────────────────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "get_context",
        "description": (
            "Return current state relevant to booking: today's date, the new day opening "
            "14 days out, existing reservations, current auto_book_slots, watched_slots, "
            "seen_open_days (when each date was first detected open), and the most recent "
            "release probe log entries with detailed booking attempts."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "send_message",
        "description": "Send a Telegram message to the user.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "done",
        "description": "Signal that the agent has finished its work for this phase.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ── Tool implementations ───────────────────────────────────────────────────────

def _get_context() -> dict:
    state = load_state()
    today = date.today()
    new_day = today + timedelta(days=14)

    upcoming_weekends = [
        (today + timedelta(days=i)).isoformat()
        for i in range(1, 16)
        if (today + timedelta(days=i)).weekday() >= 5
    ]

    return {
        "now_pt": datetime.now(tz=PT).strftime("%Y-%m-%d %H:%M %Z"),
        "today": today.isoformat(),
        "new_day": new_day.isoformat(),
        "new_day_is_weekend": new_day.weekday() >= 5,
        "new_day_weekday_name": new_day.strftime("%A"),
        "upcoming_weekends": upcoming_weekends,
        "my_reservations": state.get("my_reservations") or [],
        "auto_book_slots": state.get("auto_book_slots") or [],
        "watched_slots": [
            s for s in (state.get("watched_slots") or [])
            if s.get("date", "") >= today.isoformat()
        ],
        "seen_open_days": state.get("seen_open_days") or {},
        "release_probe_log": (state.get("release_probe_log") or [])[-30:],
        "last_release_probe_session": state.get("last_release_probe_session"),
    }


def _weekday_report_text(state: dict, now_pt: datetime) -> str:
    booked_today = []
    for entry in state.get("app_booking_log") or []:
        if not isinstance(entry, dict):
            continue
        try:
            booked_at = datetime.fromisoformat(str(entry.get("booked_at", "")).replace("Z", "+00:00"))
            if booked_at.astimezone(PT).date() != now_pt.date():
                continue
        except (TypeError, ValueError):
            continue
        booked_today.append(
            f"Court {entry.get('court', '?')} {entry.get('date', '?')} {entry.get('time', '?')}"
        )
    if not booked_today:
        return "Nothing booked."
    return f"Booked: {'; '.join(booked_today)}."


def _run_tool(name: str, args: dict) -> dict:
    if name == "get_context":
        return _get_context()
    if name == "send_message":
        send_telegram(args["text"])
        return {"ok": True}
    if name == "done":
        return {"done": True}
    return {"error": f"unknown tool: {name}"}


# ── System prompt ──────────────────────────────────────────────────────────────

_REPORT_SYSTEM = """\
You are a pickleball court booking agent running at 8:10 AM PT.

Your task:
1. Call get_context to read release_probe_log and my_reservations.
2. Check today's day of week (from now_pt in context).
3. If today is a weekday:
   - If nothing was booked today: send "Nothing booked." and call done.
   - If something was booked: send one line — "Booked: [court] [date] [time]." and call done.
4. If today is a weekend, send a full report (3–5 lines, plain text, no markdown):
   [Date] booking report:
   • Booked: [court] [date] [time] / Nothing booked: [reason]
   • Probes: [N] probes, slot opened at [time] / slot never opened
   • [Any useful next note]
5. Call done.

When mentioning a date, include the weekday, e.g. 2026-06-01 (Monday)."""


# ── Agent loop ─────────────────────────────────────────────────────────────────

def run_agent(phase: str) -> bool:
    """Run the report phase. Called from monitor.handler."""
    if phase != "report":
        print(f"Booking agent ({phase}): phase removed — skipping.")
        return True
    now_pt = datetime.now(tz=PT)
    if now_pt.weekday() < 5:
        report_text = _weekday_report_text(load_state(), now_pt)
        if report_text == "Nothing booked.":
            print(f"Booking agent ({phase}): weekday, nothing booked — skipping Telegram.")
            return True
        send_telegram(report_text)
        if REPORT_EMAIL:
            try:
                send_report_email(
                    f"Pickleball {now_pt.strftime('%Y-%m-%d')} 8:30 AM probe report",
                    report_text,
                )
            except Exception as exc:
                print(f"Booking agent ({phase}) report email failed: {exc}")
        print(f"Booking agent ({phase}): weekday summary sent.")
        return True

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"Booking agent ({phase}): ANTHROPIC_API_KEY not set, skipping.")
        return False

    client = anthropic.Anthropic()
    system = _REPORT_SYSTEM
    now_str = now_pt.strftime("%Y-%m-%d %H:%M %Z")

    messages: list[dict] = [
        {"role": "user", "content": f"Run the {phase} phase. Current time: {now_str}."}
    ]
    report_texts: list[str] = []
    final_text = ""

    print(f"Booking agent ({phase}) started at {now_str}.")

    for iteration in range(12):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as exc:
            print(f"Booking agent ({phase}) error: {type(exc).__name__}: {exc}")
            break
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            final_text = "".join(b.text for b in response.content if b.type == "text").strip()
            if final_text:
                print(f"Booking agent ({phase}) final: {final_text}")
            break

        tool_results = []
        done = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"  [{phase}] tool: {block.name}({json.dumps(block.input, default=str)[:120]})")
            if block.name == "send_message":
                text = str(block.input.get("text", "")).strip()
                if text:
                    report_texts.append(text)
            try:
                result = _run_tool(block.name, block.input)
                is_error = False
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
                is_error = True
                print(f"  [{phase}] tool error: {exc}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
                "is_error": is_error,
            })
            if block.name == "done":
                done = True

        messages.append({"role": "user", "content": tool_results})
        if done:
            print(f"Booking agent ({phase}) done after {iteration + 1} iteration(s).")
            break

    email_body = "\n\n".join(report_texts).strip()
    if not email_body:
        email_body = final_text
    if email_body and REPORT_EMAIL:
        try:
            send_report_email(
                f"Pickleball {datetime.now(tz=PT).strftime('%Y-%m-%d')} 8:30 AM probe report",
                email_body,
            )
        except Exception as exc:
            print(f"Booking agent ({phase}) report email failed: {exc}")

    print(f"Booking agent ({phase}) done.")
    return True
