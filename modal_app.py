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
    modal.Secret.from_name("campflare"),   # CAMPFLARE_API_KEY, CAMPFLARE_JWT_SECRET, optional CAMPFLARE_WEBHOOK_URL
    modal.Secret.from_name("anthropic"),   # ANTHROPIC_API_KEY
    modal.Secret.from_name("discord"),     # DISCORD_WEBHOOK_URL, DISCORD_PUBLIC_KEY, DISCORD_APP_ID
]

# Unified state Dict: {region_name: alert_id}. Replaces the previous
# per-workflow dicts (mn-weekday-alerts, np-camping-alerts).
region_alerts_state = modal.Dict.from_name("region-alerts", create_if_missing=True)


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


@app.function(image=endpoint_image, secrets=secrets)
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

        return {"type": 4, "data": {"content": f"Unknown command: `{name}`"}}

    return {"type": 4, "data": {"content": f"Unhandled interaction type: {itype}"}}
