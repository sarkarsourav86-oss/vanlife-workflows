# vanlife-workflows

Phase 1 of an AI-powered vanlife automation stack built on the [Campflare API](https://docs-v2.campflare.com/welcome), Anthropic Claude, and Modal.

The starter workflow (`mn_weekday_finder`) watches northern Minnesota campgrounds for weekday openings in the summer months and pings you on Discord when something opens up.

## What you're learning here

- **API client design** — typed Python wrapper over a real REST API (Pydantic + httpx)
- **Webhooks, both sides** — sending (Discord) and receiving (Campflare → your endpoint)
- **Serverless deploys** — Modal crons and HTTP endpoints, no server to manage
- **Structured LLM output** — LangChain + Claude Haiku turning raw JSON into human-readable alerts
- **Cost observability** — every LLM and API call logged to SQLite with an estimated dollar cost
- **Prompt caching** — the single biggest cost lever in LLM apps

What this project deliberately does *not* teach yet: LangGraph state machines (Phase 2), tool-calling agents (Phase 3), vector DBs (not needed), LangSmith tracing (Phase 2).

## Layout

```
src/
  campflare.py          # typed Campflare client
  discord.py            # post_to_discord()
  cost_tracker.py       # SQLite log of every external call with $ cost
  alert_formatter.py    # LangChain: Haiku → human-readable alert message
  workflows/
    mn_weekday_finder.py   # Workflow #1: find + alert on MN summer weekday openings
    webhook_handler.py     # handle inbound alert webhooks from Campflare
modal_app.py            # Modal deploy: daily cron + webhook endpoint
```

## Setup

1. **Install Python 3.11+**, then:
   ```bash
   pip install -e .
   ```

2. **Copy env vars**:
   ```bash
   cp .env.example .env
   # fill in CAMPFLARE_API_KEY, ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL
   ```

3. **Discord webhook (2 min)** — in any Discord server you own, right-click a channel →
   *Edit Channel* → *Integrations* → *Webhooks* → *New Webhook* → *Copy URL*.

4. **Modal account (free)**:
   ```bash
   pip install modal
   modal token new
   ```

## Run locally first

Before deploying, sanity-check each piece locally:

```bash
# 1. Does Discord work?
python -c "from src.discord import post_to_discord; post_to_discord('hello from vanlife')"

# 2. Does Campflare work?
python -m src.workflows.mn_weekday_finder --dry-run

# 3. Does the LLM formatter work?
python -m src.alert_formatter --demo
```

## Deploy to Modal

```bash
modal deploy modal_app.py
```

This gives you:
- A daily cron that refreshes the MN weekday alert
- A public HTTPS webhook endpoint for Campflare to POST to

Copy the printed webhook URL into your Campflare dashboard (or set `CAMPFLARE_WEBHOOK_URL` and let the cron register it).

## Cost expectations

Personal use, free tiers on Modal/Supabase/Upstash, Haiku for formatting:
**$0–$5/month**, dominated by LLM calls. See `src/cost_tracker.py` for live tracking.

## Next steps (later phases)

- Phase 2: rebuild "auto-replan when a site closes" as a LangGraph state machine with human-in-the-loop.
- Phase 3: natural-language trip planner — LLM with Campflare endpoints as tools.
