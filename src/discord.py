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


def _open_dm_channel(bot_token: str, user_id: str) -> str:
    """Return the DM channel ID for a user, opening it if needed."""
    r = httpx.post(
        "https://discord.com/api/v10/users/@me/channels",
        headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
        json={"recipient_id": user_id},
        timeout=10.0,
    )
    r.raise_for_status()
    return r.json()["id"]


def send_dm(
    user_id: str,
    content: str | None = None,
    *,
    embeds: list[dict] | None = None,
    attachment: bytes | None = None,
    attachment_filename: str = "image.jpg",
) -> None:
    """Send a Discord DM to a user by their Discord user ID.

    Opens (or reuses) a DM channel via the bot API, then posts the message.
    Requires DISCORD_BOT_TOKEN in the environment.

    If `attachment` bytes are provided, the file is uploaded alongside the
    message. The embed should reference it as ``attachment://<filename>``.
    """
    import json as _json

    bot_token = os.environ["DISCORD_BOT_TOKEN"]
    channel_id = _open_dm_channel(bot_token, user_id)

    payload: dict = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    if attachment is not None:
        msg_resp = httpx.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {bot_token}"},
            files={"file": (attachment_filename, attachment, "image/jpeg")},
            data={"payload_json": _json.dumps(payload)},
            timeout=20.0,
        )
    else:
        msg_resp = httpx.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=15.0,
        )
    msg_resp.raise_for_status()


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
