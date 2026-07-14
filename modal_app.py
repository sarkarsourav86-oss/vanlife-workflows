"""Modal deployment: webhook endpoint + Discord interactions endpoint.

Deploy: `modal deploy modal_app.py`
Force fresh containers: `modal deploy modal_app.py --strategy recreate`

Provides:
  - `campflare_webhook` — public HTTPS endpoint for Campflare alert webhooks.
  - `discord_interactions` — public HTTPS endpoint for Discord slash commands.
    Handles both APPLICATION_COMMAND (type 2) and APPLICATION_COMMAND_AUTOCOMPLETE
    (type 4).
  - `refresh_region(region_name)` and `status_report` — work functions
    invoked via Modal's .spawn.aio() to stay under Discord's 3s reply limit.

After first deploy, paste the discord_interactions URL into the
"Interactions Endpoint URL" box on the Discord developer portal
(General Information tab). Discord will PING; we PONG; you save.
"""

from __future__ import annotations

import modal
from fastapi import Header, HTTPException, Request

# Two images, scoped per-function to keep cold-start fast on lean paths.
#
# endpoint_image: small (no LLM/vision deps). Used by HTTP endpoints whose
#   only job is to verify a signature and immediately spawn a worker.
#   Cold-start matters here because Discord enforces a 3-second reply
#   deadline on interactions.
# worker_image: extends endpoint_image with anthropic/langchain/pillow.
#   Used by functions that score images, call the LLM, or otherwise need
#   the heavy stack. Cold-start time is irrelevant — we already deferred.
#
# Industry pattern: smallest viable image per function. Same final-artifact
# concept as Dockerfiles, just declared in Python.

_base_image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "httpx>=0.27",
    "pydantic>=2.7",
    "fastapi>=0.115",
    "pyjwt>=2.9",        # Campflare webhook JWT verification
    "pynacl>=1.5",       # Discord interactions Ed25519 verification
    "python-dotenv>=1.0",
)

# add_local_python_source MUST be last in each chain — Modal warns otherwise.
endpoint_image = _base_image.add_local_python_source("src")

worker_image = (
    _base_image
    .pip_install(
        "anthropic>=0.40",
        "langchain>=0.3",
        "langchain-anthropic>=0.3",
        "pillow>=10",
    )
    .add_local_python_source("src")
)

app = modal.App("vanlife-workflows")

secrets = [
    modal.Secret.from_name("campflare"),          # CAMPFLARE_API_KEY, CAMPFLARE_JWT_SECRET, optional CAMPFLARE_WEBHOOK_URL
    modal.Secret.from_name("anthropic"),           # ANTHROPIC_API_KEY
    modal.Secret.from_name("discord"),             # DISCORD_WEBHOOK_URL, DISCORD_PUBLIC_KEY, DISCORD_APP_ID
    modal.Secret.from_name("openrouteservice"),    # ORS_API_KEY for road-accurate route weather
]

# Unified state Dict: {region_name: alert_id}. Replaces the previous
# per-workflow dicts (mn-weekday-alerts, np-camping-alerts).
region_alerts_state = modal.Dict.from_name("region-alerts", create_if_missing=True)

# Per-user alert state: {discord_user_id: [{alert_id, park, campground_ids, start, end}, ...]}
user_alerts_state = modal.Dict.from_name("user-alerts", create_if_missing=True)

# Poller state: kept for backwards compat but no longer used for availability snapshots.
poll_state = modal.Dict.from_name("poll-state", create_if_missing=True)

# Notification dedup cache: {alert_id|site_id|date: YYYY-MM-DD} — prevents duplicate DMs
# from both the poller and Campflare webhook firing for the same opening on the same day.
notif_cache = modal.Dict.from_name("notification-cache", create_if_missing=True)

# Volume holding the iOverlander+PAD-US enriched SQLite DB. Built locally by
# `scripts/build_dispersed_db.py` and uploaded with
# `modal volume put dispersed-data dispersed.db /dispersed.db --force`.
# Mounted read-only at /data in the dispersed_search worker; src/dispersed_db.py
# reads from $DISPERSED_DB_PATH.
dispersed_data_volume = modal.Volume.from_name("dispersed-data")


# ---------- Work functions (called via .spawn.aio() from interaction handler) ----------

