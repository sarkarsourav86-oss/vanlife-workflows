"""Modal deployment: daily cron + public webhook endpoint.

Deploy: `modal deploy modal_app.py`

Provides:
  - `refresh_mn_alert` — daily cron that (re)creates the MN weekday alert.
  - `campflare_webhook` — public HTTPS endpoint for Campflare alerts to POST to.
    After first deploy, Modal prints a URL like
      https://<user>--vanlife-workflows-campflare-webhook.modal.run
    Paste that into Campflare's dashboard OR set CAMPFLARE_WEBHOOK_URL to it
    so the cron registers it on the alert automatically.
"""

from __future__ import annotations

import modal

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


@app.function(secrets=secrets)
@modal.fastapi_endpoint(method="POST")
def campflare_webhook(payload: dict) -> dict:
    """Public webhook Campflare POSTs to when an availability alert fires."""
    from src.workflows.webhook_handler import handle_alert
    return handle_alert(payload)
