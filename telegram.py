import json
import os
from datetime import date, datetime
from urllib.request import Request, urlopen

from config import CORS_HEADERS, COURT_PREFERENCE, PT, SLOT_TIMES, TELEGRAM_BOT_TOKEN
from state import (
    _has_future_watched_slots, _load_chat_history, _load_telegram_usage,
    _normalize_slot_records, _record_telegram_usage, _save_chat_history, _utc_now_iso,
    load_state, save_state,
)
from http_utils import get_body, get_header
from message_format import with_weekday_dates
from rec_api import _firebase_login
from scanner import _api_fetch_availability, _api_scan
from booking import _apply_booked_slots, _notify_booked_slots
from scheduler import _run_full_refresh_worker
from state import _auto_watch_upcoming_weekends

ANTHROPIC_API_KEY       = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", os.environ.get("API_PASSWORD", ""))
BOT_MODEL               = "claude-haiku-4-5-20251001"

BOT_SYSTEM_PROMPT = """You are a helpful assistant managing a pickleball court monitor.

Capabilities (via tools):
- Read current monitor state (slot availability, watched/auto-book lists).
- Trigger an ad-hoc scan.
- Add or remove watched slots (date + time + court 4/5/6).
- Add or remove auto-book slots (date + time). The monitor will book them when an open court appears on the next scan.
- Toggle weekend 9 AM auto-watch.
- Book a slot immediately (book_slot) — probes live availability then books the best open court.

Conventions:
- Times are "8:00 AM", "9:00 AM", "10:00 AM", "11:00 AM", "4:00 PM", "5:00 PM", "6:00 PM".
- Courts: "4", "5", "6" (preference order: 6 > 4 > 5).
- Dates: YYYY-MM-DD. Resolve relative dates ("next Saturday", "this Sunday") using today's Pacific date.
- When mentioning a date, include the weekday, e.g. 2026-06-01 (Monday).
- Before any write (watch/auto-book), confirm with the user. Auto-booking is irreversible once a court opens.
- Call get_state first if you're unsure about current watched/auto-book lists.

Booking on demand:
- If the user says "book it", "book this", "grab it", or similar, look at your recent
  assistant messages in this conversation to find which slot was just mentioned, then
  call book_slot immediately. Do NOT ask for confirmation — the explicit intent is sufficient.
- Report the outcome clearly: which court was booked, or why it failed.

Keep replies short. Use plain text."""

BOT_TOOLS: list[dict] = [
    {
        "name": "get_state",
        "description": "Return current monitor state: availability, watched_slots, auto_book_slots, auto_watch_weekends_enabled, last_scanned.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "scan_now",
        "description": "Trigger an immediate scan. Pass `dates` (YYYY-MM-DD list) to scan specific dates, or omit to scan all visible slots.",
        "input_schema": {
            "type": "object",
            "properties": {"dates": {"type": "array", "items": {"type": "string"}}},
            "required": [],
        },
    },
    {
        "name": "add_watched_slots",
        "description": "Add (date, time, court) triplets to the watched list. Court must be '4', '5', or '6'.",
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
        "description": "Add (date, time) pairs to the auto-book list.",
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
    {
        "name": "book_slot",
        "description": (
            "Immediately probe availability and book a specific slot (date + time). "
            "Tries courts in preference order 6 > 4 > 5. "
            "Use when the user says 'book it' or similar in reply to an availability notification."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "e.g. '9:00 AM'"},
            },
            "required": ["date", "time"],
        },
    },
]


def _bot_compact_state() -> dict:
    state = load_state()
    today_str = date.today().isoformat()
    watched = [
        {"date": s["date"], "time": s["time"], "court": s["court"]}
        for s in state.get("watched_slots", [])
        if s.get("date", "") >= today_str
    ]
    open_slots = []
    for date_str, day_avail in state.get("availability", {}).items():
        if date_str < today_str:
            continue
        for time_text, court_avail in day_avail.items():
            for court, avail in court_avail.items():
                if avail is True:
                    open_slots.append({"date": date_str, "time": time_text, "court": court})
    return {
        "today": today_str,
        "last_scanned": state.get("last_scanned"),
        "scan_in_progress": bool(state.get("scan_started_at")),
        "watched_slots": watched,
        "auto_book_slots": state.get("auto_book_slots", []),
        "auto_watch_weekends_enabled": state.get("auto_watch_weekends_enabled", True),
        "open_slots": sorted(open_slots, key=lambda x: (x["date"], x["time"], x["court"])),
    }


