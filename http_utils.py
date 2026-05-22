from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin

from config import (
    CORS_HEADERS, COURT_PREFERENCE, SYNC_SCAN_URL, SYNC_SIGNING_SECRET,
    SYNC_TOKEN_TTL_SECONDS, build_next_dates, load_check_dates,
)
from state import _empty_court_availability


def parse_query_params(event) -> dict[str, str]:
    return (event or {}).get("queryStringParameters") or {}


def get_header(event, name: str) -> str:
    headers = (event or {}).get("headers") or {}
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return ""


def get_path(event) -> str:
    rc = (event or {}).get("requestContext") or {}
    path = rc.get("http", {}).get("path") or event.get("path") or "/"
    stage = rc.get("stage", "")
    if stage and stage != "$default":
        prefix = f"/{stage}"
        if path.startswith(prefix + "/"):
            path = path[len(prefix):]
        elif path == prefix:
            path = "/"
    return path.rstrip("/") or "/"


def get_method(event) -> str:
    rc = (event or {}).get("requestContext") or {}
    return (
        rc.get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()


def get_body(event) -> dict:
    raw = (event or {}).get("body") or ""
    if event.get("isBase64Encoded") and raw:
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def parse_requested_dates(event) -> list[date]:
    params = parse_query_params(event)
    dates_param = (params.get("dates") or "").strip()
    if dates_param:
        return sorted(date.fromisoformat(r.strip()) for r in dates_param.split(",") if r.strip())
    start_raw = (params.get("start_date") or "").strip()
    start_date = date.fromisoformat(start_raw) if start_raw else date.today()
    days_raw = (params.get("days") or "").strip()
    if days_raw:
        days = max(1, min(int(days_raw), 7))
        return build_next_dates(days=days, start=start_date)
    configured = load_check_dates()
    if configured:
        return configured
    return build_next_dates(days=3, start=start_date)


def parse_time_filter(event, *, default: str | None = None) -> str | None:
    params = parse_query_params(event)
    raw = (params.get("time") or "").strip()
    return raw if raw else default


def summarize_results(results: dict[str, list[dict]]) -> dict[str, int]:
    return {
        "days_with_availability": len(results),
        "slots_found": sum(len(slots) for slots in results.values()),
    }


def build_scan_payload(*, targets, target_time, results):
    days = []
    for target in targets:
        key = target.strftime("%A, %B %-d")
        days.append({"date": target.isoformat(), "label": key, "slots": results.get(key, [])})
    summary = summarize_results(results)
    summary["days_scanned"] = len(targets)
    return {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "filters": {"dates": [t.isoformat() for t in targets], "time": target_time},
        "court_preference": COURT_PREFERENCE,
        "summary": summary,
        "days": days,
    }


def _mark_booked_slots_in_scan_results(
    *,
    targets: list[date],
    results: dict[str, list[dict]],
    booked_slots: list[dict],
) -> None:
    booked_keys = {(b["date"], b["time"]) for b in booked_slots}
    if not booked_keys:
        return
    for target in targets:
        date_str = target.isoformat()
        label = target.strftime("%A, %B %-d")
        for slot in results.get(label, []):
            if (date_str, slot.get("time")) not in booked_keys:
                continue
            slot["court_avail"] = _empty_court_availability(False)
            slot["courts"] = []
            slot["preferred_court"] = None


def _is_lambda_url_request(event) -> bool:
    domain = ((event or {}).get("requestContext") or {}).get("domainName", "")
    return ".lambda-url." in domain


def _sync_token_payload(path: str, params: dict[str, str], expires: str) -> str:
    filtered = {
        key: value
        for key, value in params.items()
        if key not in {"sync_token", "sync_expires"}
    }
    query = urlencode(sorted(filtered.items()))
    return f"{path}\n{expires}\n{query}"


def _sign_sync_token(path: str, params: dict[str, str], expires: str) -> str:
    if not SYNC_SIGNING_SECRET:
        raise RuntimeError("SYNC_SIGNING_SECRET or API_PASSWORD must be configured")
    payload = _sync_token_payload(path, params, expires)
    return hmac.new(
        SYNC_SIGNING_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _has_valid_sync_token(event) -> bool:
    params = parse_query_params(event)
    token = (params.get("sync_token") or "").strip()
    expires = (params.get("sync_expires") or "").strip()
    if not token or not expires:
        return False
    try:
        expires_at = int(expires)
    except ValueError:
        return False
    now = int(datetime.now(tz=timezone.utc).timestamp())
    if expires_at < now:
        return False
    expected = _sign_sync_token(get_path(event), params, expires)
    return hmac.compare_digest(token, expected)


def _build_sync_redirect(event) -> dict:
    if not SYNC_SCAN_URL:
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "sync_scan_url is not configured"}),
        }
    if not SYNC_SIGNING_SECRET:
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "sync signing secret is not configured"}),
        }

    params = parse_query_params(event).copy()
    target_path = "/scan"
    expires = str(int(datetime.now(tz=timezone.utc).timestamp()) + SYNC_TOKEN_TTL_SECONDS)
    params["sync_expires"] = expires
    params["sync_token"] = _sign_sync_token(target_path, params, expires)
    target_url = urljoin(SYNC_SCAN_URL, target_path.lstrip("/"))
    location = f"{target_url}?{urlencode(sorted(params.items()))}"
    return {
        "statusCode": 307,
        "headers": {**CORS_HEADERS, "Location": location, "Cache-Control": "no-store"},
        "body": "",
    }


def _check_auth(event) -> bool:
    if _has_valid_sync_token(event):
        return True
    expected_user = os.environ.get("API_USERNAME", "")
    expected_pass = os.environ.get("API_PASSWORD", "")
    if not expected_user or not expected_pass:
        return False
    auth_header = get_header(event, "authorization")
    if not auth_header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username == expected_user and password == expected_pass
    except Exception:
        return False
