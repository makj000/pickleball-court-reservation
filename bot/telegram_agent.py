"""Telegram-driven Claude agent for the pickleball monitor.

Run locally:
  pip install -r bot/requirements.txt
  python -m bot.telegram_agent

Required env (in .env at repo root or shell):
  ANTHROPIC_API_KEY      Claude API key
  TELEGRAM_BOT_TOKEN     Telegram bot token (same one monitor.py uses)
  TELEGRAM_ALLOWED_IDS   Comma-separated allowed chat IDs (defaults to TELEGRAM_CHAT_ID)
  MONITOR_API_URL        e.g. https://....lambda-url.us-west-2.on.aws
  API_USERNAME           Basic-auth user for the monitor API
  API_PASSWORD           Basic-auth password
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from message_format import with_weekday_dates

load_dotenv(ROOT / ".env")

MODEL = "claude-haiku-4-5"
ANTHROPIC = Anthropic()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
ALLOWED_CHAT_IDS = {
    cid.strip()
    for cid in (os.environ.get("TELEGRAM_ALLOWED_IDS")
                or os.environ.get("TELEGRAM_CHAT_ID", "")).split(",")
    if cid.strip()
}
MONITOR_API_URL = os.environ["MONITOR_API_URL"].rstrip("/")
_BASIC_AUTH = base64.b64encode(
    f"{os.environ['API_USERNAME']}:{os.environ['API_PASSWORD']}".encode()
).decode()
MONITOR_HEADERS = {"Authorization": f"Basic {_BASIC_AUTH}"}

SYSTEM_PROMPT = """You are a helpful assistant managing a pickleball court monitor.

Capabilities (via tools):
- Read current monitor state (slot availability, watched/auto-book lists).
- Trigger an ad-hoc scan.
- Add or remove watched slots (date + time + court 4/5/6).
- Add or remove auto-book slots (date + time). The monitor will book them when an open court is detected on the next scan.
- Toggle weekend 9 AM auto-watch.

Conventions:
- Times are "8:00 AM", "9:00 AM", "10:00 AM", "11:00 AM", "4:00 PM", "5:00 PM", "6:00 PM".
- Courts: "4", "5", "6" (preference order: 6 > 4 > 5).
- Dates: YYYY-MM-DD. Resolve relative dates ("next Saturday", "this Sunday") using the user's local time (Pacific).
- When mentioning a date, include the weekday, e.g. 2026-06-01 (Monday).
- Before any write (watch/auto-book), confirm with the user in chat. Booking is irreversible once a court opens.
- Always call get_state first if you're unsure about current watched/auto-book lists.

Keep replies short. Use plain text (Telegram renders Markdown but keep it simple)."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_state",
        "description": "Return the current monitor state: a compact summary of availability per date/time/court, watched_slots, auto_book_slots, auto_watch_weekends_enabled, last_scanned.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "scan_now",
        "description": "Trigger an immediate scan. Pass `dates` (list of YYYY-MM-DD) to scan specific dates, or omit to scan everything visible.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dates": {"type": "array", "items": {"type": "string"}},
            },
            "required": [],
        },
    },
    {
        "name": "add_watched_slots",
        "description": "Add (date, time, court) triplets to the watched list (preserves existing). Court must be '4', '5', or '6'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                            "court": {"type": "string"},
                        },
                        "required": ["date", "time", "court"],
                    },
                }
            },
            "required": ["slots"],
        },
    },
    {
        "name": "remove_watched_slots",
        "description": "Remove (date, time, court) triplets from the watched list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                            "court": {"type": "string"},
                        },
                        "required": ["date", "time", "court"],
                    },
                }
            },
            "required": ["slots"],
        },
    },
    {
        "name": "add_auto_book_slots",
        "description": "Add (date, time) pairs to the auto-book list. The monitor will book those slots when an open court appears on the next scan (book once, then drop).",
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                        },
                        "required": ["date", "time"],
                    },
                }
            },
            "required": ["slots"],
        },
    },
    {
        "name": "remove_auto_book_slots",
        "description": "Remove (date, time) pairs from the auto-book list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                        },
                        "required": ["date", "time"],
                    },
                }
            },
            "required": ["slots"],
        },
    },
    {
        "name": "set_auto_watch_weekends",
        "description": "Enable or disable the weekend 9 AM auto-watch behavior.",
        "input_schema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    },
]


