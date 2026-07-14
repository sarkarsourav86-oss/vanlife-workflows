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
    {
        "name": "dispersed",
        "description": "Find dispersed camping near a location (lat,lng or place name).",
        "type": 1,
        "options": [
            {
                "name": "location",
                "description": "'lat,lng' (e.g. '47.9,-91.87') or a place name.",
                "type": 3,
                "required": True,
            },
            {
                "name": "radius",
                "description": "Search radius in miles (default 30).",
                "type": 10,  # NUMBER (float)
                "required": False,
            },
        ],
    },
    {
        "name": "mn-parks",
        "description": "One-shot availability check across MN state parks for given dates.",
        "type": 1,
        "options": [
            {
                "name": "start",
                "description": "Start date (YYYY-MM-DD).",
                "type": 3,
                "required": True,
            },
            {
                "name": "nights",
                "description": "Number of nights (default 1).",
                "type": 4,  # INTEGER
                "required": False,
            },
        ],
    },
    {
        "name": "weather",
        "description": "Check wind and weather along a driving route. Alerts if dangerous for a high-roof van.",
        "type": 1,
        "options": [
            {
                "name": "origin",
                "description": "Starting location (city/state or lat,lon).",
                "type": 3,  # STRING
                "required": True,
            },
            {
                "name": "destination",
                "description": "Ending location (city/state or lat,lon).",
                "type": 3,
                "required": True,
            },
        ],
    },
    {
        "name": "watch",
        "description": "Set up a personal campsite availability alert. You'll be DM'd when something opens.",
        "type": 1,
        "options": [
            {
                "name": "campground",
                "description": "Campground to watch — type to search.",
                "type": 3,
                "required": True,
                "autocomplete": True,
            },
            {
                "name": "start",
                "description": "Watch window start date (YYYY-MM-DD).",
                "type": 3,
                "required": True,
            },
            {
                "name": "end",
                "description": "Watch window end date (YYYY-MM-DD).",
                "type": 3,
                "required": True,
            },
            {
                "name": "nights",
                "description": "Minimum consecutive nights required (default 1).",
                "type": 4,  # INTEGER
                "required": False,
            },
            {
                "name": "campsite_kind",
                "description": "Filter by campsite type.",
                "type": 3,
                "required": False,
                "choices": [
                    {"name": "Standard", "value": "standard"},
                    {"name": "RV", "value": "rv"},
                    {"name": "Tent-only", "value": "tent-only"},
                    {"name": "Walk-to", "value": "walk-to"},
                    {"name": "Equestrian", "value": "equestrian"},
                ],
            },
            {
                "name": "weekdays_only",
                "description": "Only alert for Mon-Thu nights (default False).",
                "type": 5,  # BOOLEAN
                "required": False,
            },
            {
                "name": "weekends_only",
                "description": "Only alert for Fri-Sat nights (default False).",
                "type": 5,  # BOOLEAN
                "required": False,
            },
            {
                "name": "campsite",
                "description": "Campsite name(s) to watch, comma-separated (e.g. 'Y1,Y2,Y3'). Leave blank for any site.",
                "type": 3,
                "required": False,
            },
        ],
    },
    {
        "name": "unwatch",
        "description": "Cancel one of your active campsite watches.",
        "type": 1,
        "options": [
            {
                "name": "alert_id",
                "description": "Which alert to cancel (pick from the list).",
                "type": 3,
                "required": True,
                "autocomplete": True,
            },
        ],
    },
    {
        "name": "my-watches",
        "description": "List all your active campsite availability watches.",
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
