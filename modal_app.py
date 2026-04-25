"""Modal deployment: webhook endpoint + Discord interactions endpoint.

Deploy: `modal deploy modal_app.py`
Force fresh containers: `modal deploy modal_app.py --strategy recreate`

Provides:
  - `campflare_webhook` — public HTTPS endpoint for Campflare alert webhooks.
  - `discord_interactions` — public HTTPS endpoint for Discord slash commands.
  - `refresh_mn`, `refresh_np`, `status_report` — work functions invoked by
    Discord interactions via Modal's .spawn() to stay under the 3s reply limit.

After first deploy, paste the discord_interactions URL into the
"Interactions Endpoint URL" box on the Discord developer portal
(General Information tab). Discord will PING; we PONG; you save.
"""

from __future__ import annotations

import modal
from fastapi import Header, HTTPException, Request

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "httpx>=0.27",
        "pydantic>=2.7",
        "anthropic>=0.40",
        "langchain>=0.3",
        "langchain-anthropic>=0.3",
        "python-dotenv>=1.0",
        "fastapi>=0.115",
        "pyjwt>=2.9",
        "pillow>=10",
        "pynacl>=1.5",
    )
    .add_local_python_source("src")
)

app = modal.App("vanlife-workflows", image=image)

secrets = [
    modal.Secret.from_name("campflare"),   # CAMPFLARE_API_KEY, CAMPFLARE_JWT_SECRET, optional CAMPFLARE_WEBHOOK_URL
    modal.Secret.from_name("anthropic"),   # ANTHROPIC_API_KEY
    modal.Secret.from_name("discord"),     # DISCORD_WEBHOOK_URL, DISCORD_PUBLIC_KEY, DISCORD_APP_ID
]

# Modal Dicts: persisted alert-ID state, one per workflow.
np_alerts_state = modal.Dict.from_name("np-camping-alerts", create_if_missing=True)
mn_alerts_state = modal.Dict.from_name("mn-weekday-alerts", create_if_missing=True)


# ---------- Work functions (called via .spawn() from interaction handler) ----------

@app.function(secrets=secrets, timeout=600)
def refresh_mn(interaction_token: str | None = None) -> dict:
    """Rotate the MN weekday-finder alert. PATCHes Discord followup if token given."""
    import os
    from src.workflows.mn_weekday_finder import run

    previous = mn_alerts_state.get("mn_weekday")
    new_id = run(
        previous_alert_id=previous,
        webhook_override_url=os.environ.get("CAMPFLARE_WEBHOOK_URL") or None,
        dry_run=False,
    )

    if new_id:
        mn_alerts_state["mn_weekday"] = new_id
        msg = f"MN weekday alert refreshed: `{new_id}`"
    elif previous:
        # Cancelled but no candidates found.
        try:
            del mn_alerts_state["mn_weekday"]
        except KeyError:
            pass
        msg = "MN weekday alert cancelled. No candidates found this run."
    else:
        msg = "No candidates found and no previous alert to cancel."

    if interaction_token:
        from src.discord_interactions import send_followup
        send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
    return {"alert_id": new_id, "message": msg}


@app.function(secrets=secrets, timeout=600)
def refresh_np(interaction_token: str | None = None) -> dict:
    """Rotate Campflare alerts for every configured National Park."""
    import os
    from src.workflows.np_camping_finder import run

    previous: dict[str, str] = dict(np_alerts_state.items())
    new_state = run(
        state=previous,
        webhook_override_url=os.environ.get("CAMPFLARE_WEBHOOK_URL") or None,
        dry_run=False,
    )

    for key in list(np_alerts_state.keys()):
        del np_alerts_state[key]
    for park_name, alert_id in new_state.items():
        np_alerts_state[park_name] = alert_id

    msg_lines = [f"Rotated {len(new_state)} National Park alerts:"]
    for park, aid in new_state.items():
        msg_lines.append(f"- **{park}**: `{aid}`")
    msg = "\n".join(msg_lines)

    if interaction_token:
        from src.discord_interactions import send_followup
        send_followup(os.environ["DISCORD_APP_ID"], interaction_token, msg)
    return {"alerts": new_state, "count": len(new_state)}


@app.function(secrets=secrets, timeout=120)
def status_report(interaction_token: str | None = None) -> dict:
    """Build and post a status report on every tracked alert."""
    import os
    from src.workflows.status import build_status_report

    report = build_status_report(
        np_state=dict(np_alerts_state.items()),
        mn_state=dict(mn_alerts_state.items()),
    )

    if interaction_token:
        from src.discord_interactions import send_followup
        send_followup(os.environ["DISCORD_APP_ID"], interaction_token, report)
    return {"report": report}


# ---------- Public HTTP endpoints ----------

@app.function(secrets=secrets)
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


@app.function(secrets=secrets)
@modal.fastapi_endpoint(method="POST")
async def discord_interactions(
    request: Request,
    x_signature_ed25519: str = Header(None),
    x_signature_timestamp: str = Header(None),
) -> dict:
    """Public endpoint Discord posts every slash-command invocation to.

    Discord requires a response within 3s. We verify the Ed25519 signature,
    PONG to the type-1 PING handshake, and for type-2 application commands
    return a deferred response (type 5) immediately while Modal's .spawn()
    runs the actual work and PATCHes the followup.
    """
    import os
    from src.discord_interactions import verify_signature

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

    # PING handshake — Discord pings the URL when you save it in the portal,
    # and periodically thereafter. PONG is type 1.
    if itype == 1:
        return {"type": 1}

    # Type 2 = APPLICATION_COMMAND
    if itype == 2:
        name = (interaction.get("data") or {}).get("name")
        token = interaction.get("token")

        # Type 5 = DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE. No `data` allowed —
        # the actual content comes from the followup PATCH on @original.
        if name == "refresh-mn":
            await refresh_mn.spawn.aio(interaction_token=token)
            return {"type": 5}
        if name == "refresh-np":
            await refresh_np.spawn.aio(interaction_token=token)
            return {"type": 5}
        if name == "status":
            await status_report.spawn.aio(interaction_token=token)
            return {"type": 5}

        return {"type": 4, "data": {"content": f"Unknown command: `{name}`"}}

    return {"type": 4, "data": {"content": f"Unhandled interaction type: {itype}"}}
