"""Post messages to a Discord channel via an Incoming Webhook.

Default routing reads DISCORD_WEBHOOK_URL. Workflows can route to a
different channel (e.g. one-off date watches) by setting metadata that
`pick_webhook_url` recognizes.

Discord webhook docs:
https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks
"""

from __future__ import annotations

import os

import httpx


def pick_webhook_url(metadata: dict | None) -> str:
    """Route a Discord post to the right channel based on alert metadata.

    Specific workflows can have their own channel so the firehose stays
    visually separate from curated streams. Falls back to the default
    DISCORD_WEBHOOK_URL when the workflow-specific URL isn't configured —
    better to post to the wrong channel than to crash.
    """
    metadata = metadata or {}
    workflow = metadata.get("workflow")

    routing = {
        "watch_date": "DISCORD_JUL4_WEBHOOK_URL",
    }
    env_var = routing.get(workflow)
    if env_var:
        url = os.environ.get(env_var)
        if url:
            return url
    return os.environ["DISCORD_WEBHOOK_URL"]


def post_to_discord(
    content: str | None = None,
    *,
    embeds: list[dict] | None = None,
    username: str = "Vanlife Bot",
    webhook_url: str | None = None,
) -> None:
    url = webhook_url or os.environ["DISCORD_WEBHOOK_URL"]
    payload: dict = {"username": username}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    r = httpx.post(url, json=payload, timeout=15.0)
    r.raise_for_status()


def availability_embed(
    *,
    campground_name: str,
    dates: str,
    nights: int,
    booking_url: str | None = None,
    summary: str | None = None,
) -> dict:
    """Build a pre-styled embed for an availability alert."""
    embed: dict = {
        "title": f"🏕️  {campground_name}",
        "color": 0x2ECC71,  # green
        "fields": [
            {"name": "Dates", "value": dates, "inline": True},
            {"name": "Nights", "value": str(nights), "inline": True},
        ],
    }
    if summary:
        embed["description"] = summary
    if booking_url:
        embed["url"] = booking_url
        embed["fields"].append(
            {"name": "Book", "value": f"[Reserve now]({booking_url})", "inline": False}
        )
    return embed


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    post_to_discord("👋 hello from vanlife-workflows — Discord wiring works")
