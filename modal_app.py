"""Modal deployment: daily cron + public webhook endpoint.

Deploy: `modal deploy modal_app.py`

Provides:
  - `refresh_mn_alert` — daily cron that (re)creates the MN weekday alert.
  - `np_finder` — manual run-once-and-forget workflow that creates one
    Campflare alert per configured National Park. Re-running rotates the
    alerts (cancel old, create fresh) using a Modal Dict for state.
  - `campflare_webhook` — public HTTPS endpoint for Campflare alerts to POST to.
    After first deploy, Modal prints a URL like
      https://<user>--vanlife-workflows-campflare-webhook.modal.run
    Paste that into Campflare's dashboard OR set CAMPFLARE_WEBHOOK_URL to it
    so the cron registers it on the alert automatically.
"""

from __future__ import annotations

import modal
from fastapi import Header, HTTPException

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
    )
    .add_local_python_source("src")
)

app = modal.App("vanlife-workflows", image=image)

secrets = [
    modal.Secret.from_name("campflare"),   # CAMPFLARE_API_KEY, optional CAMPFLARE_WEBHOOK_URL
    modal.Secret.from_name("anthropic"),   # ANTHROPIC_API_KEY
    modal.Secret.from_name("discord"),     # DISCORD_WEBHOOK_URL
]


@app.function(secrets=secrets, schedule=modal.Cron("0 13 * * *"))  # 13:00 UTC daily
def refresh_mn_alert() -> dict:
    """Re-run the MN weekday finder once a day to pick up new campgrounds."""
    from src.workflows.mn_weekday_finder import main as run_finder
    run_finder(dry_run=False)
    return {"status": "ok"}


# Modal Dict persists between runs. Keyed by park name, value is the alert ID.
np_alerts_state = modal.Dict.from_name("np-camping-alerts", create_if_missing=True)


@app.function(secrets=secrets)
def np_finder(dry_run: bool = False) -> dict:
    """Rotate Campflare alerts for the configured National Parks.

    Cancels every alert ID in the `np-camping-alerts` Modal Dict, then creates
    fresh alerts for each park and writes the new IDs back. Run-once-and-forget
    — Campflare keeps watching for the alerts' full date window without further
    cron support. Re-run only when you want to re-arm or change the park list.

    Invoke: `python -m modal run modal_app.py::np_finder`
    Dry run: `python -m modal run modal_app.py::np_finder --dry-run`
    """
    import os
    from src.workflows.np_camping_finder import run

    previous: dict[str, str] = dict(np_alerts_state.items())
    new_state = run(
        state=previous,
        webhook_override_url=os.environ.get("CAMPFLARE_WEBHOOK_URL") or None,
        dry_run=dry_run,
    )

    if not dry_run:
        # Replace the dict's contents with the fresh state.
        for key in list(np_alerts_state.keys()):
            del np_alerts_state[key]
        for park_name, alert_id in new_state.items():
            np_alerts_state[park_name] = alert_id

    return {"alerts": new_state, "count": len(new_state)}


@app.function(secrets=secrets)
@modal.fastapi_endpoint(method="POST")
def campflare_webhook(payload: dict, authorization: str = Header(None)) -> dict:
    """Public webhook Campflare POSTs to when an availability alert fires.

    Campflare signs every webhook POST with an HS256 JWT carrying
    {iat, notification_id} in the `Authorization` header, using the
    shared secret from our account page. We must verify that signature
    before trusting the payload — without this, anyone who discovers the
    endpoint URL could spoof campground-opening alerts.
    """
    import base64
    import os
    import jwt  # pyjwt
    from src.workflows.webhook_handler import handle_alert

    secret_b64 = os.environ.get("CAMPFLARE_JWT_SECRET")
    if not secret_b64:
        raise HTTPException(status_code=500, detail="CAMPFLARE_JWT_SECRET not configured")

    # Campflare's shared secret is distributed as a base64 string; HMAC uses the
    # decoded bytes. Verifying with the raw string yields "Signature verification
    # failed" even though the fingerprint looks right.
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
