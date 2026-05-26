"""Booking agent: intelligent prep and reporting for the 8am slot-release session.

Two phases, each triggered by a separate EventBridge rule:
  prep   (7:30am PT): decide what to auto-book, send a preview message
  report (8:10am PT): read probe log + reservations, send results
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import anthropic

from config import COURT_PREFERENCE, PT, SLOT_TIMES
from notify import send_telegram
from state import load_state, save_state

MODEL = "claude-sonnet-4-6"

# ── Tools ─────────────────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "get_context",
        "description": (
            "Return current state relevant to booking: today's date, the new day opening "
            "14 days out, existing reservations, current auto_book_slots, watched_slots, "
            "seen_open_days (when each date was first detected open), and the most recent "
            "release probe log entries."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_auto_book",
        "description": (
            "Replace the auto_book_slots list. Pass the full desired list of {date, time} "
            "pairs. The existing release probe session (7:58-8:02 AM) will book them the "
            "moment a court opens."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "YYYY-MM-DD"},
                            "time": {"type": "string", "description": "e.g. '9:00 AM'"},
                        },
                        "required": ["date", "time"],
                    },
                }
            },
            "required": ["slots"],
        },
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


def _set_auto_book(slots: list[dict]) -> dict:
    state = load_state()
    today_str = date.today().isoformat()
    # Keep any existing slots for dates not in the new list, then add new ones
    new_dates = {s["date"] for s in slots}
    kept = [
        s for s in (state.get("auto_book_slots") or [])
        if s.get("date", "") >= today_str and s.get("date") not in new_dates
    ]
    state["auto_book_slots"] = kept + slots
    save_state(state)
    return {"ok": True, "auto_book_slots": state["auto_book_slots"]}


def _run_tool(name: str, args: dict) -> dict:
    if name == "get_context":
        return _get_context()
    if name == "set_auto_book":
        return _set_auto_book(args["slots"])
    if name == "send_message":
        send_telegram(args["text"])
        return {"ok": True}
    if name == "done":
        return {"done": True}
    return {"error": f"unknown tool: {name}"}


# ── System prompts ─────────────────────────────────────────────────────────────

_PREP_SYSTEM = """\
You are a pickleball court booking agent running at 7:30 AM PT.

Context:
- rec.us releases new slots at exactly 8:00 AM PT, 14 days in advance.
- The existing release probe system (7:58–8:02 AM) will automatically book whatever \
is in auto_book_slots the moment a court opens. You don't need to do the booking yourself.
- Courts in preference order: 6 > 4 > 5. Target both 9:00 AM and 8:00 AM (in that priority).

Your task:
1. Call get_context.
2. Decide whether to queue a booking for the new day (14 days out):
   - Skip if it's a weekday (no weekend courts open).
   - Skip if there's already a reservation on that weekend (Sat or Sun) for both 8am and 9am.
   - Target both 9:00 AM and 8:00 AM.
3. If needed, call set_auto_book with both time slots for the target date (keep any other future slots).
4. Send a short Telegram preview (1–2 lines: what you're targeting and why, \
or why you're skipping).
5. Call done.

Keep the message tight. No markdown, plain text only."""

_REPORT_SYSTEM = """\
You are a pickleball court booking agent running at 8:10 AM PT.

Your task:
1. Call get_context to read release_probe_log and my_reservations.
2. Determine what happened during the 7:58–8:02 AM release probe session:
   - Was a slot booked? Which court, at what time?
   - If nothing was booked, why? (slot never opened, already had a booking, no target set)
   - How many probes ran? When did the slot first appear as open?
3. Send a clear Telegram report (3–5 lines, plain text, no markdown).
4. Call done.

Report format (adjust based on what actually happened):
[Date] booking report:
• Booked: [court] [date] [time] / Nothing booked: [reason]
• Probes: [N] probes, slot opened at [time] / slot never opened
• [Any useful next note]"""


# ── Agent loop ─────────────────────────────────────────────────────────────────

def run_agent(phase: str) -> None:
    """Run prep or report phase. Called from monitor.handler."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"Booking agent ({phase}): ANTHROPIC_API_KEY not set, skipping.")
        return

    client = anthropic.Anthropic()
    system = _PREP_SYSTEM if phase == "prep" else _REPORT_SYSTEM
    now_str = datetime.now(tz=PT).strftime("%Y-%m-%d %H:%M %Z")

    messages: list[dict] = [
        {"role": "user", "content": f"Run the {phase} phase. Current time: {now_str}."}
    ]

    print(f"Booking agent ({phase}) started at {now_str}.")

    for iteration in range(12):
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            if text:
                print(f"Booking agent ({phase}) final: {text}")
            break

        tool_results = []
        done = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"  [{phase}] tool: {block.name}({json.dumps(block.input, default=str)[:120]})")
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