def _bot_run_tool(name: str, args: dict):
    from rec_api import book_slot_api
    if name == "get_state":
        return _bot_compact_state()
    if name == "scan_now":
        date_strs = args.get("dates") or []
        if date_strs:
            for d_str in date_strs:
                date.fromisoformat(d_str)
        _run_full_refresh_worker(force=True)
        return {"ok": True, "message": "Scan completed", "state": _bot_compact_state()}
    if name == "add_watched_slots":
        state = load_state()
        existing = {(s["date"], s["time"], s["court"]) for s in state.get("watched_slots", [])}
        for s in args.get("slots", []):
            existing.add((s["date"], s["time"], s["court"]))
        state["watched_slots"] = _normalize_slot_records(
            [{"date": d, "time": t, "court": c} for d, t, c in sorted(existing)],
            expand_legacy=False,
        )
        state["watched_slots_updated_at"] = _utc_now_iso() if _has_future_watched_slots(state) else None
        save_state(state)
        return {"ok": True, "watched": len(state["watched_slots"])}
    if name == "remove_watched_slots":
        state = load_state()
        rm = {(s["date"], s["time"], s["court"]) for s in args.get("slots", [])}
        state["watched_slots"] = [
            s for s in state.get("watched_slots", [])
            if (s["date"], s["time"], s["court"]) not in rm
        ]
        state["watched_slots_updated_at"] = _utc_now_iso() if _has_future_watched_slots(state) else None
        save_state(state)
        return {"ok": True, "watched": len(state["watched_slots"])}
    if name == "add_auto_book_slots":
        state = load_state()
        existing = {(s["date"], s["time"]) for s in state.get("auto_book_slots", [])}
        for s in args.get("slots", []):
            existing.add((s["date"], s["time"]))
        state["auto_book_slots"] = [{"date": d, "time": t} for d, t in sorted(existing)]
        save_state(state)
        return {"ok": True, "auto_book": len(state["auto_book_slots"])}
    if name == "remove_auto_book_slots":
        state = load_state()
        rm = {(s["date"], s["time"]) for s in args.get("slots", [])}
        state["auto_book_slots"] = [
            s for s in state.get("auto_book_slots", [])
            if (s["date"], s["time"]) not in rm
        ]
        save_state(state)
        return {"ok": True, "auto_book": len(state["auto_book_slots"])}
    if name == "set_auto_watch_weekends":
        state = load_state()
        state["auto_watch_weekends_enabled"] = bool(args.get("enabled"))
        if args.get("enabled"):
            _auto_watch_upcoming_weekends(state)
        save_state(state)
        return {"ok": True, "auto_watch_weekends_enabled": bool(args.get("enabled"))}
    if name == "book_slot":
        date_str = str(args.get("date", ""))
        time_text = str(args.get("time", ""))
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            return {"ok": False, "error": f"Invalid date: {date_str}"}
        if time_text not in SLOT_TIMES:
            return {"ok": False, "error": f"Invalid time '{time_text}'. Valid: {SLOT_TIMES}"}
        avail = _api_fetch_availability({date_str: [time_text]})
        court_avail = avail.get(date_str, {}).get(time_text, {})
        open_courts = [c for c in COURT_PREFERENCE if court_avail.get(c) is True]
        if not open_courts:
            return {"ok": False, "error": f"No open courts for {date_str} {time_text} right now"}
        jwt = _firebase_login()
        for court in open_courts:
            ok = book_slot_api(jwt, target_date, time_text, court)
            if ok:
                state = load_state()
                booked = [{"date": date_str, "time": time_text, "court": court}]
                _apply_booked_slots(state, booked)
                save_state(state)
                _notify_booked_slots(booked)
                return {"ok": True, "booked": f"Court {court} on {date_str} {time_text}"}
        return {"ok": False, "error": f"All courts failed for {date_str} {time_text}"}
    return {"error": f"unknown tool: {name}"}


def _bot_content_to_dicts(content) -> list[dict]:
    result = []
    for block in content or []:
        if isinstance(block, dict):
            result.append(block)
        elif hasattr(block, "model_dump"):
            result.append(block.model_dump())
        else:
            d: dict = {"type": getattr(block, "type", "text")}
            if hasattr(block, "text"):
                d["text"] = block.text
            if hasattr(block, "id"):
                d["id"] = block.id
            if hasattr(block, "name"):
                d["name"] = block.name
            if hasattr(block, "input"):
                d["input"] = block.input
            result.append(d)
    return result


