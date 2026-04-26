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

import time
from datetime import date, datetime, timedelta
from typing import Any

from ..discord import availability_embed, post_to_discord
from ..site_photo import get_site_info
from ..starlink_score import get_starlink_score
from .region_finder import REGIONS

_SCORE_EMOJI = {"good": "🛰️ Good", "marginal": "🛰️ Marginal", "poor": "🛰️ Poor"}


def _record_seen_alert(alert_id: str, metadata: dict) -> None:
    """Log an alert_id to the `seen-alert-ids` Modal Dict the first time we see it.

    Campflare has no list-alerts API, so this is the only way to discover orphan
    alert IDs we've lost track of. Failures here must never block the webhook.
    """
    try:
        import modal
        seen = modal.Dict.from_name("seen-alert-ids", create_if_missing=True)
        if alert_id not in seen:
            seen[alert_id] = {
                "first_seen": time.time(),
                "workflow": metadata.get("workflow"),
                "park": metadata.get("park"),
            }
    except Exception:
        pass


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
    aid = payload.get("alert_id")
    if aid:
        _record_seen_alert(aid, metadata)
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
    dates_str = f"{start.strftime('%a %b %d')} -> {end.strftime('%a %b %d, %Y')}"

    # Resolve a human-readable region label. New alerts (post-refactor) set
    # `metadata.region` to a slug; old alerts (pre-refactor np_camping_finder)
    # set `metadata.park` to a display name. Support both.
    region_label: str | None = None
    region_slug = metadata.get("region")
    if region_slug and region_slug in REGIONS:
        region_label = REGIONS[region_slug].display_name
    elif region_slug:
        region_label = region_slug
    elif metadata.get("park"):
        region_label = metadata["park"]

    summary_parts = [f"{cg_name} has availability"]
    if region_label:
        summary_parts.append(f"in {region_label}")
    summary = " ".join(summary_parts) + f" ({nights} night{'s' if nights != 1 else ''})"

    embed = availability_embed(
        campground_name=cg_name,
        dates=dates_str,
        nights=nights,
        booking_url=payload.get("reservation_url"),
        summary=summary,
    )
    # Promote region to the embed title so it's visible at a glance.
    if region_label:
        embed["title"] = f"🏕️  {region_label}: {cg_name}"
    if campsite:
        embed["fields"].append({"name": "Site", "value": campsite, "inline": True})
    if region_label:
        embed["fields"].append({"name": "Region", "value": region_label, "inline": True})

    # Optional recreation.gov site info: hero photo + shade attribute. Augments
    # the embed without replacing Starlink scoring (different signals — photo
    # shows the pad, shade is rec.gov's own metadata, Starlink scoring is
    # satellite-derived sky view). Returns None for non-recreation.gov listings
    # (state parks etc.) and for any HTTP/parse failure — silently skipped.
    reservation_url = payload.get("reservation_url") or ""
    try:
        site_info = get_site_info(reservation_url, campsite)
    except Exception:
        site_info = None
    if site_info is not None:
        if site_info.photo_url:
            embed["image"] = {"url": site_info.photo_url}
        if site_info.shade:
            embed["fields"].append({"name": "Shade", "value": site_info.shade, "inline": True})

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

    # Footer carries truncated notification + alert IDs so duplicates can be
    # diagnosed at a glance: same notification_id => Campflare retried; different
    # notification_ids on similar alerts => orphan alerts firing in parallel.
    notif_id = payload.get("notification_id") or ""
    footer_parts: list[str] = []
    if notif_id:
        footer_parts.append(f"notif: {notif_id[:8]}")
    if aid:
        footer_parts.append(f"alert: {aid[:8]}")
    if footer_parts:
        embed["footer"] = {"text": " • ".join(footer_parts)}

    post_to_discord(embeds=[embed])
    return {"status": "posted", "campground": cg_name, "start": start.isoformat(), "nights": nights}