@app.function(image=endpoint_image, secrets=secrets, timeout=600, retries=0)
def refresh_region(region_name: str, interaction_token: str | None = None) -> dict:
    """Rotate the alert for one region. PATCHes Discord followup if token given."""
    import os
    from src.workflows.region_finder import REGIONS, run

    if region_name not in REGIONS:
        msg = f"Unknown region: `{region_name}`. Known: {sorted(REGIONS.keys())}"
        if interaction_token:
            from src.discord_interactions import send_followup
            send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
        return {"error": msg}

    region = REGIONS[region_name]
    previous = region_alerts_state.get(region_name)

    new_id = run(
        region_name=region_name,
        previous_alert_id=previous,
        webhook_override_url=os.environ.get("CAMPFLARE_WEBHOOK_URL") or None,
        dry_run=False,
    )

    if new_id:
        region_alerts_state[region_name] = new_id
        msg = f"**{region.display_name}** alert refreshed: `{new_id}`"
    elif previous:
        try:
            del region_alerts_state[region_name]
        except KeyError:
            pass
        msg = f"**{region.display_name}** alert cancelled. No candidates found."
    else:
        msg = f"**{region.display_name}**: no candidates and no previous alert."

    if interaction_token:
        from src.discord_interactions import send_followup
        send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
    return {"region": region_name, "alert_id": new_id, "message": msg}


@app.function(image=endpoint_image, secrets=secrets, timeout=120, retries=0)
def status_report(interaction_token: str | None = None) -> dict:
    """Build and post a status report on every tracked region."""
    import os
    from src.workflows.status import build_status_report

    report = build_status_report(state=dict(region_alerts_state.items()))

    if interaction_token:
        from src.discord_interactions import send_followup
        send_followup(os.environ["DISCORD_APP_ID"], interaction_token, report)
    return {"report": report}


@app.function(image=endpoint_image, secrets=secrets, timeout=120, retries=0)
def mn_parks_check(
    start: str,
    nights: int = 1,
    interaction_token: str | None = None,
) -> dict:
    """One-shot MN state-park availability check. No alert created."""
    from src.workflows.mn_parks import build_embeds, find_availability, parse_date

    import os
    parsed = parse_date(start)
    if not parsed:
        msg = f"Couldn't parse date: `{start}`. Try `YYYY-MM-DD`."
        if interaction_token:
            from src.discord_interactions import send_followup
            send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
        return {"error": msg}

    results = find_availability(parsed, nights=nights)
    embeds = build_embeds(results, parsed, nights)

    if interaction_token:
        from src.discord_interactions import post_followup
        post_followup(interaction_token, {"embeds": embeds})

    return {"start": str(parsed), "nights": nights, "n_parks": len(results)}


@app.function(
    image=worker_image,
    secrets=secrets,
    volumes={"/data": dispersed_data_volume},
    timeout=300,
    retries=0,
)
def dispersed_search(
    location: str,
    radius_miles: float = 30.0,
    interaction_token: str | None = None,
) -> dict:
    """Resolve location, query the dispersed-camping DB, post a Discord embed.

    Uses the worker image because Starlink scoring on the top candidates is
    a Sonnet vision call. The dispersed-data volume mounts the prebuilt SQLite
    DB at /data/dispersed.db; we set DISPERSED_DB_PATH so src.dispersed_db
    reads from there instead of the default cwd-relative path.
    """
    import os
    os.environ["DISPERSED_DB_PATH"] = "/data/dispersed.db"

    from src.workflows.dispersed_finder import (
        build_embed,
        enrich_with_starlink,
        find_spots,
        parse_location,
    )

    coords = parse_location(location)
    if not coords:
        msg = f"Couldn't resolve location: `{location}`. Try `lat,lng` or a place name."
        if interaction_token:
            from src.discord_interactions import send_followup
            send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
        return {"error": msg}

    lat, lng = coords
    spots = find_spots(lat, lng, radius_miles=radius_miles)
    if spots:
        spots = enrich_with_starlink(spots)
    embed = build_embed(spots, origin=coords)

    if interaction_token:
        from src.discord_interactions import post_followup
        post_followup(interaction_token, {"embeds": [embed]})

    return {"location": location, "lat": lat, "lng": lng, "n_spots": len(spots)}


