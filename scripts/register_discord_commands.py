"""One-off script: register slash commands with Discord.

Run this whenever you add/remove/rename commands. Idempotent — Discord
upserts by name. Uses the bot token from .env (DISCORD_BOT_TOKEN), which
never goes to Modal.

Usage:
  python -m scripts.register_discord_commands              # global (slow propagation)
  python -m scripts.register_discord_commands --guild GID  # per-guild (instant)

Find your guild ID: in Discord, enable Developer Mode (Settings → Advanced),
right-click your server icon → Copy Server ID.
"""

from __future__ import annotations

import argparse
import os

import httpx
from dotenv import load_dotenv

COMMANDS = [
    {
        "name": "refresh-mn",
        "description": "Recreate the MN weekday-finder Campflare alert (rotates the slate).",
        "type": 1,
    },
    {
        "name": "refresh-np",
        "description": "Rotate Campflare alerts for all configured National Parks.",
        "type": 1,
    },
    {
        "name": "status",
        "description": "Show all active Campflare alerts and their state.",
        "type": 1,
    },
]


def register(app_id: str, bot_token: str, guild_id: str | None) -> None:
    if guild_id:
        url = f"https://discord.com/api/v10/applications/{app_id}/guilds/{guild_id}/commands"
        scope = f"guild {guild_id}"
    else:
        url = f"https://discord.com/api/v10/applications/{app_id}/commands"
        scope = "global"

    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}

    # PUT bulk-overwrites the whole command list — old commands not in COMMANDS get deleted.
    r = httpx.put(url, headers=headers, json=COMMANDS, timeout=30.0)
    r.raise_for_status()

    registered = r.json()
    print(f"Registered {len(registered)} {scope} commands:")
    for c in registered:
        print(f"  /{c['name']}: {c['description']}")


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--guild", help="Guild ID for instant per-server commands")
    args = parser.parse_args()

    app_id = os.environ["DISCORD_APP_ID"]
    bot_token = os.environ["DISCORD_BOT_TOKEN"]
    register(app_id, bot_token, args.guild)
