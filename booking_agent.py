"""Booking agent: intelligent prep and reporting for the 8am slot-release session.

Two phases, each triggered by a separate EventBridge rule:
  prep   (7:30am PT): decide what to auto-book, send a preview message
  report (8:30am PT): read probe log + reservations, send results
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import anthropic

from config import COURT_PREFERENCE, PT, REPORT_EMAIL, SLOT_TIMES
from notify import send_report_email, send_telegram
from state import _enqueue_work, load_state, save_state

MODEL = "claude-sonnet-4-6"
PREP_RETRY_DELAY_SECONDS = 15 * 60

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
    new_day = date.today() + timedelta(days=14)
    # Keep any existing slots for dates not in the new list, then add new ones
    new_dates = {s["date"] for s in slots}
    if (
        new_day.weekday() >= 5
        and any(s.get("date") == new_day.isoformat() and s.get("time") == "8:00 AM" for s in slots)
        and not any(s.get("date") == new_day.isoformat() and s.get("time") == "9:00 AM" for s in slots)
    ):
        slots = list(slots) + [{"date": new_day.isoformat(), "time": "9:00 AM"}]
        new_dates = {s["date"] for s in slots}
    kept = [
        s for s in (state.get("auto_book_slots") or [])
        if s.get("date", "") >= today_str and s.get("date") not in new_dates
    ]

    def _slot_sort_key(slot: dict) -> tuple[str, int]:
        time_text = slot.get("time", "")
        if time_text == "9:00 AM":
            time_rank = 0
        elif time_text == "8:00 AM":
            time_rank = 1
        else:
            time_rank = 2 + (SLOT_TIMES.index(time_text) if time_text in SLOT_TIMES else len(SLOT_TIMES))
        return (slot.get("date", ""), time_rank)

    state["auto_book_slots"] = sorted(kept + slots, key=_slot_sort_key)
    save_state(state)
    return {"ok": True, "auto_book_slots": state["auto_book_slots"]}


def _prep_target_date() -> date:
    return date.today() + timedelta(days=14)


def _prep_is_complete(state: dict) -> tuple[bool, str]:
    target_date = _prep_target_date()
    target_str = target_date.isoformat()
    if target_date.weekday() < 5:
        return True, "weekday skip"

    reservations = {
        (r.get("date"), r.get("time"))
        for r in (state.get("my_reservations") or [])
        if isinstance(r, dict)
    }
    if {
        (target_str, "8:00 AM"),
        (target_str, "9:00 AM"),
    }.issubset(reservations):
        return True, "already reserved"

    auto_book_slots = {
        (s.get("date"), s.get("time"))
        for s in (state.get("auto_book_slots") or [])
        if isinstance(s, dict)
    }
    if (target_str, "9:00 AM") in auto_book_slots:
        return True, "prep complete"

    return False, f"{target_str} 9:00 AM is not queued for auto-book"


def _schedule_prep_retry(*, attempt: int, reason: str) -> str | None:
    state = load_state()
    scheduled_for = datetime.now(tz=PT) + timedelta(seconds=PREP_RETRY_DELAY_SECONDS)
    state["booking_agent_prep_retry_attempt"] = attempt
    state["booking_agent_prep_retry_scheduled_at"] = scheduled_for.isoformat(timespec="seconds")
    state["booking_agent_prep_last_error"] = reason
    save_state(state)
    queued = _enqueue_work(
        "booking_agent_prep_retry",
        {"attempt": attempt},
        delay_seconds=PREP_RETRY_DELAY_SECONDS,
    )
    return scheduled_for.strftime("%Y-%m-%d %I:%M %p PT") if queued else None


def _send_prep_failure(reason: str, retry_at: str | None) -> None:
    lines = [f"❌ Prep agent failed: {reason}"]
    if retry_at:
        lines.append(f"Retry scheduled: {retry_at}")
    else:
        lines.append("Retry not scheduled.")
    send_telegram("\n".join(lines))


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
- Courts in preference order: 6 > 4 > 5. Target 9:00 AM before 8:00 AM.

Your task:
1. Call get_context.
2. Decide whether to queue a booking for the new day (14 days out):
   - Skip if it's a weekday (no weekend courts open).
   - Skip if there's already a reservation on that weekend (Sat or Sun) for both 8am and 9am.
   - Target 9:00 AM first, then 8:00 AM only if you are keeping a backup.
3. If needed, call set_auto_book with the full desired list for the target date (keep any other future slots).
4. Send a short Telegram preview (1–2 lines: what you're targeting and why, \
or why you're skipping).
5. Call done.

Keep the message tight. No markdown, plain text only.
When mentioning a date, include the weekday, e.g. 2026-06-01 (Monday)."""

_REPORT_SYSTEM = """\
You are a pickleball court booking agent running at 8:30 AM PT.

Your task:
1. Call get_context to read release_probe_log and my_reservations.
2. Determine what happened during the 7:58–8:02 AM release probe session:
   - Was a slot booked? Which court, at what time?
   - If nothing was booked, why? (slot never opened, already had a booking, no target set)
   - How many probes ran? When did the slot first appear as open?
   - Which booking attempts were tried, retried, or failed?
3. Send a clear Telegram report (3–5 lines, plain text, no markdown).
4. Call done.

Report format (adjust based on what actually happened):
[Date] booking report:
• Booked: [court] [date] [time] / Nothing booked: [reason]
• Probes: [N] probes, slot opened at [time] / slot never opened
• [Any useful next note]

When mentioning a date, include the weekday, e.g. 2026-06-01 (Monday)."""


# ── Agent loop ─────────────────────────────────────────────────────────────────

def run_agent(phase: str, *, retry_attempt: int = 1) -> bool:
    """Run prep or report phase. Called from monitor.handler."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"Booking agent ({phase}): ANTHROPIC_API_KEY not set, skipping.")
        if phase == "prep":
            retry_at = _schedule_prep_retry(
                attempt=retry_attempt + 1,
                reason="ANTHROPIC_API_KEY not set",
            )
            _send_prep_failure("ANTHROPIC_API_KEY not set", retry_at)
        return False

    client = anthropic.Anthropic()
    system = _PREP_SYSTEM if phase == "prep" else _REPORT_SYSTEM
    now_str = datetime.now(tz=PT).strftime("%Y-%m-%d %H:%M %Z")

    messages: list[dict] = [
        {"role": "user", "content": f"Run the {phase} phase. Current time: {now_str}."}
    ]
    report_texts: list[str] = []
    final_text = ""

    print(f"Booking agent ({phase}) started at {now_str}.")

    had_exception = False
    error_reason = ""
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
            had_exception = True
            error_reason = f"{type(exc).__name__}: {exc}"
            print(f"Booking agent ({phase}) error: {error_reason}")
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
            if phase == "report" and block.name == "send_message":
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

    if phase == "report":
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

    if phase == "prep":
        state = load_state()
        ready, reason = _prep_is_complete(state)
        if ready and not had_exception:
            state["booking_agent_prep_retry_attempt"] = None
            state["booking_agent_prep_retry_scheduled_at"] = None
            state["booking_agent_prep_last_error"] = None
            save_state(state)
            print(f"Booking agent ({phase}) prep validated: {reason}.")
            print(f"Booking agent ({phase}) done.")
            return True

        failure_reason = error_reason or reason
        retry_at = _schedule_prep_retry(
            attempt=retry_attempt + 1,
            reason=failure_reason,
        )
        _send_prep_failure(failure_reason, retry_at)

    print(f"Booking agent ({phase}) done.")
    return phase != "prep" or (not had_exception and _prep_is_complete(load_state())[0])
