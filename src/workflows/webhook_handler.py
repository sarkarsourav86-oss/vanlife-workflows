"""Handle inbound Availability Alert webhooks from Campflare.

Real webhook payload shape (one notification = one availability):

    {
      "alert_id": "...",
      "notification_id": "...",
      "sent_at": "2026-04-24T23:30:00Z",
      "campground_id": "...",
      "campground_name": "Bear Head Lake State Park",
      "campsite_id": "...",
      "campsite_name": "Site 12",
      "reservation_url": "https://...",
      "date_range": {"starting_date": "2026-07-08", "nights": 2, ...},
      "metadata": {"workflow": "mn_weekday_finder", "weekdays_only": true, ...}
    }

We post-filter for weekday nights (Mon-Thu) when metadata flags it, format
the payload with Haiku, and post a rich embed to Discord.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..alert_formatter import format_alert
from ..discord import availability_embed, post_to_discord


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _has_weekday_night(start: date, nights: int) -> bool:
    """True if any night in [start, start+nights) falls on Mon-Thu (weekday 0-3)."""
    for i in range(max(nights, 1)):
        if (start + timedelta(days=i)).weekday() <= 3:
            return True
    return False


def handle_alert(payload: dict) -> dict:
    """Entry point for webhook POSTs. Returns a small summary for logging."""
    metadata = payload.get("metadata") or {}
    date_range = payload.get("date_range") or {}
    start_raw = date_range.get("starting_date") or date_range.get("start_date")
    if not start_raw:
        return {"status": "skipped", "reason": "no starting_date on date_range"}

    start = _parse_date(start_raw)
    nights = int(date_range.get("nights") or 1)

    if metadata.get("weekdays_only") and not _has_weekday_night(start, nights):
        return {"status": "skipped", "reason": "no weekday nights in window"}

    formatted = format_alert(payload)

    cg_name = payload.get("campground_name") or "Unknown campground"
    campsite = payload.get("campsite_name")
    end = start + timedelta(days=nights)
    dates_str = f"{start.isoformat()} -> {end.isoformat()}"

    embed = availability_embed(
        campground_name=cg_name,
        dates=dates_str,
        nights=nights,
        booking_url=payload.get("reservation_url"),
        summary=formatted.summary,
    )
    if campsite:
        embed["fields"].append({"name": "Site", "value": campsite, "inline": True})
    embed["fields"].append({"name": "Urgency", "value": formatted.urgency, "inline": True})
    if formatted.highlights:
        embed["fields"].append(
            {"name": "Highlights",
             "value": "\n".join(f"- {h}" for h in formatted.highlights),
             "inline": False}
        )

    post_to_discord(embeds=[embed])
    return {"status": "posted", "campground": cg_name, "start": start.isoformat(), "nights": nights}