@app.function(image=endpoint_image, secrets=secrets, timeout=180, retries=0)
def weather_check(
    origin: str,
    destination: str,
    interaction_token: str | None = None,
) -> dict:
    """Check weather along a road route (origin → destination) and post a Discord embed.

    Geocodes both endpoints via Open-Meteo, fetches the actual road polyline via
    OpenRouteService, samples weather every ~50 miles, then posts a danger/clear embed.
    If dangerous conditions are found, also spawns weather_watch at the worst segment.
    """
    import os
    from src.workflows.weather_check import check_route, build_weather_embed
    from src.discord_interactions import post_followup

    result = check_route(origin, destination)
    embeds = build_weather_embed(result)

    if interaction_token:
        post_followup(interaction_token, {"embeds": embeds})

    if result.worst_segment:
        ws = result.worst_segment
        weather_watch.spawn(lat=ws.lat, lon=ws.lon, label=ws.label)

    return {
        "origin": origin,
        "destination": destination,
        "waypoints_checked": result.waypoints_checked,
        "all_clear": result.all_clear,
        "n_dangerous": len(result.dangerous_segments),
    }


@app.function(image=endpoint_image, secrets=secrets, timeout=86400, retries=0)
def weather_watch(lat: float, lon: float, label: str) -> dict:
    """Poll weather at a parked location until conditions clear, then post to Discord.

    Spawned automatically by weather_check when dangerous conditions are found.
    Polls every 30 min; posts a clearing embed when the next 2 hours look safe.
    Max runtime: 24 h (timeout=86400). Posts status updates every ~2 h while waiting.
    """
    import os
    from src.workflows.weather_check import start_weather_watch
    from src.discord import post_to_discord

    def _post(embed: dict) -> None:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
        post_to_discord(embeds=[embed], webhook_url=webhook_url)

    start_weather_watch(lat, lon, label, _post)
    return {"lat": lat, "lon": lon, "label": label}


_MAX_USER_ALERTS = 6


@app.function(image=endpoint_image, secrets=secrets, timeout=120, retries=0)
def user_watch_create(
    user_id: str,
    campground_id: str,
    start: str,
    end: str,
    nights: int = 1,
    campsite_kind: str | None = None,
    weekdays_only: bool = False,
    weekends_only: bool = False,
    campsite: str | None = None,
    interaction_token: str | None = None,
) -> dict:
    """Create a personal Campflare alert for a Discord user and DM them when it fires.

    Receives a specific campground_id chosen via autocomplete, creates an alert,
    stores it in the user-alerts Modal Dict, and posts a confirmation followup.
    """
    import os
    from datetime import date as _date
    from src.campflare import CampflareClient, CreateAlertRequest, AvailabilityFilter
    from src.discord_interactions import send_followup
    from src.workflows.region_finder import daily_ranges

    existing = list(user_alerts_state.get(user_id) or [])
    if len(existing) >= _MAX_USER_ALERTS:
        lines = "\n".join(
            f"• `{a['alert_id'][:8]}` — {a.get('campground_name') or a.get('park', '?')} ({a['start']} to {a['end']})"
            for a in existing
        )
        msg = f"You already have {_MAX_USER_ALERTS} active watches (the max).\n{lines}\nUse `/unwatch` to remove one first."
        if interaction_token:
            send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
        return {"error": "limit_reached"}

    try:
        start_date = _date.fromisoformat(start)
        end_date = _date.fromisoformat(end)
    except ValueError:
        msg = f"Invalid date format. Use YYYY-MM-DD (got start=`{start}`, end=`{end}`)."
        if interaction_token:
            send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
        return {"error": "bad_date"}

    kinds = [campsite_kind] if campsite_kind else None

    with CampflareClient() as client:
        cg = client.get_campground(campground_id)
        campground_name = cg.name

        metadata: dict = {
            "workflow": "user_watch",
            "discord_user_id": user_id,
            "park": campground_name,
            "weekdays_only": weekdays_only,
            "weekends_only": weekends_only,
        }
        if campsite:
            metadata["campsite"] = campsite

        alert = client.create_alert(CreateAlertRequest(
            campground_ids=[campground_id],
            parameters=AvailabilityFilter(
                date_ranges=daily_ranges(start_date, end_date, nights, tuple(range(1, 13))),
                status=["available"],
                campsite_kinds=kinds,
            ),
            metadata=metadata,
            webhook_override_url=os.environ.get("CAMPFLARE_WEBHOOK_URL") or None,
        ))
    alert_id = alert.get("id") or alert.get("alert_id") or str(alert)

    entry = {
        "alert_id": alert_id,
        "campground_id": campground_id,
        "campground_name": campground_name,
        "start": start,
        "end": end,
        "nights": nights,
        "campsite_kind": campsite_kind,
        "weekdays_only": weekdays_only,
        "weekends_only": weekends_only,
        "campsite": campsite,
    }
    user_alerts_state[user_id] = existing + [entry]

    # Immediately poll so the user gets a DM right away if anything is already open.
    poll_availability.spawn()

    flags = []
    if weekdays_only:
        flags.append("weekdays only")
    if weekends_only:
        flags.append("weekends only")
    if campsite:
        flags.append(f"site {campsite}")
    flag_str = (", " + ", ".join(flags)) if flags else ""
    msg = (
        f"Watching **{campground_name}** ({start} to {end}, {nights} night{'s' if nights != 1 else ''}{flag_str})."
        "\n\nI'll DM you when something opens. Use `/unwatch` to cancel."
    )
    if interaction_token:
        send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)

    return {"alert_id": alert_id, "campground_name": campground_name}


