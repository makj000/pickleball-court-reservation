"""Booking agent: two phases.

prep   (daily, EventBridge rule): for the weekend newly entering the 3-week
       preview window (21-22 days out), set default weekend-target checkbox
       state if it hasn't been touched yet, and warn if tomorrow's live
       release target (15 days out) has nothing checked to book.
report (8:30am PT, EventBridge rule): read probe log + reservations, send
       results for today's 8am release burst.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import date, datetime, timedelta

import anthropic
import holidays

from config import PT, REPORT_EMAIL, SLOT_TIMES, WEEKEND_TARGET_TIMES
from notify import send_report_email, send_telegram
from state import (
    _is_weekend_target_prefilled, _set_weekend_target_defaults,
    _weekend_target_checked_times, _weekend_target_watched_times,
    load_state, save_state,
)

MODEL = "claude-haiku-4-5"
RECENT_HISTORY_LIMIT = 20


def _next_date_for_weekday(today: date, weekday: int) -> str:
    days = (weekday - today.weekday()) % 7
    if days == 0:
        days = 7
    return (today + timedelta(days=days)).isoformat()


def _history_summary(history: list[dict], today: date) -> dict:
    weekday_time_counts: Counter[tuple[int, str]] = Counter()
    recent_counts: Counter[tuple[int, str]] = Counter()
    cutoff = today - timedelta(days=60)

    for entry in history:
        try:
            slot_day = date.fromisoformat(str(entry.get("date")))
        except ValueError:
            continue
        time_text = entry.get("time")
        if time_text not in SLOT_TIMES:
            continue
        key = (slot_day.weekday(), time_text)
        weekday_time_counts[key] += 1
        if slot_day >= cutoff:
            recent_counts[key] += 1

    rows = []
    for (weekday, time_text), count in weekday_time_counts.most_common(20):
        target_date = _next_date_for_weekday(today, weekday)
        rows.append({
            "weekday": date(2026, 1, 5 + weekday).strftime("%A"),
            "weekday_index": weekday,
            "next_date": target_date,
            "time": time_text,
            "count": count,
            "recent_count": recent_counts[(weekday, time_text)],
        })

    return {
        "by_weekday_time": rows,
        "recent_examples": sorted(
            history,
            key=lambda e: (e.get("date", ""), e.get("time", "")),
            reverse=True,
        )[:RECENT_HISTORY_LIMIT],
    }

# ── Shared tools ────────────────────────────────────────────────────────────────

_SHARED_TOOLS: list[dict] = [
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


# ── report phase ──────────────────────────────────────────────────────────────

REPORT_TOOLS: list[dict] = [
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
    *_SHARED_TOOLS,
]


def _get_report_context() -> dict:
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


def _run_report_tool(name: str, args: dict) -> dict:
    if name == "get_context":
        return _get_report_context()
    if name == "send_message":
        send_telegram(args["text"])
        return {"ok": True}
    if name == "done":
        return {"done": True}
    return {"error": f"unknown tool: {name}"}


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


# ── prep phase ────────────────────────────────────────────────────────────────

PREP_TOOLS: list[dict] = [
    {
        "name": "get_context",
        "description": (
            "Return: weekend dates 21 and 22 days out that may need default weekend-target "
            "checkboxes (with whether each is already prefilled and whether it's adjacent to "
            "a US federal holiday), the weekend 15 days out (tomorrow's live release target, "
            "if tomorrow is a release day) with its currently checked times, and a summary of "
            "past booking history by weekday+time."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_weekend_targets",
        "description": (
            "Set default checklist state for a weekend date that has not been prefilled yet "
            "(check already_prefilled in get_context first — this is a no-op if it's already "
            "true, to never overwrite the user's own edits). auto_book_times go to $ auto-book, "
            "watch_times go to \U0001f441️ watch-only. Times must be from "
            "WEEKEND_TARGET_TIMES (8:00 AM, 9:00 AM, 10:00 AM)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD, must be a Saturday or Sunday"},
                "auto_book_times": {"type": "array", "items": {"type": "string"}},
                "watch_times": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["date", "auto_book_times", "watch_times"],
        },
    },
    *_SHARED_TOOLS,
]


def _saturday_of(d: date) -> date:
    """The Saturday of the Sat/Sun weekend containing d (d must be Sat or Sun)."""
    return d - timedelta(days=(d.weekday() - 5) % 7)


def _weekend_is_holiday_adjacent(saturday: date) -> bool:
    us_holidays = holidays.US()
    sunday = saturday + timedelta(days=1)
    friday = saturday - timedelta(days=1)
    monday = sunday + timedelta(days=1)
    return any(d in us_holidays for d in (friday, saturday, sunday, monday))


def _get_prep_context() -> dict:
    state = load_state()
    today = date.today()

    candidates = []
    for offset in (21, 22):
        d = today + timedelta(days=offset)
        if d.weekday() < 5:
            continue
        d_str = d.isoformat()
        candidates.append({
            "date": d_str,
            "weekday_name": d.strftime("%A"),
            "already_prefilled": _is_weekend_target_prefilled(state, d_str),
            "is_holiday_adjacent": _weekend_is_holiday_adjacent(_saturday_of(d)),
            "currently_checked_auto_book_times": list(_weekend_target_checked_times(state, d_str)),
            "currently_watched_times": list(_weekend_target_watched_times(state, d_str)),
        })

    imminent_target = None
    imminent_date = today + timedelta(days=15)
    if imminent_date.weekday() >= 5:
        imminent_date_str = imminent_date.isoformat()
        checked = _weekend_target_checked_times(state, imminent_date_str)
        imminent_target = {
            "date": imminent_date_str,
            "weekday_name": imminent_date.strftime("%A"),
            "checked_auto_book_times": list(checked),
            "nothing_queued": len(checked) == 0,
        }

    return {
        "now_pt": datetime.now(tz=PT).strftime("%Y-%m-%d %H:%M %Z"),
        "today": today.isoformat(),
        "weekend_target_times": list(WEEKEND_TARGET_TIMES),
        "candidates": candidates,
        "imminent_target": imminent_target,
        "history_summary": _history_summary(state.get("booking_history") or [], today),
    }


def _set_weekend_targets_tool(args: dict) -> dict:
    date_str = str(args.get("date") or "")
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return {"error": f"invalid date: {date_str}"}
    if d.weekday() < 5:
        return {"error": f"{date_str} is not a Saturday or Sunday"}

    state = load_state()
    if _is_weekend_target_prefilled(state, date_str):
        return {"ok": False, "skipped": "already prefilled, not overwriting"}

    auto_times = [t for t in (args.get("auto_book_times") or []) if t in WEEKEND_TARGET_TIMES]
    watch_times = [t for t in (args.get("watch_times") or []) if t in WEEKEND_TARGET_TIMES]
    _set_weekend_target_defaults(state, date_str, auto_book_times=auto_times, watch_times=watch_times)
    save_state(state)
    return {"ok": True, "date": date_str, "auto_book_times": auto_times, "watch_times": watch_times}


def _run_prep_tool(name: str, args: dict) -> dict:
    if name == "get_context":
        return _get_prep_context()
    if name == "set_weekend_targets":
        return _set_weekend_targets_tool(args)
    if name == "send_message":
        send_telegram(args["text"])
        return {"ok": True}
    if name == "done":
        return {"done": True}
    return {"error": f"unknown tool: {name}"}


_PREP_SYSTEM = """\
You are a pickleball court weekend-target planning agent running once a day.

