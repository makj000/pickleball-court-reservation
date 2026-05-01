# Pickleball Court Monitor — Requirements

Build a pickleball court availability monitor for rec.us Foster City.

## Core behavior

- Log into rec.us using stored credentials (email + password)
- Navigate to specific dates using the site's date picker UI
- Scrape all visible time slot buttons, cycling through the carousel ("Next slot") to catch off-screen ones
- For each available time slot, click it to open the modal and extract which courts are available (e.g. "Court 4 - Pickleball")
- Return structured data: date → list of `{time, courts[]}`

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

Send SMS to a hardcoded number via AWS SNS when the async scan completes.

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
- Manual/internal scheduled refreshes must bypass `scan_interval_hours`; otherwise a forced refresh can silently preserve stale cache.