@app.function(image=endpoint_image, secrets=secrets, timeout=60, retries=0)
def user_watch_cancel(
    user_id: str,
    alert_id: str,
    interaction_token: str | None = None,
) -> dict:
    """Cancel a personal alert and remove it from user state."""
    import os
    from src.campflare import CampflareClient
    from src.discord_interactions import send_followup

    existing = list(user_alerts_state.get(user_id) or [])
    entry = next((a for a in existing if a["alert_id"] == alert_id), None)
    if not entry:
        msg = f"Alert `{alert_id[:8]}` not found in your watches."
        if interaction_token:
            send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
        return {"error": "not_found"}

    try:
        with CampflareClient() as client:
            client.cancel_alert(alert_id)
    except Exception as e:
        # Alert may have already expired — still remove from our state
        print(f"[user_watch_cancel] cancel_alert error (ignored): {e}")

    user_alerts_state[user_id] = [a for a in existing if a["alert_id"] != alert_id]
    display_name = entry.get("campground_name") or entry.get("park", alert_id[:8])
    msg = f"Alert for **{display_name}** (`{alert_id[:8]}`) cancelled."
    if interaction_token:
        send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
    return {"cancelled": alert_id, "campground_name": display_name}


@app.function(image=endpoint_image, secrets=secrets, timeout=60, retries=0)
def user_watch_list(
    user_id: str,
    interaction_token: str | None = None,
) -> dict:
    """List a user's active watches and post them as a followup."""
    import os
    from src.discord_interactions import send_followup

    existing = list(user_alerts_state.get(user_id) or [])
    if not existing:
        msg = "You have no active watches. Use `/watch` to set one up."
    else:
        def _alert_line(a: dict) -> str:
            flags = []
            if a.get("weekdays_only"):
                flags.append("weekdays only")
            if a.get("weekends_only"):
                flags.append("weekends only")
            if a.get("campsite"):
                flags.append(f"site {a['campsite']}")
            flag_str = (" · " + ", ".join(flags)) if flags else ""
            name = a.get("campground_name") or a.get("park", "?")
            return (
                f"• `{a['alert_id'][:8]}` — **{name}** "
                f"({a['start']} to {a['end']}, {a['nights']} night{'s' if a['nights'] != 1 else ''})"
                + flag_str
            )
        lines = "\n".join(_alert_line(a) for a in existing)
        msg = f"**Your active watches ({len(existing)}/{_MAX_USER_ALERTS}):**\n{lines}"

    if interaction_token:
        send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
    return {"n_alerts": len(existing)}


@app.function(image=endpoint_image, secrets=secrets, timeout=120, retries=0, schedule=modal.Cron("*/2 * * * *"))
def poll_availability() -> dict:
    """Poll bulk_availability for all user_watch campgrounds every 2 minutes.

    Compares current availability against the last known state (poll-state Dict)
    and fires handle_alert() for any new openings, which DMs the user directly.
    """
    from src.workflows.poller import run_poll
    result = run_poll(user_alerts_state, notif_cache)
    print(
        f"[poll] active={result['active_alerts']} polled={result['campgrounds_polled']} "
        f"new_openings={result['new_openings']} dm_sent={result['dm_sent']} "
        f"filtered={result['filtered']} watching={result['watching']}"
    )
    return result


# ---------- Public HTTP endpoints ----------

