"""One-off script: register slash commands with Discord.

Run this whenever you add/remove/rename commands. PUT bulk-overwrites the
whole command list, so old commands not in COMMANDS get deleted. Idempotent.

Usage:
  python -m scripts.register_discord_commands              # global (slow propagation)
  python -m scripts.register_discord_commands --guild GID  # per-guild (instant)

Find your guild ID: Discord -> User Settings -> Advanced -> Developer Mode on,
then right-click your server icon -> Copy Server ID.

Note: `/refresh region:<name>` uses Discord's autocomplete. The region
parameter sets `autocomplete: true` here; the actual choice list comes
from the discord_interactions endpoint at runtime (interaction type 4).
"""

from __future__ import annotations

import argparse
import os

import httpx
from dotenv import load_dotenv

COMMANDS = [
    {
        "name": "refresh",
        "description": "Rotate the Campflare alert for one region (cancel previous, create fresh).",
        "type": 1,  # CHAT_INPUT
        "options": [
            {
                "name": "region",
                "description": "Which region to refresh.",
                "type": 3,  # STRING
                "required": True,
                "autocomplete": True,
            },
        ],
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
    r = httpx.put(url, headers=headers, json=COMMANDS, timeout=30.0)
    r.raise_for_status()

    registered = r.json()
    print(f"Registered {len(registered)} {scope} commands:")
    for c in registered:
        opts = c.get("options") or []
        opt_str = ""
        if opts:
            opt_str = " (" + ", ".join(o["name"] for o in opts) + ")"
        print(f"  /{c['name']}{opt_str}: {c['description']}")


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--guild", help="Guild ID for instant per-server commands")
    args = parser.parse_args()

    app_id = os.environ["DISCORD_APP_ID"]
    bot_token = os.environ["DISCORD_BOT_TOKEN"]
    register(app_id, bot_token, args.guild)
