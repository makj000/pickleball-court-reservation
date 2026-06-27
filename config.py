import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*_args, **_kwargs):
        return False

APP_DIR = Path(__file__).parent
CONFIG_FILE = APP_DIR / "config.json"
load_dotenv(APP_DIR / ".env")

PT = ZoneInfo("America/Los_Angeles")
APP_VERSION = "2.4.1"
SCAN_HISTORY_MAX = 300

# ── Credentials ───────────────────────────────────────────────────────────────
EMAIL    = os.environ.get("REC_US_LOGIN") or os.environ.get("EMAIL", "")
PASSWORD = os.environ.get("REC_US_PASSWORD") or os.environ.get("PASSWORD", "")
EMAIL2    = os.environ.get("REC_US_LOGIN2") or os.environ.get("EMAIL2", "")
PASSWORD2 = os.environ.get("REC_US_PASSWORD2") or os.environ.get("PASSWORD2", "")
# ─────────────────────────────────────────────────────────────────────────────

HEADLESS      = True
DEFAULT_TIME_FILTER = "8:00 AM"
NOTIFY_NUMBER = os.environ.get("NOTIFY_NUMBER", "")
NOTIFY_EMAIL  = os.environ.get("NOTIFY_EMAIL", "")
REPORT_EMAIL  = os.environ.get("REPORT_EMAIL") or NOTIFY_EMAIL or os.environ.get("EMAIL", "")

STATE_BUCKET  = os.environ.get("STATE_BUCKET", "")
WORK_QUEUE_URL = os.environ.get("PICKLEBALL_QUEUE_URL") or os.environ.get("WORK_QUEUE_URL", "")
STATE_KEY           = "state.json"
TELEGRAM_USAGE_KEY  = "telegram_usage.json"
TELEGRAM_USAGE_MAX  = 100
SCAN_LOCK_TTL_SECONDS = 20 * 60
SYNC_TOKEN_TTL_SECONDS = 300
SQS_DELAY_MAX_SECONDS = 15 * 60
SQS_STALE_GRACE_SECONDS = 5 * 60
_RELEASE_PRE_INTERVAL_S  = 15
_RELEASE_BURST_UNTIL_S   = 30
_RELEASE_POST_INTERVAL_S = 15
_RELEASE_END_S           = 120

SYNC_SIGNING_SECRET = os.environ.get("SYNC_SIGNING_SECRET") or os.environ.get("API_PASSWORD", "")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

BOT_HISTORY_PREFIX    = "telegram_history/"
BOT_MAX_HISTORY_TURNS = 12

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, PUT, POST, OPTIONS",
    "Content-Type": "application/json",
}

SLOT_TIMES = [
    "8:00 AM", "9:00 AM", "10:00 AM", "11:00 AM",
    "4:00 PM", "5:00 PM", "6:00 PM",
]

COURT_PREFERENCE = ["6", "4", "5"]
TARGET_COURTS = COURT_PREFERENCE[:]

COURT_SITE_IDS: dict[str, str] = {
    "6": "a474166c-53fb-4444-9f49-e5da379deab0",
    "4": "ce22b935-aeb9-44ae-852d-bf2e7c91617c",
    "5": "445abe2b-cb2f-450d-a376-0f643890731c",
}
COURT_SPORT_IDS: dict[str, str] = {
    "6": "e4def6e2-b46d-4d1f-a44f-6bb65f603198",
    "4": "d3bfa8f9-03f4-4c80-ac27-fbb4dbfb9a15",
    "5": "671d9687-dfa5-4f1c-8d29-de68baf12137",
}
PARTICIPANT_USER_ID   = os.environ.get("PARTICIPANT_USER_ID", "")
PARTICIPANT_USER_ID2  = os.environ.get("PARTICIPANT_USER_ID2", "")
FIREBASE_API_KEY      = os.environ.get("FIREBASE_API_KEY", "")
# rec.us's Stripe publishable key (embedded in their frontend JS — public)
STRIPE_PUBLISHABLE_KEY = "pk_live_51MPUx4CMyY4UUjhBlgalg5uPiGdXHOWbOTEOioIXfReEeAuLviTRXhdTGvZtTnYtDm2eZonv8buTf73YKIzJHV4i00YikF7WiB"
FIREBASE_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
    f"?key={FIREBASE_API_KEY}"
)

_TIME_TEXT_TO_HHMMSS: dict[str, str] = {
    "8:00 AM": "08:00:00", "9:00 AM": "09:00:00", "10:00 AM": "10:00:00",
    "11:00 AM": "11:00:00", "4:00 PM": "16:00:00", "5:00 PM": "17:00:00",
    "6:00 PM": "18:00:00",
}

TIME_RE  = re.compile(r"^\d{1,2}:\d{2}\s*(AM|PM)", re.IGNORECASE)
COURT_RE = re.compile(r"^court\s+(\d+)", re.IGNORECASE)


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def load_check_dates():
    from datetime import date
    cfg = load_config()
    return sorted(date.fromisoformat(d) for d in cfg.get("check_dates", []))


def build_next_dates(days: int, start=None):
    from datetime import date, timedelta
    start = start or date.today()
    return [start + timedelta(days=offset) for offset in range(days)]


BASE_URL      = load_config()["base_url"]
SYNC_SCAN_URL = load_config().get("sync_scan_url", "")


def _time_text_to_hhmm(time_text: str) -> str:
    """'9:00 AM' → '09:00'  (matches HH:MM prefix in API start_time strings)."""
    from datetime import datetime as _dt
    try:
        return _dt.strptime(time_text.strip(), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return ""


# Build lookup once at import time: "09:00" → "9:00 AM"
_HHMM_TO_TIME_TEXT: dict[str, str] = {
    _time_text_to_hhmm(t): t for t in SLOT_TIMES if _time_text_to_hhmm(t)
}
