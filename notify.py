from __future__ import annotations

import json
from urllib.request import Request, urlopen

import boto3

from config import (
    COURT_PREFERENCE, NOTIFY_EMAIL, NOTIFY_NUMBER,
    SLOT_TIMES, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)
from message_format import with_weekday_dates
from state import _append_notification_to_history


def send_sms(to: str, message: str) -> None:
    sns = boto3.client("sns", region_name="us-west-2")
    sns.publish(PhoneNumber=to, Message=message)


def send_email(to: str, subject: str, body: str) -> None:
    ses = boto3.client("sesv2", region_name="us-west-2")
    ses.send_email(
        FromEmailAddress=to,
        Destination={"ToAddresses": [to]},
        Content={"Simple": {
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        }},
    )


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured")

    message = with_weekday_dates(message)
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        body = resp.read()
    data = json.loads(body.decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    _append_notification_to_history(TELEGRAM_CHAT_ID, message)


def notify(message: str, subject: str = "Pickleball alert") -> None:
    try:
        send_sms(NOTIFY_NUMBER, message)
        print(f"SMS sent: {message}")
    except Exception as exc:
        print(f"SMS failed: {exc}")
    try:
        send_telegram(message)
        print("Telegram sent.")
    except Exception as exc:
        print(f"Telegram failed: {exc}")
    try:
        send_email(NOTIFY_EMAIL, subject, message)
        print(f"Email sent to {NOTIFY_EMAIL}.")
    except Exception as exc:
        print(f"Email failed: {exc}")


def _ordered_open_courts(court_availability: dict[str, bool | None]) -> list[str]:
    ordered = [court for court in COURT_PREFERENCE if court_availability.get(court) is True]
    extras = sorted(
        court
        for court, is_open in court_availability.items()
        if is_open is True and court not in COURT_PREFERENCE
    )
    return ordered + extras


def _alert_lines_for_open_targets(
    state: dict,
    scanned_availability: dict[str, dict[str, dict[str, bool | None]]],
) -> list[str]:
    watched_set = {
        (slot["date"], slot["time"], slot["court"])
        for slot in state.get("watched_slots", [])
    }
    auto_book_set = {
        (slot.get("date"), slot.get("time"))
        for slot in (state.get("auto_book_slots") or [])
        if slot.get("date") and slot.get("time")
    }

    lines: list[str] = []
    for date_str in sorted(scanned_availability):
        time_map = scanned_availability[date_str]
        ordered_times = sorted(
            time_map,
            key=lambda t: SLOT_TIMES.index(t) if t in SLOT_TIMES else len(SLOT_TIMES),
        )
        for time_text in ordered_times:
            court_availability = time_map[time_text]
            open_courts = _ordered_open_courts(court_availability)
            if not open_courts:
                continue

            watched_open = [
                court for court in open_courts
                if (date_str, time_text, court) in watched_set
            ]
            auto_book_open = (date_str, time_text) in auto_book_set
            if not watched_open and not auto_book_open:
                continue

            tags = []
            if watched_open:
                tags.append("watched " + ", ".join(f"Court {court}" for court in watched_open))
            if auto_book_open:
                tags.append("auto-book")
            lines.append(
                f"{date_str} {time_text}: open {', '.join(f'Court {court}' for court in open_courts)}"
                + (f" ({'; '.join(tags)})" if tags else "")
            )
    return lines


def ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
