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

## ReserveMN API (UseDirect) reverse-engineering notes

The [ReserveMN](https://reservemn.usedirect.com/MinnesotaWeb/) booking system is a ClojureScript SPA built on the UseDirect platform. Key findings for MN state parks:

### API base

```
https://mnrdr.usedirect.com/minnesotardr/rdr/
```

Set at runtime as `globalThis.apiurl`. Enterprise name is `Minnesota`.

### Useful endpoints

| Endpoint | Method | Notes |
|---|---|---|
| `fd/places` | GET | All MN state parks with `PlaceId` |
| `fd/facilities` | GET | All campground facilities with `FacilityId`, `PlaceId` |
| `fd/placeinfo/{placeId}` | GET | Park contact info, hours |
| `fd/placeinfo/additional-place-info/{placeId}` | GET | Extended park info |
| `fd/citypark/{placeId}` | GET | Park lat/lon + **`EnterpriseId`** (= 1 for MN) |
| `search/place` | POST | Availability by park — returns facilities + unit-type counts, not individual sites |
| `search/grid` | POST | **Per-site availability grid** — returns `Units` dict with `UnitId`, `Name`, `Slices` (date → IsFree) |

### Mapping a site photo URL to a campsite name

ReserveMN site-specific photos follow this pattern:

```
https://reservemn.usedirect.com/MinnesotaWeb/images/Minnesota/ParkImages/Units/{UnitId}.jpg
```

To resolve `UnitId` → campsite name:

1. Find the park's `PlaceId` via `GET fd/places`
2. Find the facility's `FacilityId` via `GET fd/facilities` (filter by `PlaceId`)
3. Call `POST search/grid` with `{"FacilityId": <id>, "UnitTypeId": 0, "StartDate": "MM/DD/YYYY", "Nights": 1}` — note: a park may have multiple facilities (e.g. Upper/Lower Campground)
4. Match `UnitId` in the `Facility.Units` dict → `Name` field

**Example:** `images/Minnesota/ParkImages/Units/12286.jpg` resolves to:
- `UnitId` 12286 = **Drive-In #16**, Temperance River State Park — **Upper Campground** (FacilityId 783, PlaceId 103)
- Coordinates: lat 47.5545, lon -90.8716 · Max vehicle length: 50 ft

The Lower Campground at Temperance River is FacilityId 755 (UnitIds 13982–14039). Upper Campground is FacilityId 783 (UnitIds 12264–15410).

### Gotchas

- `GetFacilityData` on the `.aspx` web service returns 403 — use the `rdr/` REST API instead.
- `search/grid` requires a `POST`, not `GET`.
- The map icon image path (`MapInfo.UnitImage`) is different from the site photo path — icon uses `Minnesota/Units/{type}`, photo uses `Minnesota/ParkImages/Units/{UnitId}.jpg`.
- A single park can have multiple facilities (e.g. Upper vs Lower Campground) each with their own `FacilityId` and unit ID range — always check all facilities for a `PlaceId`.

## Next steps (later phases)

- Phase 2: rebuild "auto-replan when a site closes" as a LangGraph state machine with human-in-the-loop.
- Phase 3: natural-language trip planner — LLM with Campflare endpoints as tools.
