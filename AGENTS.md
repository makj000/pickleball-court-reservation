# Pickleball Court Monitor — Requirements

Build a pickleball court availability monitor for rec.us Foster City.

## Core behavior

- Log into rec.us using stored credentials (email + password)
- Navigate to specific dates using the site's date picker UI
- Scrape all visible time slot buttons, cycling through the carousel ("Next slot") to catch off-screen ones
- For each available time slot, click it to open the modal and extract which courts are available (e.g. "Court 4 - Pickleball")
- Return structured data: date → list of `{time, courts[]}`
- Expose `/state` as per-court availability, not just slot-level booleans
- Treat court preference as `6 > 4 > 5` for display, state, and any future booking automation

## Deployment target

AWS Lambda, packaged as a Docker container (Lambda Python 3.12 base image + Playwright/Chromium).

## Two invocation modes

- `mode=sync` — run the scan inline, return JSON immediately
- `mode=async` (default) — fire-and-forget: re-invoke the same Lambda asynchronously, return HTTP 202, and send an SMS via AWS SNS when the scan completes

## API surface

- Single `GET /scan` endpoint behind HTTP Basic Auth
- Query params:
  - `mode` — `sync` or `async`
  - `days` — number of consecutive days to scan (1–7), starting today or `start_date`
  - `start_date` — optional starting date (YYYY-MM-DD)
  - `dates` — optional comma-separated explicit dates (YYYY-MM-DD)
  - `time` — optional exact time filter (e.g. `8:00 AM`); omit for all visible slots
- Fall back to dates from `config.json` if no date params are provided
- CORS support (OPTIONS preflight)

## Notifications

All notifications go to Telegram (no SMS/SNS). Triggers:

### Slot availability alerts (watched & auto-book slots only)
- **Hourly scheduled scan** — if any watched or auto-book slot is open, sends:
  `Pickleball slot(s) now available:\n{date} {time}: open Court {N} (watched/auto-book)`
- **Targeted daily scan** (8 AM & 9 AM EventBridge tick) — same message format
- **Ad-hoc full scan** (user-triggered) — same message format

### Auto-booking flow
- `🎯 Trying to book {date} {time} (courts: ...)` — sent before each booking attempt
- `Auto-booked pickleball slot(s):\n{date} {time} Court {N}` — sent on booking success
- `❌ Auto-book login failed: {exc}` — sent when Firebase login fails before booking
- `❌ Failed to book {date} {time} after 5 attempts` — sent when all retries fail
- `⚠️ Booking rate limit reached (N in window). Halting auto-book.` — sent when the rate cap is hit

### 8 AM release probe session (7:58–8:02 AM window)
Queued from the 7:45 AM EventBridge tick; fires ~7:58 AM via SQS (780s delay).
- `❌ Release probe login failed: {exc}` — if Firebase login fails at session start
- Booking attempt/success/failure messages same as auto-booking flow above (sent per probe)
- **End-of-session summary** (always sent at ~8:02 AM):
  ```
  8am session done: {N} probes
  ✅ Booked: {date} {time} Court {N}        ← if any booking succeeded
  ⚠️ Saw open but not booked: {date} {time} ← if open slots seen but not booked
  No openings found                          ← otherwise
  ⚠️ {N} probe error(s)                     ← appended if any probe threw
  ```

### What does NOT trigger a notification
- Slots that are neither watched nor in auto-book
- Individual probe results during the 8 AM burst phase (only the end summary is sent)
- Scheduled scan runs that find nothing open

## Booking Agent (booking_agent.py)

An AI agent (Claude claude-sonnet-4-6) that wraps the 8 AM release probe session with intelligent decision-making and Telegram reporting. Triggered by two dedicated EventBridge rules daily.

### Architecture

```
7:30 AM PT ──► EventBridge (booking-agent-prep)
                    │
                    ▼
              booking_agent.run_agent("prep")
                    │  Claude reasons: which date needs booking?
                    │  Is there already a reservation this weekend?
                    │
                    ├── get_context()       ← reads state from S3
                    ├── set_auto_book()     ← writes target to auto_book_slots
                    └── send_message()      ← Telegram preview

7:45 AM PT ──► EventBridge (existing rule)
                    │
                    └── queues release_probe_session via SQS (780s delay)

~7:58 AM PT ◄── SQS trigger
                    │
                    ▼
              _run_release_probe_session()   ← existing scheduler.py
                    │  pre  (7:58–8:00): probes every 15s
                    │  burst (8:00–8:00:30): back-to-back probes for 9:00 AM
                    │  post  (8:00:30–8:02): probes every 15s
                    │  books from auto_book_slots on first open court
                    └── writes release_probe_log to state

8:10 AM PT ──► EventBridge (booking-agent-report)
                    │
                    ▼
              booking_agent.run_agent("report")
                    │  Claude reads probe log + reservations
                    │
                    ├── get_context()       ← reads state, probe log, reservations
                    └── send_message()      ← Telegram report
```

