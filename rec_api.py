from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.request import Request, urlopen

from config import (
    COURT_PREFERENCE, COURT_SPORT_IDS, EMAIL, EMAIL2, FIREBASE_SIGN_IN_URL, PASSWORD,
    PASSWORD2, PARTICIPANT_USER_ID, PARTICIPANT_USER_ID2, SLOT_TIMES, STRIPE_PUBLISHABLE_KEY,
    _HHMM_TO_TIME_TEXT, _TIME_TEXT_TO_HHMMSS,
)
from state import _normalize_court_number, _normalize_slot_records, _utc_now_iso


def _configured_rec_accounts() -> list[dict[str, str | int]]:
    accounts: list[dict[str, str | int]] = []
    if EMAIL and PASSWORD:
        accounts.append({
            "index": 1,
            "email": EMAIL,
            "password": PASSWORD,
            "participant_user_id": PARTICIPANT_USER_ID,
        })
    if EMAIL2 and PASSWORD2:
        accounts.append({
            "index": 2,
            "email": EMAIL2,
            "password": PASSWORD2,
            "participant_user_id": PARTICIPANT_USER_ID2,
        })
    return accounts


def _jwt_cache_keys(account_index: int = 1) -> tuple[str, str]:
    if account_index == 1:
        return "cached_jwt", "cached_jwt_expires_at"
    return f"cached_jwt_{account_index}", f"cached_jwt_{account_index}_expires_at"


def _get_cached_jwt(state: dict, account_index: int = 1) -> str | None:
    jwt_key, expires_key = _jwt_cache_keys(account_index)
    jwt = state.get(jwt_key)
    expires_at = state.get(expires_key)
    if not jwt or not expires_at:
        return None
    try:
        exp = datetime.fromisoformat(expires_at)
        if datetime.now(tz=timezone.utc) < exp - timedelta(minutes=2):
            return jwt
    except (ValueError, TypeError):
        pass
    return None


def _cache_jwt(state: dict, jwt: str, account_index: int = 1) -> None:
    jwt_key, expires_key = _jwt_cache_keys(account_index)
    state[jwt_key] = jwt
    state[expires_key] = (datetime.now(tz=timezone.utc) + timedelta(minutes=55)).isoformat()


