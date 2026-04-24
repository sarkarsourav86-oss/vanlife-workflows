# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[dev]"          # install with dev extras (pytest, ruff)
ruff check .                     # lint (line-length 100, target py311)

# Local sanity checks for each component
python -c "from src.discord import post_to_discord; post_to_discord('hello from vanlife')"
python -m src.workflows.mn_weekday_finder --dry-run   # search only, no alert created
python -m src.workflows.mn_weekday_finder             # create/refresh alert
python -m src.alert_formatter                         # demo LLM formatting with a canned payload
python -m src.cost_tracker                            # print LLM/API cost summary from SQLite

# Modal deploy (requires `modal token new` first)
modal deploy modal_app.py
```

There is no test suite yet. When adding tests, `pytest` is already a dev dependency.

## Architecture

The codebase is organized as three layers plus a deployment shim:

1. **Integration primitives** ([src/campflare.py](src/campflare.py), [src/discord.py](src/discord.py), [src/alert_formatter.py](src/alert_formatter.py)) — thin, typed wrappers over external services. Each one is independently runnable/debuggable.
2. **Workflows** ([src/workflows/](src/workflows/)) — compose the primitives into end-to-end tasks. Each workflow owns both the outbound side (creating a Campflare alert) and the inbound side (handling the webhook when it fires).
3. **Cost observability** ([src/cost_tracker.py](src/cost_tracker.py)) — every Campflare HTTP call and every LLM call is logged to a SQLite DB (`cost_tracker.db` by default, override with `COST_DB_PATH`). `log_api_call` is a context manager used by the Campflare client; `log_llm_call` is invoked manually after each LangChain call using `usage_metadata` from the raw message.
4. **Deployment** ([modal_app.py](modal_app.py)) — Modal app exposing two things: a daily cron (`refresh_mn_alert`) and a public FastAPI webhook (`campflare_webhook`). Secrets are pulled from three named Modal secrets: `campflare`, `anthropic`, `discord`.

### The alert loop (Workflow #1)

The `mn_weekday_finder` workflow splits into two halves that meet through a webhook round-trip:

- **Outbound** ([src/workflows/mn_weekday_finder.py](src/workflows/mn_weekday_finder.py)): searches for ≤12 northern-MN campgrounds (Campflare caps alerts at 12 campground IDs) and creates one `Availability Alert` covering that summer. `summer_window()` rolls forward to next year if called after August. Metadata on the alert carries `weekdays_only: True` — Campflare has no server-side weekday filter, so this flag survives the round-trip and instructs the inbound side to post-filter.
- **Inbound** ([src/workflows/webhook_handler.py](src/workflows/webhook_handler.py)): entry point `handle_alert(payload)` reads that metadata, filters openings to Mon–Thu nights if set, asks [src/alert_formatter.py](src/alert_formatter.py) (Haiku via `ChatAnthropic.with_structured_output`) for summary/highlights/urgency, and posts a Discord embed built by `availability_embed()`.

When adding new workflows, mirror this split and register a new Modal function (cron + handler) in [modal_app.py](modal_app.py).

### Campflare client conventions

- Base URL is `https://api.campflare.com/v2`. Auth header is the raw API key — **no `Bearer ` prefix**.
- `Campground` uses `model_config = {"extra": "allow"}` so the client tolerates new fields from the API without breaking.
- `bulk_availability` rejects >25 IDs; `create_alert` rejects >12 campground IDs (Pydantic `max_length=12` on `CreateAlertRequest.campground_ids`).
- The client is a context manager — always use `with CampflareClient() as client:` so the underlying `httpx.Client` closes.
- `AvailabilityFilter` uses `date_ranges: list[DateRange]` (with `starting_date`/`ending_date`/`nights` per range) — **not** flat top-level `start_date`/`end_date`/`nights`. The server rejects the flat shape with `missing field date_ranges`.
- `status="open"` combined with `kind="established"` on `CampgroundSearchRequest` intersects too narrowly and returns zero results against real data. Each works individually; together they don't. Avoid the combo.
- Alert endpoints don't follow a single convention: `POST /alert/create`, `GET /alert/{id}`, `POST /alert/{id}/cancel`, `POST /alert/{id}/test`. Don't normalize them into a single pattern — the server will 404/405 in surprising ways.
- Webhook payloads are one-notification-per-POST — a flat dict with `campground_name`, `campsite_name`, `reservation_url`, `date_range: {starting_date, nights}`, `metadata`, etc. Not a nested `openings[]` array. Campflare fans out multiple matches as multiple webhook POSTs.

### Webhook JWT verification

Campflare signs every webhook with an HS256 JWT carrying `{event, notification_id, iat}` in the `Authorization` header (no `Bearer ` prefix). The shared secret is set on the account page at https://campflare.com/platform and must also be in `CAMPFLARE_JWT_SECRET` (both `.env` locally and the `campflare` Modal secret).

Gotcha: the secret is distributed as a **base64 string**, but Campflare signs with the **decoded bytes**. Passing the raw string to `jwt.decode` produces `"Signature verification failed"` even when the secret "looks right". [modal_app.py](modal_app.py) does `base64.urlsafe_b64decode(secret + "==")` before verification — don't remove that.

### Modal deploys

Default rolling deploys can serve stale code when only files under `src/` change (the image hash doesn't move, so old containers keep running). If a deploy looks like it succeeded but the behavior didn't change, use:

```
python -m modal deploy modal_app.py --strategy recreate
```

This terminates running containers and forces new ones to pick up the fresh source mount.

### LLM cost tracking

When adding a new LLM call site:
1. Use `ChatAnthropic(...).with_structured_output(Model, include_raw=True)` so you still have access to `usage_metadata`.
2. After `chain.invoke(...)`, pull `result["raw"].usage_metadata` and pass `input_tokens`, `output_tokens`, and `input_token_details.cache_read` (for cached tokens) into `log_llm_call`.
3. If using a new model, add its pricing to the `PRICING` dict in [src/cost_tracker.py](src/cost_tracker.py) — `log_llm_call` raises on unknown models by design.

Default model for formatting-style tasks is `claude-haiku-4-5` (cheap, structured output reliable). Reach for Sonnet/Opus only when the task actually requires reasoning.