@app.function(image=worker_image, secrets=secrets)
@modal.fastapi_endpoint(method="POST")
def campflare_webhook(payload: dict, authorization: str = Header(None)) -> dict:
    """Public webhook Campflare POSTs to when an availability alert fires.

    Verifies the HS256 JWT signature (secret distributed as base64; HMAC uses
    the decoded bytes — verifying with the raw string yields a misleading
    "Signature verification failed").
    """
    import base64
    import os
    import jwt
    from src.workflows.webhook_handler import handle_alert

    secret_b64 = os.environ.get("CAMPFLARE_JWT_SECRET")
    if not secret_b64:
        raise HTTPException(status_code=500, detail="CAMPFLARE_JWT_SECRET not configured")
    secret_bytes = base64.urlsafe_b64decode(secret_b64 + "==")

    token = authorization or ""
    if token.lower().startswith("bearer "):
        token = token[7:]
    if not token:
        raise HTTPException(status_code=401, detail="missing authorization header")

    try:
        jwt.decode(token, secret_bytes, algorithms=["HS256"])
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"invalid jwt: {e}")

    return handle_alert(payload)


@app.function(image=endpoint_image, secrets=secrets, min_containers=1)
@modal.fastapi_endpoint(method="POST")
async def discord_interactions(
    request: Request,
    x_signature_ed25519: str = Header(None),
    x_signature_timestamp: str = Header(None),
) -> dict:
    """Discord interactions endpoint. Handles PING (1), APPLICATION_COMMAND (2),
    and APPLICATION_COMMAND_AUTOCOMPLETE (4).

    Discord requires a response within 3s. Slow handlers return type 5
    (deferred) and run via .spawn.aio() that PATCHes a followup later.
    """
    import os
    from src.discord_interactions import verify_signature
    from src.workflows.region_finder import REGIONS

    body = await request.body()
    public_key = os.environ.get("DISCORD_PUBLIC_KEY")
    if not public_key:
        raise HTTPException(status_code=500, detail="DISCORD_PUBLIC_KEY not configured")

    if not x_signature_ed25519 or not x_signature_timestamp:
        raise HTTPException(status_code=401, detail="missing signature headers")
    if not verify_signature(public_key, x_signature_ed25519, x_signature_timestamp, body):
        raise HTTPException(status_code=401, detail="invalid request signature")

    interaction = await request.json()
    itype = interaction.get("type")

    # PING handshake.
    if itype == 1:
        return {"type": 1}

    data = interaction.get("data") or {}
    name = data.get("name")

    # Type 4 = APPLICATION_COMMAND_AUTOCOMPLETE. Discord asks "what choices
    # should I show?" for the focused parameter. We respond synchronously
    # with up to 25 choices.
    if itype == 4:
        if name == "refresh":
            options = data.get("options") or []
            focused_value = ""
            for opt in options:
                if opt.get("focused") and opt.get("name") == "region":
                    focused_value = (opt.get("value") or "").lower()
                    break
            choices = [
                {"name": r.display_name, "value": r.name}
                for r in REGIONS.values()
                if focused_value in r.name.lower() or focused_value in r.display_name.lower()
            ][:25]
            return {"type": 8, "data": {"choices": choices}}
        if name == "watch":
            focused_value = ""
            for opt in (data.get("options") or []):
                if opt.get("focused") and opt.get("name") == "campground":
                    focused_value = (opt.get("value") or "").strip()
                    break
            if len(focused_value) < 2:
                return {"type": 8, "data": {"choices": []}}
            try:
                from src.campflare import CampflareClient, CampgroundSearchRequest
                with CampflareClient() as client:
                    results = client.search_campgrounds(CampgroundSearchRequest(query=focused_value, limit=25))
                choices = [{"name": cg.name, "value": cg.id} for cg in results]
            except Exception:
                choices = []
            return {"type": 8, "data": {"choices": choices[:25]}}
        if name == "unwatch":
            user_id = (
                (interaction.get("member") or {}).get("user", {}).get("id")
                or (interaction.get("user") or {}).get("id")
            )
            existing = list(await user_alerts_state.get.aio(user_id) or []) if user_id else []
            focused_value = ""
            for opt in (data.get("options") or []):
                if opt.get("focused") and opt.get("name") == "alert_id":
                    focused_value = (opt.get("value") or "").lower()
                    break
            choices = [
                {
                    "name": f"{a.get('campground_name') or a.get('park', '?')} ({a['start']} to {a['end']})",
                    "value": a["alert_id"],
                }
                for a in existing
                if focused_value in (a.get("campground_name") or a.get("park", "")).lower()
                or focused_value in a["alert_id"].lower()
            ][:25]
            return {"type": 8, "data": {"choices": choices}}
        return {"type": 8, "data": {"choices": []}}

    # Type 2 = APPLICATION_COMMAND.
    if itype == 2:
        token = interaction.get("token")
        if name == "refresh":
            options = {opt["name"]: opt.get("value") for opt in (data.get("options") or [])}
            region_name = options.get("region")
            if not region_name:
                return {"type": 4, "data": {"content": "Missing `region` parameter."}}
            await refresh_region.spawn.aio(region_name=region_name, interaction_token=token)
            return {"type": 5}
        if name == "status":
            await status_report.spawn.aio(interaction_token=token)
            return {"type": 5}
        if name == "dispersed":
            options = {opt["name"]: opt.get("value") for opt in (data.get("options") or [])}
            location = options.get("location")
            if not location:
                return {"type": 4, "data": {"content": "Missing `location` parameter."}}
            radius = float(options.get("radius") or 30.0)
            await dispersed_search.spawn.aio(
                location=location, radius_miles=radius, interaction_token=token,
            )
            return {"type": 5}
        if name == "mn-parks":
            options = {opt["name"]: opt.get("value") for opt in (data.get("options") or [])}
            start = options.get("start")
            if not start:
                return {"type": 4, "data": {"content": "Missing `start` parameter (YYYY-MM-DD)."}}
            nights = int(options.get("nights") or 1)
            await mn_parks_check.spawn.aio(
                start=start, nights=nights, interaction_token=token,
            )
            return {"type": 5}

        if name == "weather":
            options = {opt["name"]: opt.get("value") for opt in (data.get("options") or [])}
            origin = options.get("origin")
            destination = options.get("destination")
            if not origin or not destination:
                return {"type": 4, "data": {"content": "Missing `origin` or `destination`."}}
            await weather_check.spawn.aio(
                origin=origin, destination=destination, interaction_token=token,
            )
            return {"type": 5}

        if name == "watch":
            user_id = (
                (interaction.get("member") or {}).get("user", {}).get("id")
                or (interaction.get("user") or {}).get("id")
            )
            if not user_id:
                return {"type": 4, "data": {"content": "Could not determine your Discord user ID."}}
            options = {opt["name"]: opt.get("value") for opt in (data.get("options") or [])}
            campground_id = options.get("campground")
            start = options.get("start")
            end = options.get("end")
            if not campground_id or not start or not end:
                return {"type": 4, "data": {"content": "Missing required options: `campground`, `start`, `end`."}}
            await user_watch_create.spawn.aio(
                user_id=user_id,
                campground_id=campground_id,
                start=start,
                end=end,
                nights=int(options.get("nights") or 1),
                campsite_kind=options.get("campsite_kind") or None,
                weekdays_only=bool(options.get("weekdays_only")),
                weekends_only=bool(options.get("weekends_only")),
                campsite=options.get("campsite") or None,
                interaction_token=token,
            )
            return {"type": 5}

        if name == "unwatch":
            user_id = (
                (interaction.get("member") or {}).get("user", {}).get("id")
                or (interaction.get("user") or {}).get("id")
            )
            if not user_id:
                return {"type": 4, "data": {"content": "Could not determine your Discord user ID."}}
            options = {opt["name"]: opt.get("value") for opt in (data.get("options") or [])}
            alert_id = options.get("alert_id")
            if not alert_id:
                return {"type": 4, "data": {"content": "Missing `alert_id`."}}
            await user_watch_cancel.spawn.aio(
                user_id=user_id, alert_id=alert_id, interaction_token=token,
            )
            return {"type": 5}

        if name == "my-watches":
            user_id = (
                (interaction.get("member") or {}).get("user", {}).get("id")
                or (interaction.get("user") or {}).get("id")
            )
            if not user_id:
                return {"type": 4, "data": {"content": "Could not determine your Discord user ID."}}
            await user_watch_list.spawn.aio(user_id=user_id, interaction_token=token)
            return {"type": 5}

        return {"type": 4, "data": {"content": f"Unknown command: `{name}`"}}

    return {"type": 4, "data": {"content": f"Unhandled interaction type: {itype}"}}
