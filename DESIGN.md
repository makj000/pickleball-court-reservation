# Pickleball Monitor — Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          YOUR DEVICES                               │
│                                                                     │
│  Browser ──────────────────────────────────┐                        │
│  Telegram app ─────────────────────────────┼──► (internet)          │
└────────────────────────────────────────────┼────────────────────────┘
                                             │
              ┌──────────────────────────────┼──────────────────────────┐
              │  AWS us-west-2               │                          │
              │                             ▼                           │
              │  S3: your-ui-s3-bucket/index.html                  │
              │        (serves static UI)    │                          │
              │                             │ API calls (Basic Auth)    │
              │                             ▼                           │
              │  ┌──────────────────────────────────────────────┐      │
              │  │   Lambda: <function-name>                  │      │
              │  │   (Docker / ECR, Python 3.12 + Playwright)   │      │
              │  │                                               │      │
              │  │  Entrypoints:                                 │      │
              │  │   • Function URL  ◄── Browser / Telegram      │      │
              │  │   • EventBridge   ◄── cron every 15 min       │      │
              │  │   • SQS trigger   ◄── delayed self-queued work│      │
              │  │                                               │      │
              │  │  Routes handled:                              │      │
              │  │   GET  /state          GET  /scan             │      │
              │  │   PUT  /watch          POST /force-scan        │      │
              │  │   PUT  /auto-book      PUT  /scan-interval     │      │
              │  │   GET  /my-reservations                        │      │
              │  │   PUT  /my-reservations (manual refresh)       │      │
              │  │   PUT  /auto-watch-weekends                    │      │
              │  │   PUT  /focus-newest-weekend                   │      │
              │  │   POST /telegram  (Telegram webhook)           │      │
              │  └──────────┬────────────────────────────────────┘      │
              │             │                                            │
              │    ┌────────┼────────────────────────────────┐          │
              │    │        │  Lambda talks to:               │          │
              │    │        ▼                                 │          │
              │    │  S3 (state.json, bot chat history,       │          │
              │    │      telegram_usage.json, scan history)  │          │
              │    │        │                                 │          │
              │    │        ▼                                 │          │
              │    │  SQS: your-sqs-queue-name            │          │
              │    │  (schedules delayed probes, up to 15min) │          │
              │    │        │                                 │          │
              │    │        ▼                                 │          │
              │    │  SNS → SMS  ─┐                           │          │
              │    │  SES → Email ┼─ all fire together on     │          │
              │    │  Telegram   ─┘   every notify() call     │          │
              │    └─────────────────────────────────────────┘          │
              └──────────────────────────────────────────────────────────┘
                             │
              ┌──────────────┼──────────────────────────────────────────┐
              │  External    │                                           │
              │              ▼                                           │
              │  rec.us API (api.rec.us/v1/...)                         │
              │   • Firebase REST auth  (token, ~0.4s)                  │
              │   • Availability scan   (3 courts × 1 req, parallel)    │
              │   • Booking + credit checkout  (on open auto-book slot) │
              │   • My reservations sync                                 │
              │                                                          │
              │  Playwright/Chromium  (only for booking confirmation UI) │
              │                                                          │
              │  Telegram Bot API                                        │
              │   • Outbound: slot alerts, booking results               │
              │   • Inbound:  /telegram webhook → Claude API →           │
              │               tool calls (add/remove watches, scan, etc) │
              │                                                          │
              │  Anthropic Claude API  (Telegram bot reasoning)          │
              └──────────────────────────────────────────────────────────┘
```

## Key flows

**Scheduled scanning:** EventBridge fires every 15 min → Lambda checks if a real scan is due
(based on `scan_interval_hours`) → runs scan → queues an SQS follow-up probe for sub-15-min
precision around slot-release times (weekend burst). SQS → Lambda handles delayed probes
without sleeping inside a running invocation.

**Booking:** scan finds an `auto_book` slot open → Playwright starts → Firebase REST auth →
booking API POST → up to 5 retries → `notify()` fires SMS + email + Telegram with result.

**Telegram bot:** inbound message → `/telegram` webhook → Lambda passes conversation to
Claude API with tool definitions → Claude calls tools (add/remove watches, trigger scan,
check state) → Lambda executes tools → reply sent back via Telegram Bot API.

**Notifications:** `notify()` attempts SMS (SNS), email (SES), and Telegram independently
in sequence; each failure is logged and swallowed so the others still fire.
