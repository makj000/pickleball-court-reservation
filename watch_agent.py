"""AI watch-list agent based on the user's booking history.

The model decides intent. This module validates and applies state changes.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import date, datetime, timedelta

import anthropic

from config import COURT_PREFERENCE, PT, SLOT_TIMES
from notify import send_telegram
from rec_api import sync_rec_my_reservations
from state import (
    _normalize_slot_records, _utc_now_iso, load_state,
    record_booking_history_from_reservations, save_state,
)

MODEL = "claude-haiku-4-5"
RECENT_HISTORY_LIMIT = 20
MIN_PATTERN_COUNT = 2


TOOLS: list[dict] = [
    {
        "name": "get_context",
        "description": "Return compact booking-history statistics and current watch state.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_watch",
        "description": "Add validated watch targets. This only watches; it never books.",
        "input_schema": {
            "type": "object",
            "properties": {
                "watch": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "times": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "reason": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                        "required": ["date", "times", "reason", "confidence"],
                    },
                },
                "message": {"type": "string"},
            },
            "required": ["watch", "message"],
        },
    },
    {
        "name": "done",
        "description": "Signal that the agent is finished.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _upcoming_dates(today: date) -> list[str]:
    return [(today + timedelta(days=i)).isoformat() for i in range(16)]


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


def _get_context() -> dict:
    state = load_state()
    sync_rec_my_reservations(state)
    record_booking_history_from_reservations(state)
    save_state(state)

    today = datetime.now(tz=PT).date()
    return {
        "now_pt": datetime.now(tz=PT).strftime("%Y-%m-%d %H:%M %Z"),
        "today": today.isoformat(),
        "upcoming_dates": _upcoming_dates(today),
        "available_slot_times": SLOT_TIMES,
        "court_preference": COURT_PREFERENCE,
        "history_summary": _history_summary(state.get("booking_history") or [], today),
        "current_watched_slots": [
            s for s in (state.get("watched_slots") or [])
            if s.get("date", "") >= today.isoformat()
        ],
        "already_ai_watched_dates": state.get("ai_watched_dates") or [],
    }


def _pattern_supports_watch(context: dict, target_date: str, time_text: str) -> bool:
    for row in (context.get("history_summary") or {}).get("by_weekday_time") or []:
        if row.get("next_date") == target_date and row.get("time") == time_text:
            count = int(row.get("count") or 0)
            recent_count = int(row.get("recent_count") or 0)
            return count >= MIN_PATTERN_COUNT or recent_count >= 1
    return False


def _apply_watch_decision(context: dict, watch: list[dict], message: str) -> dict:
    state = load_state()
    today = datetime.now(tz=PT).date()
    allowed_dates = set(_upcoming_dates(today))
    ai_watched_dates = set(state.get("ai_watched_dates") or [])
    existing = {
        (s["date"], s["time"], s["court"])
        for s in _normalize_slot_records(state.get("watched_slots"), expand_legacy=True)
    }

    new_slots = []
    decision_slots = []
    for item in watch or []:
        if not isinstance(item, dict):
            continue
        target_date = str(item.get("date") or "")
        if target_date not in allowed_dates or target_date in ai_watched_dates:
            continue
        try:
            confidence = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        if confidence < 0.6:
            continue
        for time_text in item.get("times") or []:
            if time_text not in SLOT_TIMES:
                continue
            if not _pattern_supports_watch(context, target_date, time_text):
                continue
            decision_slots.append({"date": target_date, "time": time_text})
            for court in COURT_PREFERENCE:
                key = (target_date, time_text, court)
                if key not in existing:
                    new_slots.append({"date": target_date, "time": time_text, "court": court})
                    existing.add(key)

    if not new_slots:
        return {"ok": True, "added": 0}

    state["watched_slots"] = _normalize_slot_records(
        state.get("watched_slots", []) + new_slots,
        expand_legacy=False,
    )
    state["watched_slots_updated_at"] = _utc_now_iso()
    state["ai_watched_dates"] = sorted(ai_watched_dates | {slot["date"] for slot in decision_slots})
    state["ai_watch_decisions"] = (state.get("ai_watch_decisions") or []) + [{
        "decided_at": _utc_now_iso(),
        "target_date": sorted({slot["date"] for slot in decision_slots})[0],
        "slots": decision_slots,
        "reason": "; ".join(
            str(item.get("reason") or "").strip()
            for item in watch
            if isinstance(item, dict) and item.get("reason")
        )[:500],
        "confidence": max(
            _safe_confidence(item.get("confidence")) for item in watch if isinstance(item, dict)
        ),
        "message": message.strip()[:1000],
    }]
    save_state(state)
    if message.strip():
        send_telegram(message.strip())
    return {"ok": True, "added": len(new_slots)}


def _safe_confidence(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _run_tool(name: str, args: dict, context_cache: dict | None) -> tuple[dict, dict | None]:
    if name == "get_context":
        context = _get_context()
        return context, context
    if name == "set_watch":
        context = context_cache or _get_context()
        return _apply_watch_decision(
            context,
            args.get("watch") or [],
            str(args.get("message") or ""),
        ), context
    if name == "done":
        return {"done": True}, context_cache
    return {"error": f"unknown tool: {name}"}, context_cache


SYSTEM_PROMPT = """\
You are a pickleball watch-list agent.

Your job is to choose slots to WATCH, not book. Never create auto-book targets.
Prefer patterns from the user's own booking history.
If the user repeatedly books Saturday morning, watch the next Saturday once.
If evidence is weak, choose nothing.

Process:
1. Call get_context.
2. Decide whether any upcoming date/time should be watched.
3. Call set_watch with JSON watch targets, or call set_watch with an empty watch list.
4. Call done.

Rules:
- Only choose dates from upcoming_dates.
- Only choose times from available_slot_times.
- Do not choose a date in already_ai_watched_dates.
- Use confidence >= 0.6 only when the history summary clearly supports the choice.
- Keep message one plain English sentence.
"""


def run_agent() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Watch agent: ANTHROPIC_API_KEY not set, skipping.")
        return False

    client = anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": "Run today's watch-list decision."}]
    context_cache: dict | None = None

    for iteration in range(8):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as exc:
            print(f"Watch agent error: {type(exc).__name__}: {exc}")
            return False
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            text = "".join(block.text for block in response.content if block.type == "text").strip()
            if text:
                print(f"Watch agent final text: {text}")
            return True

        tool_results = []
        done = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"  [watch] tool: {block.name}({json.dumps(block.input, default=str)[:120]})")
            result, context_cache = _run_tool(block.name, block.input, context_cache)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
                "is_error": bool(result.get("error")),
            })
            if block.name == "done":
                done = True
        messages.append({"role": "user", "content": tool_results})
        if done:
            print(f"Watch agent done after {iteration + 1} iteration(s).")
            return True

    print("Watch agent stopped after iteration limit.")
    return False
