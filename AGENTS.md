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

## Weekend-target checklist (auto_book_slots / watched_slots)

The single source of truth for what gets auto-booked ($) vs. only watched (👁) for any given weekend date + time is `auto_book_slots` / `watched_slots`, editable directly in the big timetable in the UI (scroll down — it now renders `LOOKAHEAD_DAYS` = 23 days out). `_weekend_release_booking_times()` reads this at release time; an empty checklist for a date means "don't book that weekend," never a hardcoded fallback.
判断某个周末的某个时段是该自动预订（$）还是只关注（👁），唯一的依据是 `auto_book_slots` / `watched_slots`，可以直接在前端大表格里编辑（往下滚动——现在会渲染到 `LOOKAHEAD_DAYS` = 23 天后）。抢票时 `_weekend_release_booking_times()` 读取的就是这份数据；某个日期的 checklist 为空，就意味着"这个周末不订"，绝不会退回到任何写死的默认值。

Eligible times: `WEEKEND_TARGET_TIMES` = 8:00 AM, 9:00 AM, 10:00 AM (config.py). At release time, `_blind_book_release_targets` races one rec.us account per checked time in parallel — 2 accounts/2 times or 3 accounts/3 times, whatever's actually checked.
可选时段：`WEEKEND_TARGET_TIMES` = 8:00 AM、9:00 AM、10:00 AM（config.py）。抢票时 `_blind_book_release_targets` 会为每个被勾选的时段并行分配一个 rec.us 账号——实际勾选了几个时段就用几个账号（2或3个）。

## Booking Agent (booking_agent.py)

Claude (claude-haiku-4-5) runs two phases, both dispatched from `run_agent(phase)`. Each phase's Telegram message is the only automatic notification for its concern — there is no separate "watch agent" anymore (retired; its history-pattern judgment was folded into the prep phase below).
Claude（claude-haiku-4-5）运行两个阶段，都通过 `run_agent(phase)` 分发。每个阶段的 Telegram 消息就是该职责唯一的自动通知——不再有单独的"watch agent"了（已退役；它基于历史规律的判断逻辑已经并入下面的 prep 阶段）。

### prep phase — weekend-target planner
EventBridge rule `pickleball-booking-agent-prep`, `cron(30 14 ? * FRI,SAT,SUN *)` (7:30 AM PT, **Fri/Sat/Sun only** — Mon-Thu runs are a mathematically guaranteed no-op: today+21 shares today's weekday, today+22 and today+15 are both tomorrow's weekday, so none of the three offsets can land on a weekend unless today is Fri/Sat/Sun). `run_agent("prep")` also has its own cheap pre-check that skips the model call entirely if there's truly nothing to do, as a second layer independent of the schedule.

Job: for the weekend newly entering the 21-22-day preview window, and not yet touched (`weekend_target_prefilled_dates`), decide default checklist state:
- Holiday-adjacent (`holidays` package, checks Fri/Sat/Sun/Mon around the weekend) → all three times default to 👁 watch, never auto-book.
- Otherwise → all three times default to $ auto-book.
- Then cross-checks `booking_history` (via `_history_summary`, moved here from the old watch_agent.py): a time with a clear, consistent pattern of *not* being played gets moved to watch instead — but a time with no history at all (e.g. a newly added slot) is left at the default, never penalized for lack of data.

Also checks the weekend 15 days out (tomorrow's live release target, if tomorrow is a release day) and warns in the same Telegram message if nothing is checked to book there — the last chance to notice before it's too late.

Never overwrites a date already in `weekend_target_prefilled_dates` — the checklist reflects the user's own edits from then on.

### 7:45 AM PT — release probe queued
Existing `pickleball-release-probe-queue` rule queues `_run_release_probe_session()` via SQS (~780s delay, fires ~7:58 AM):
- pre (7:58–8:00): probes every 15s
- burst (8:00–8:02): `_blind_book_release_targets` races one account per checked time in parallel (see checklist section above)
- writes `release_probe_log` to state

### report phase — 8:10 AM PT
EventBridge rule `booking-agent-report`, daily. Reads `release_probe_log` + `my_reservations`, sends a Telegram report on what happened during the 7:58–8:02 AM burst.

### Recurring scan (`_run_targeted_daily_scan`)
Runs once daily via `scan_interval_hours = 24.0` (10 PM PT, `_DAILY_SCAN_HOUR_PT` in scheduler.py) instead of every 3 hours — weekdays don't need frequent checking since nothing gets booked on them; the checklist plus the dedicated 7:58 AM release burst already cover what matters. Configurable in the UI frequency dropdown.
现在通过 `scan_interval_hours = 24.0` 每天只跑一次（太平洋时间晚上10点，对应 scheduler.py 里的 `_DAILY_SCAN_HOUR_PT`），而不是每3小时一次——工作日不需要频繁检查，因为工作日不会订场，checklist 加上专门的7:58 AM 抢票已经覆盖了真正重要的部分。可以在前端的频率下拉菜单里配置。

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
Bookings use rec.us credit first against the payable remaining amount (`totalAmountRemaining` when rec.us provides it, otherwise `total`); any remainder is charged to the credit card on file. There is no credit-only guard — partial credit + card is allowed.

预订会优先使用 rec.us credit，并且基于实际还需支付的金额付款（如果 rec.us 提供 `totalAmountRemaining` 就用它，否则回退用 `total`）；剩余金额会从已绑定信用卡扣款。没有 credit-only 保护——允许部分 credit 加信用卡付款。

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