def _monitor_get(path: str) -> dict:
    r = requests.get(MONITOR_API_URL + path, headers=MONITOR_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _monitor_put(path: str, body: dict) -> dict:
    r = requests.put(MONITOR_API_URL + path, headers={**MONITOR_HEADERS, "Content-Type": "application/json"}, json=body, timeout=30)
    r.raise_for_status()
    return r.json() if r.text else {"ok": True}


def _monitor_post(path: str, body: dict | None = None) -> dict:
    r = requests.post(MONITOR_API_URL + path, headers={**MONITOR_HEADERS, "Content-Type": "application/json"}, json=body or {}, timeout=30)
    r.raise_for_status()
    return r.json() if r.text else {"ok": True}


def _compact_state() -> dict:
    """Strip the bulky grid down to only open/watched/auto-book/last-scan info."""
    state = _monitor_get("/state")
    open_slots: list[dict] = []
    watched: list[dict] = []
    for day in state.get("grid", []):
        for slot in day.get("slots", []):
            if slot.get("watching"):
                watched.append({"date": day["date"], "time": slot["time"], "court": slot["court"]})
            if slot.get("available") is True:
                open_slots.append({
                    "date": day["date"],
                    "time": slot["time"],
                    "court": slot["court"],
                    "watching": bool(slot.get("watching")),
                    "mine": bool(slot.get("mine")),
                })
    return {
        "today": state["grid"][0]["date"] if state.get("grid") else None,
        "last_scanned": state.get("last_scanned"),
        "scan_in_progress": bool(state.get("scan_started_at")),
        "watched_slots": watched,
        "auto_book_slots": state.get("auto_book_slots", []),
        "auto_watch_weekends_enabled": state.get("auto_watch_weekends_enabled", True),
        "open_slots": open_slots,
    }


def _diff_watch(current: list[dict], add: list[dict] | None = None, remove: list[dict] | None = None) -> list[dict]:
    keys = {(s["date"], s["time"], s["court"]) for s in current}
    for s in add or []:
        keys.add((s["date"], s["time"], s["court"]))
    for s in remove or []:
        keys.discard((s["date"], s["time"], s["court"]))
    return [{"date": d, "time": t, "court": c} for (d, t, c) in sorted(keys)]


def _diff_auto_book(current: list[dict], add: list[dict] | None = None, remove: list[dict] | None = None) -> list[dict]:
    keys = {(s["date"], s["time"]) for s in current}
    for s in add or []:
        keys.add((s["date"], s["time"]))
    for s in remove or []:
        keys.discard((s["date"], s["time"]))
    return [{"date": d, "time": t} for (d, t) in sorted(keys)]


def run_tool(name: str, args: dict) -> Any:
    if name == "get_state":
        return _compact_state()
    if name == "scan_now":
        return _monitor_post("/force-scan", {"dates": args.get("dates") or []})
    if name == "add_watched_slots":
        state = _monitor_get("/state")
        current = [
            {"date": day["date"], "time": s["time"], "court": s["court"]}
            for day in state.get("grid", [])
            for s in day.get("slots", [])
            if s.get("watching")
        ]
        new_slots = _diff_watch(current, add=args["slots"])
        return _monitor_put("/watch", {"slots": new_slots})
    if name == "remove_watched_slots":
        state = _monitor_get("/state")
        current = [
            {"date": day["date"], "time": s["time"], "court": s["court"]}
            for day in state.get("grid", [])
            for s in day.get("slots", [])
            if s.get("watching")
        ]
        new_slots = _diff_watch(current, remove=args["slots"])
        return _monitor_put("/watch", {"slots": new_slots})
    if name == "add_auto_book_slots":
        state = _monitor_get("/state")
        new_slots = _diff_auto_book(state.get("auto_book_slots", []), add=args["slots"])
        return _monitor_put("/auto-book", {"slots": new_slots})
    if name == "remove_auto_book_slots":
        state = _monitor_get("/state")
        new_slots = _diff_auto_book(state.get("auto_book_slots", []), remove=args["slots"])
        return _monitor_put("/auto-book", {"slots": new_slots})
    if name == "set_auto_watch_weekends":
        return _monitor_put("/auto-watch-weekends", {"enabled": bool(args["enabled"])})
    return {"error": f"unknown tool {name}"}


# ─── Telegram I/O ────────────────────────────────────────────────────────────

_TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def tg_send(chat_id: str, text: str) -> None:
    text = with_weekday_dates(text)
    requests.post(f"{_TG_BASE}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=30)


def tg_get_updates(offset: int | None) -> list[dict]:
    r = requests.get(f"{_TG_BASE}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data.get("result", []) if data.get("ok") else []


# ─── Agent loop ──────────────────────────────────────────────────────────────

# Per-chat conversation history (last N user/assistant turns).
HISTORY: dict[str, list[dict]] = {}
MAX_HISTORY_TURNS = 12


def reply(chat_id: str, user_text: str) -> str:
    history = HISTORY.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_pt = datetime.now(ZoneInfo("America/Los_Angeles"))
    date_hint = f"Current Pacific time: {now_pt.strftime('%Y-%m-%d %A %H:%M %Z')}."

    # Prompt caching: mark the last tool def for a cache breakpoint, so all
    # tools + system prompt get cached and re-used across turns.
    cached_tools = [
        {**t, "cache_control": {"type": "ephemeral"}} if i == len(TOOLS) - 1 else t
        for i, t in enumerate(TOOLS)
    ]
    system_blocks = [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": date_hint},
    ]

    while True:
        response = ANTHROPIC.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_blocks,
            tools=cached_tools,
            messages=history,
        )

        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            history.append({"role": "assistant", "content": response.content})
            # Trim history (always keep last MAX_HISTORY_TURNS exchanges).
            if len(history) > MAX_HISTORY_TURNS * 2:
                del history[: len(history) - MAX_HISTORY_TURNS * 2]
            return text or "(no reply)"

        history.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                result = run_tool(block.name, block.input)
                content = json.dumps(result, default=str)
                is_error = False
            except Exception as exc:
                content = f"{type(exc).__name__}: {exc}"
                is_error = True
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
                "is_error": is_error,
            })
        history.append({"role": "user", "content": tool_results})


def main() -> None:
    if not ALLOWED_CHAT_IDS:
        print("Warning: no TELEGRAM_ALLOWED_IDS / TELEGRAM_CHAT_ID set — all messages will be ignored.", file=sys.stderr)

    print(f"Agent started. Model={MODEL}. Allowed chat IDs={sorted(ALLOWED_CHAT_IDS) or '(none)'}.")
    offset: int | None = None
    while True:
        try:
            updates = tg_get_updates(offset)
        except Exception as exc:
            print(f"getUpdates failed: {exc}", file=sys.stderr)
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat_id = str(msg["chat"]["id"])
            if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
                print(f"Ignoring message from chat {chat_id}")
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            if text in ("/reset", "/clear"):
                HISTORY.pop(chat_id, None)
                tg_send(chat_id, "Conversation reset.")
                continue
            print(f"[{chat_id}] > {text}")
            try:
                answer = reply(chat_id, text)
            except Exception as exc:
                answer = f"Agent error: {type(exc).__name__}: {exc}"
            print(f"[{chat_id}] < {answer}")
            tg_send(chat_id, answer)


if __name__ == "__main__":
    main()