### Agent tools
| Tool | Phase | Effect |
|------|-------|--------|
| `get_context` | both | Returns today/new_day/reservations/auto_book_slots/probe_log |
| `set_auto_book` | prep | Writes target {date, time} pairs to `auto_book_slots` in state |
| `send_message` | both | Sends Telegram message to configured chat |
| `done` | both | Signals end of agent loop |

### What Claude decides
**Prep phase:** Should I queue a booking for the date 14 days from today?
  - Skip if it's a weekday
  - Skip if there's already a reservation on that weekend (Sat or Sun) for both slots
  - Otherwise add both 9:00 AM (priority) and 8:00 AM to auto_book_slots and send a preview

**Report phase:** What happened during the 7:58–8:02 AM probe session?
  - How many probes ran, when did the slot open, was it booked?
  - If nothing booked: was there no target, or did the slot never open?

### EventBridge rules (created by deploy.sh)
- `booking-agent-prep`: `cron(30 14 * * ? *)` = 7:30 AM PDT (14:30 UTC)
- `booking-agent-report`: `cron(10 15 * * ? *)` = 8:10 AM PDT (15:10 UTC)

> Note: cron times assume PDT (UTC-7, summer). Adjust to 15:30/16:10 UTC for PST (winter).

## Booking safety limits (scanner.py)

Two guards run before every auto-book attempt to prevent runaway bookings:

### Per-day cap (`_DAY_CAP = 2`)
Before booking a slot on date D, the app counts existing `my_reservations` for D plus slots already booked in the current scan session. If the total is ≥ 2, that slot is skipped.

### Rate limit (`_RATE_CAP = 4`)
Tracks all app-initiated bookings in `state["app_booking_log"]` (each entry: `{booked_at, date, time, court}`). The rate window varies by time of day (PT):
- **Daytime 8am–11pm**: rolling 1-hour window
- **Nighttime 11pm–8am**: since the most recent 11pm PT

If `_recent_booking_count(state) >= 4` before a booking attempt, the loop `break`s immediately and sends a Telegram alert. State is saved to S3 after every successful booking so the counter persists across Lambda invocations.

### Payment
Bookings use rec.us credit first (`creditAdjustment: min(max_credit, total)`); any remainder is charged to the credit card on file. There is no credit-only guard — partial credit + card is allowed.

## Supporting pieces

- `authorizer.py` — separate Lambda for API Gateway HTTP Basic Auth
- `config.json` — base URL and default check dates
- `openapi.yaml` — OpenAPI 3.1 spec describing the API
- `static/` — frontend (separate, not part of the Lambda)

## Operational notes

- Do not trust cached `/state` alone when the official rec.us site disagrees with the monitor.
- Always compare `GET /state` with `GET /scan?mode=sync...` for the same dates before changing scraper logic.
- Check `last_scanned` from `/state` immediately; an old timestamp usually means stale cache, not broken parsing.
- For Foster City, court availability comes from the modal's `Select a court` combobox options, not just the currently selected value.
- `/state`, `/watch`, and `/my-reservations` are keyed by `{date, time, court}`.
- Legacy slot-level state may still exist in S3; normalize or rebuild it before trusting per-court results.
- Manual/internal scheduled refreshes must bypass `scan_interval_hours`; otherwise a forced refresh can silently preserve stale cache.
- After deploying scraper changes, trigger a forced scheduled refresh so `/state` is rebuilt on the new code instead of serving pre-deploy cache for the next interval.
- The frontend sign-in modal is for monitor API Basic Auth, not rec.us credentials.
- The frontend caches the API auth token in browser `localStorage`.
- The frontend auth modal can be dismissed with `Escape` or `Cancel` to reach the main page header and reopen `Credentials` manually.
- The frontend has a collapsible API log panel at the bottom of the screen that shows every request with timestamp, method, path, HTTP status, and response body preview. Keep this panel — it is the primary debugging tool for diagnosing auth failures, stale state, and unexpected API errors without opening DevTools.