def _tg_send_to(chat_id: str, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    text = with_weekday_dates(text)
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        resp.read()


def _bot_reply(chat_id: str, user_text: str) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    history = _load_chat_history(chat_id)
    history.append({"role": "user", "content": user_text})
    now_pt = datetime.now(tz=PT)
    date_hint = f"Current Pacific time: {now_pt.strftime('%Y-%m-%d %A %H:%M %Z')}."
    cached_tools = [
        {**t, "cache_control": {"type": "ephemeral"}} if i == len(BOT_TOOLS) - 1 else t
        for i, t in enumerate(BOT_TOOLS)
    ]
    system_blocks = [
        {"type": "text", "text": BOT_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": date_hint},
    ]
    total_in = total_out = total_cache_read = total_cache_write = 0
    all_tools: list[str] = []
    started_at = _utc_now_iso()
    while True:
        response = client.messages.create(
            model=BOT_MODEL,
            max_tokens=1024,
            system=system_blocks,
            tools=cached_tools,
            messages=history,
        )
        content_dicts = _bot_content_to_dicts(response.content)
        u = response.usage
        total_in          += u.input_tokens
        total_out         += u.output_tokens
        total_cache_read  += getattr(u, "cache_read_input_tokens", 0) or 0
        total_cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        tools_called = [b.get("name") for b in content_dicts if b.get("type") == "tool_use"]
        all_tools.extend(tools_called)
        print(
            f"Bot [{chat_id}] tokens: in={u.input_tokens} out={u.output_tokens}"
            + (f" cache_read={u.cache_read_input_tokens}" if getattr(u, "cache_read_input_tokens", None) else "")
            + (f" cache_write={u.cache_creation_input_tokens}" if getattr(u, "cache_creation_input_tokens", None) else "")
            + (f" tools={tools_called}" if tools_called else "")
        )
        if response.stop_reason != "tool_use":
            text_out = "".join(
                b.get("text", "") for b in content_dicts if b.get("type") == "text"
            ).strip()
            history.append({"role": "assistant", "content": content_dicts})
            _save_chat_history(chat_id, history)
            _record_telegram_usage({
                "at": started_at,
                "input_tokens": total_in,
                "output_tokens": total_out,
                "cache_read_tokens": total_cache_read,
                "cache_write_tokens": total_cache_write,
                "tools": all_tools,
                "reply_preview": (text_out or "")[:80],
            })
            return text_out or "(no reply)"
        history.append({"role": "assistant", "content": content_dicts})
        tool_results = []
        for block in content_dicts:
            if block.get("type") != "tool_use":
                continue
            try:
                result = _bot_run_tool(block["name"], block.get("input", {}))
                content_str = json.dumps(result, default=str)
                is_error = False
            except Exception as exc:
                content_str = f"{type(exc).__name__}: {exc}"
                is_error = True
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": content_str,
                "is_error": is_error,
            })
        history.append({"role": "user", "content": tool_results})


def handle_telegram(event) -> dict:
    secret = get_header(event, "x-telegram-bot-api-secret-token")
    if TELEGRAM_WEBHOOK_SECRET and secret != TELEGRAM_WEBHOOK_SECRET:
        return {"statusCode": 403, "headers": CORS_HEADERS, "body": json.dumps({"error": "Forbidden"})}
    body = get_body(event)
    msg = body.get("message") or body.get("edited_message")
    if not msg:
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    allowed = {
        cid.strip()
        for cid in (os.environ.get("TELEGRAM_ALLOWED_IDS") or os.environ.get("TELEGRAM_CHAT_ID", "")).split(",")
        if cid.strip()
    }
    if allowed and chat_id not in allowed:
        print(f"Telegram: ignoring message from chat {chat_id}")
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
    text = (msg.get("text") or "").strip()
    if not text:
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
    if text in ("/reset", "/clear", "/start"):
        _save_chat_history(chat_id, [])
        _tg_send_to(chat_id, "Conversation reset.")
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
    try:
        answer = _bot_reply(chat_id, text)
    except Exception as exc:
        print(f"Telegram bot error for chat {chat_id}: {exc}")
        answer = f"Error: {type(exc).__name__}: {exc}"
    _tg_send_to(chat_id, answer)
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
