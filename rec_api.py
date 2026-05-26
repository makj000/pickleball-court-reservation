from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.request import Request, urlopen

from config import (
    COURT_PREFERENCE, COURT_SPORT_IDS, EMAIL, FIREBASE_SIGN_IN_URL, PASSWORD,
    PARTICIPANT_USER_ID, SLOT_TIMES, _HHMM_TO_TIME_TEXT, _TIME_TEXT_TO_HHMMSS,
)
from state import _normalize_court_number, _normalize_slot_records, _utc_now_iso


def _get_cached_jwt(state: dict) -> str | None:
    jwt = state.get("cached_jwt")
    expires_at = state.get("cached_jwt_expires_at")
    if not jwt or not expires_at:
        return None
    try:
        exp = datetime.fromisoformat(expires_at)
        if datetime.now(tz=timezone.utc) < exp - timedelta(minutes=2):
            return jwt
    except (ValueError, TypeError):
        pass
    return None


def _cache_jwt(state: dict, jwt: str) -> None:
    state["cached_jwt"] = jwt
    state["cached_jwt_expires_at"] = (datetime.now(tz=timezone.utc) + timedelta(minutes=55)).isoformat()


def _firebase_login() -> str:
    """Return a fresh rec.us Bearer token via Firebase REST auth (~0.4 s, no browser)."""
    payload = json.dumps({
        "returnSecureToken": True,
        "email": EMAIL,
        "password": PASSWORD,
        "clientType": "CLIENT_TYPE_WEB",
    }).encode()
    hdrs = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.rec.us/",
        "Origin": "https://www.rec.us",
    }
    req = Request(FIREBASE_SIGN_IN_URL, data=payload, headers=hdrs, method="POST")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["idToken"]


def _rec_api(url: str, method: str = "GET", body=None, jwt: str = "") -> tuple[int, dict]:
    from urllib.error import HTTPError
    data = json.dumps(body).encode() if body else None
    hdrs: dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    if jwt:
        hdrs["Authorization"] = "Bearer " + jwt
    req = Request(url, data=data, headers=hdrs, method=method)
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as e:
        raw = e.read() or b""
        try:
            body_r: dict = json.loads(raw) if raw else {}
        except Exception:
            body_r = {"raw": raw.decode(errors="replace")[:300]}
        return e.code, body_r


def _rec_api_required(url: str, *, jwt: str) -> dict:
    status, body = _rec_api(url, jwt=jwt)
    if status != 200:
        raise RuntimeError(f"rec.us API failed [{status}]: {json.dumps(body)[:200]}")
    return body


def _rec_user_id(jwt: str) -> str:
    body = _rec_api_required("https://api.rec.us/v1/users/me", jwt=jwt)
    user = body.get("data") or body.get("user") or body
    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        raise RuntimeError("rec.us user id not found")
    return user_id


def _slot_from_rec_reservation(reservation: dict, court: str) -> dict | None:
    time_range = reservation.get("reservationTimestampRange")
    if not isinstance(time_range, list) or not time_range:
        return None
    start_text = str(time_range[0])
    parts = start_text.split()
    if len(parts) < 2:
        return None
    date_text, time_text = parts[0], parts[1]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
        return None
    slot_time = _HHMM_TO_TIME_TEXT.get(time_text[:5])
    if slot_time is None:
        return None
    return {"date": date_text, "time": slot_time, "court": court}


def _extract_my_reservations_from_rec_bookings(page: dict) -> list[dict]:
    included = page.get("included") if isinstance(page, dict) else {}
    included = included if isinstance(included, dict) else {}
    reservations = [
        r for r in included.get("reservations", [])
        if isinstance(r, dict) and not r.get("canceledAt")
    ]
    sites = {
        s.get("id"): s
        for s in included.get("sites", [])
        if isinstance(s, dict) and s.get("id")
    }
    reservation_site_ids = included.get("reservationSiteIds") or {}
    if not isinstance(reservation_site_ids, dict):
        reservation_site_ids = {}

    reservations_by_id = {r.get("id"): r for r in reservations if r.get("id")}
    reservations_by_facility: dict[str, list[dict]] = {}
    for reservation in reservations:
        facility_id = reservation.get("facilityRentalId")
        if facility_id:
            reservations_by_facility.setdefault(facility_id, []).append(reservation)

    slots = []
    bookings = page.get("data", []) if isinstance(page, dict) else []
    for booking in bookings:
        if not isinstance(booking, dict):
            continue
        if booking.get("status") != "confirmed" or booking.get("canceledAt"):
            continue
        matching_reservations = []
        linked_reservation_id = booking.get("linkedReservationId")
        if linked_reservation_id and linked_reservation_id in reservations_by_id:
            matching_reservations.append(reservations_by_id[linked_reservation_id])
        facility_id = booking.get("facilityRentalId")
        if facility_id:
            matching_reservations.extend(reservations_by_facility.get(facility_id, []))

        seen_reservations: set[str] = set()
        for reservation in matching_reservations:
            reservation_id = reservation.get("id")
            if reservation_id in seen_reservations:
                continue
            if reservation_id:
                seen_reservations.add(reservation_id)
            site_ids = reservation_site_ids.get(reservation_id, []) if reservation_id else []
            for site_id in site_ids:
                site = sites.get(site_id) or {}
                court = _normalize_court_number(site.get("courtNumber") or site.get("name"))
                if not court:
                    continue
                slot = _slot_from_rec_reservation(reservation, court)
                if slot and slot["date"] >= date.today().isoformat():
                    slots.append(slot)
    return _normalize_slot_records(slots, expand_legacy=False)


