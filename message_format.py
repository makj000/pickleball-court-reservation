from __future__ import annotations

import re
from datetime import date

_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?![\dT])")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def with_weekday_dates(message: str) -> str:
    def replace(match: re.Match) -> str:
        date_str = match.group(1)
        try:
            weekday = _WEEKDAYS[date.fromisoformat(date_str).weekday()]
        except ValueError:
            return date_str
        if _date_already_has_weekday(message, match.start(), match.end(), weekday):
            return date_str
        return f"{date_str} ({weekday})"

    return _ISO_DATE_RE.sub(replace, message)


def _date_already_has_weekday(message: str, start: int, end: int, weekday: str) -> bool:
    after = message[end:]
    if after.startswith((f" ({weekday})", f" {weekday}", f", {weekday}")):
        return True

    before = message[:start].rstrip()
    return before.endswith(weekday)