Your job: for each weekend (Saturday/Sunday) that has newly entered the 3-week
preview window and has not been prefilled yet, set default checkbox state for
8:00 AM, 9:00 AM, and 10:00 AM — then tell the user what you set via Telegram.

Process:
1. Call get_context.
2. For each entry in `candidates` where already_prefilled is false:
   - Default rule: if is_holiday_adjacent is true, put ALL THREE times into
     watch_times (not auto_book_times) — the user may be away for the
     holiday, and can manually switch a time to auto-book if they do want
     to play. Otherwise, start from all three times in auto_book_times.
   - Then cross-check `history_summary` for this weekend's weekday (Saturday
     or Sunday). For each of the three times, look at its row in
     by_weekday_time (matched by weekday + time) and its presence in
     recent_examples:
     - If there is a clear, consistent pattern of NOT playing that specific
       time (near-zero recent_count while the other two times for the same
       weekday are consistently booked), move just that time from
       auto_book_times to watch_times instead, and say why in your message.
     - A time with no history at all (e.g. a newly added time slot) is NOT
       the same as a negative signal — leave it at the default (book unless
       holiday). Only move a time to watch_times when the history actively
       argues against it, never for lack of data.
   - Call set_weekend_targets with the resulting decision. Never call it for
     a date where already_prefilled is true — that would overwrite the
     user's own edits.
3. Check `imminent_target` (the weekend 15 days out — tomorrow's release
   target, if tomorrow is a release day). If it exists and nothing_queued is
   true, you MUST clearly warn about it — this is the last chance to notice
   before it's too late to add anything back for that weekend's 8am release.
4. Send exactly ONE Telegram message (send_message) summarizing:
   - What you just set for each newly-visible weekend, or say there was
     nothing new to preview if every candidate was already prefilled.
   - The imminent-target warning from step 3, if applicable. If nothing is
     queued, say so plainly so the user can confirm that's intentional.
   Keep it short, plain text, a few lines. Mention the day of week with each
   date, e.g. 2026-06-13 (Saturday).
5. Call done.
"""


# ── Agent loop ─────────────────────────────────────────────────────────────────

def run_agent(phase: str) -> bool:
    """Run a booking-agent phase. Called from monitor.handler."""
    if phase not in ("prep", "report"):
        print(f"Booking agent ({phase}): unknown phase — skipping.")
        return True

    now_pt = datetime.now(tz=PT)

    if phase == "report" and now_pt.weekday() < 5:
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

    if phase == "prep":
        context = _get_prep_context()
        needs_action = (
            any(not c["already_prefilled"] for c in context["candidates"])
            or context["imminent_target"] is not None
        )
        if not needs_action:
            print(f"Booking agent (prep): nothing to prefill or warn about ({context['today']}) — skipping model call.")
            return True

    system = _PREP_SYSTEM if phase == "prep" else _REPORT_SYSTEM
    tools = PREP_TOOLS if phase == "prep" else REPORT_TOOLS
    run_tool = _run_prep_tool if phase == "prep" else _run_report_tool

    client = anthropic.Anthropic()
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
                tools=tools,
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
                result = run_tool(block.name, block.input)
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

    print(f"Booking agent ({phase}) done.")
    return True