def fetch_rec_my_reservations(jwt: str | None = None) -> list[dict]:
    jwt = jwt or _firebase_login()
    user_id = _rec_user_id(jwt)
    all_slots: list[dict] = []
    page_num = 1
    page_size = 100
    while True:
        query = f"pg[num]={page_num}&pg[size]={page_size}"
        page = _rec_api_required(
            f"https://api.rec.us/v1/users/{user_id}/bookings?{query}",
            jwt=jwt,
        )
        all_slots.extend(_extract_my_reservations_from_rec_bookings(page))
        pg = ((page.get("meta") or {}).get("pg") or {}) if isinstance(page, dict) else {}
        total = int(pg.get("totalResults") or len(page.get("data", []) or []))
        size = int(pg.get("size") or page_size)
        num = int(pg.get("num") or page_num)
        if num * size >= total:
            break
        page_num += 1

    order = {time_text: idx for idx, time_text in enumerate(SLOT_TIMES)}
    court_order = {court: idx for idx, court in enumerate(COURT_PREFERENCE)}
    normalized = _normalize_slot_records(all_slots, expand_legacy=False)
    return sorted(
        normalized,
        key=lambda s: (
            s["date"],
            order.get(s["time"], len(order)),
            court_order.get(s["court"], len(court_order)),
        ),
    )


def sync_rec_my_reservations(state: dict, *, strict: bool = False) -> bool:
    try:
        slots = fetch_rec_my_reservations()
    except Exception as exc:
        print(f"rec.us reservations sync failed: {exc}")
        if strict:
            raise
        return False

    state["my_reservations"] = slots
    state["my_reservations_synced_at"] = _utc_now_iso()
    state["my_reservations_source"] = "rec.us"
    return True


def book_slot_api(jwt: str, target_date: date, time_text: str, court: str) -> bool:
    """Book a court fully via API (no browser)."""
    time_str = _TIME_TEXT_TO_HHMMSS.get(time_text)
    if not time_str:
        print(f"  book_slot_api: unknown time_text '{time_text}'")
        return False

    court_sport_id = COURT_SPORT_IDS.get(court)
    if not court_sport_id:
        print(f"  book_slot_api: unknown court '{court}'")
        return False

    date_str = target_date.isoformat()
    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    end_str = (start_dt + timedelta(hours=1)).strftime("%H:%M:%S")

    print(f"  Booking Court {court} {date_str} {time_text} via API…")
    s, order = _rec_api(
        "https://api.rec.us/v1/reservations",
        method="POST",
        body={
            "courtSportIds": [court_sport_id],
            "from": {"date": date_str, "time": time_str},
            "participantUserId": PARTICIPANT_USER_ID,
            "to": {"date": date_str, "time": end_str},
        },
        jwt=jwt,
    )
    if s not in (200, 201):
        print(f"  API booking failed [{s}]: {json.dumps(order)[:200]}")
        return False

    od = order.get("data", order)
    order_id: str = od["id"]
    total: int = od.get("total", 0)
    max_credit: int = od.get("maxCreditAdjustmentAllowed", 0)
    credit_to_apply = min(max_credit, total)
    remaining = total - credit_to_apply
    print(f"  Order {order_id[:8]} | ${total/100:.2f} | credit: ${max_credit/100:.2f} | card: ${remaining/100:.2f}")

    payments = []
    if credit_to_apply > 0:
        payments.append({"paymentMethodType": "organization-credit", "amountCents": credit_to_apply})
    if remaining > 0:
        payments.append({"paymentMethodType": "card-online", "amountCents": remaining})
    if not payments:
        payments.append({"paymentMethodType": "free", "amountCents": 0})

    s2, result = _rec_api(
        f"https://api.rec.us/v1/orders/{order_id}/pay",
        method="POST",
        body={"data": {"payments": payments}},
        jwt=jwt,
    )
    if s2 == 200:
        print(f"  Confirmed! Court {court} {date_str} {time_text}.")
        return True

    print(f"  Payment failed [{s2}]: {json.dumps(result)[:200]}")
    return False