def _firebase_login(account: dict[str, str | int] | None = None) -> str:
    """Return a fresh rec.us Bearer token via Firebase REST auth (~0.4 s, no browser)."""
    if account is None:
        account = {
            "email": EMAIL,
            "password": PASSWORD,
        }
    payload = json.dumps({
        "returnSecureToken": True,
        "email": account.get("email", ""),
        "password": account.get("password", ""),
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


def _rec_booking_sessions(state: dict) -> list[dict[str, str | int]]:
    sessions: list[dict[str, str | int]] = []
    for account in _configured_rec_accounts():
        account_index = int(account["index"])
        jwt = _get_cached_jwt(state, account_index)
        if not jwt:
            jwt = _firebase_login(account)
            _cache_jwt(state, jwt, account_index)
        participant_user_id = str(account.get("participant_user_id") or "")
        if not participant_user_id:
            participant_user_id = _rec_user_id(jwt)
        sessions.append({
            "account_index": account_index,
            "jwt": jwt,
            "participant_user_id": participant_user_id,
        })
    return sessions


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


def _stripe_confirm_payment_intent(pi_id: str, client_secret: str, pm_id: str) -> tuple[int, dict]:
    """Confirm a Stripe PaymentIntent using rec.us's publishable key (mirrors Stripe.js flow)."""
    from urllib.error import HTTPError
    from urllib.parse import urlencode
    body = urlencode({
        "client_secret": client_secret,
        "payment_method": pm_id,
        "return_url": "https://www.rec.us/foster-city",
    }).encode()
    req = Request(
        f"https://api.stripe.com/v1/payment_intents/{pi_id}/confirm",
        data=body,
        headers={
            "Authorization": f"Bearer {STRIPE_PUBLISHABLE_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as e:
        raw = e.read() or b""
        try:
            body_r: dict = json.loads(raw) if raw else {}
        except Exception:
            body_r = {"raw": raw.decode(errors="replace")[:500]}
        return e.code, body_r


def book_slot_api(
    jwt: str,
    target_date: date,
    time_text: str,
    court: str,
    transaction_log: dict | None = None,
    participant_user_id: str | None = None,
) -> bool:
    """Book a court fully via API (no browser)."""
    if transaction_log is None:
        transaction_log = {}

    time_str = _TIME_TEXT_TO_HHMMSS.get(time_text)
    if not time_str:
        print(f"  book_slot_api: unknown time_text '{time_text}'")
        transaction_log["error"] = f"Unknown time_text: {time_text}"
        return False

    court_sport_id = COURT_SPORT_IDS.get(court)
    if not court_sport_id:
        print(f"  book_slot_api: unknown court '{court}'")
        transaction_log["error"] = f"Unknown court: {court}"
        return False

    date_str = target_date.isoformat()
    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    end_str = (start_dt + timedelta(hours=1)).strftime("%H:%M:%S")
    reservation_url = "https://api.rec.us/v1/reservations"
    reservation_body = {
        "courtSportIds": [court_sport_id],
        "from": {"date": date_str, "time": time_str},
        "participantUserId": participant_user_id or PARTICIPANT_USER_ID or _rec_user_id(jwt),
        "to": {"date": date_str, "time": end_str},
    }

    print(f"  Booking Court {court} {date_str} {time_text} via API…")
    s, order = _rec_api(
        reservation_url,
        method="POST",
        body=reservation_body,
        jwt=jwt,
    )
    transaction_log["reservation"] = {
        "request": {
            "method": "POST",
            "url": reservation_url,
            "body": reservation_body,
        },
        "response": {"status": s, "body": order},
    }
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

    payment_url = f"https://api.rec.us/v1/orders/{order_id}/pay"
    payment_body = {"data": {"payments": payments}}
    s2, result = _rec_api(
        payment_url,
        method="POST",
        body=payment_body,
        jwt=jwt,
    )
    transaction_log["payment"] = {
        "request": {
            "method": "POST",
            "url": payment_url,
            "body": payment_body,
        },
        "response": {"status": s2, "body": result},
    }
    if s2 == 200:
        # If payment is pending, confirm the Stripe PaymentIntent immediately —
        # rec.us cancels it within ~5 s if it isn't confirmed client-side.
        data_obj = result.get("data") or {}
        included_obj = result.get("included") or {}
        stripe_payments = (included_obj.get("payments") or [])
        if data_obj.get("status") == "pending" and stripe_payments:
            gd = (stripe_payments[0].get("gatewayData") or {})
            pi_id  = gd.get("paymentIntentId")
            cs     = gd.get("clientSecret")
            pms    = gd.get("paymentMethods") or []
            pm_id_stripe = pms[0].get("id") if pms else None
            if pi_id and cs and pm_id_stripe and STRIPE_PUBLISHABLE_KEY:
                print(f"  Confirming Stripe PI {pi_id[:28]}…")
                s_stripe, stripe_body = _stripe_confirm_payment_intent(pi_id, cs, pm_id_stripe)
                print(f"  Stripe confirm [{s_stripe}]: status={stripe_body.get('status')}")
                transaction_log["stripe_confirm"] = {"status": s_stripe, "body": stripe_body}

        expected = {"date": date_str, "time": time_text, "court": court}
        verification = {"expected": expected, "attempts": [], "confirmed": False}
        transaction_log["verification"] = verification
        for check in range(1, 4):
            try:
                reservations = fetch_rec_my_reservations(jwt)
            except Exception as exc:
                verification["attempts"].append({
                    "attempt": check,
                    "error": str(exc),
                })
                reservations = []
            else:
                matched = expected in reservations
                verification["attempts"].append({
                    "attempt": check,
                    "matched": matched,
                    "reservations": reservations,
                })
            if expected in reservations:
                verification["confirmed"] = True
                print(f"  Confirmed! Court {court} {date_str} {time_text}.")
                return True
            if check < 3:
                time.sleep(1)
        print(
            f"  Payment returned 200 but reservation was not confirmed: "
            f"{json.dumps(result)}"
        )
        return False

    print(f"  Payment failed [{s2}]: {json.dumps(result)[:200]}")
    return False
