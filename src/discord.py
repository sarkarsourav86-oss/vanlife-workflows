"""Post messages to a Discord channel via an Incoming Webhook.

Set DISCORD_WEBHOOK_URL in your .env. Supports plain messages and rich embeds.

Discord webhook docs:
https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks
"""

from __future__ import annotations

import os

import httpx


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
