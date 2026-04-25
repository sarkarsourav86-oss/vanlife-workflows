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

Formatting is deterministic — no LLM call in the hot path. The Anthropic
free tier is 5 req/min; under bursty webhook volume an LLM-formatted handler
hit RateLimitError, raised, and Campflare retried the same notification
multiple times, producing duplicate Discord messages.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..discord import availability_embed, post_to_discord
from ..starlink_score import get_starlink_score

_SCORE_EMOJI = {"good": "🛰️ Good", "marginal": "🛰️ Marginal", "poor": "🛰️ Poor"}


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

    cg_name = payload.get("campground_name") or "Unknown campground"
    cg_id = payload.get("campground_id")
    campsite = payload.get("campsite_name")
    end = start + timedelta(days=nights)
    dates_str = f"{start.strftime('%b %d')} -> {end.strftime('%b %d, %Y')}"
    park = metadata.get("park")

    summary_parts = [f"{cg_name} has availability"]
    if park:
        summary_parts.append(f"in {park}")
    summary = " ".join(summary_parts) + f" ({nights} night{'s' if nights != 1 else ''})"

    embed = availability_embed(
        campground_name=cg_name,
        dates=dates_str,
        nights=nights,
        booking_url=payload.get("reservation_url"),
        summary=summary,
    )
    if campsite:
        embed["fields"].append({"name": "Site", "value": campsite, "inline": True})
    if park:
        embed["fields"].append({"name": "Park", "value": park, "inline": True})

    # Optional Starlink suitability score. Failures here must not block the alert.
    # Coordinates are looked up from Campflare on first use and cached.
    if cg_id:
        try:
            score = get_starlink_score(
                campground_id=cg_id,
                campground_name=cg_name,
            )
        except Exception:
            score = None
        if score is not None:
            embed["fields"].append({
                "name": "Starlink",
                "value": f"{_SCORE_EMOJI[score.score]} ({score.confidence} confidence)\n{score.reasoning}",
                "inline": False,
            })

    post_to_discord(embeds=[embed])
    return {"status": "posted", "campground": cg_name, "start": start.isoformat(), "nights": nights}
