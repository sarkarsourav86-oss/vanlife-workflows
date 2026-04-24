"""Handle inbound Availability Alert webhooks from Campflare.

Responsibilities:
  - Parse the JSON payload (shape is tolerant — Campflare may add fields).
  - If metadata flags `weekdays_only`, filter openings to Mon–Thu nights.
  - Ask the LLM formatter for human-readable copy.
  - Post a rich embed to Discord.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..alert_formatter import format_alert
from ..discord import availability_embed, post_to_discord


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _weekday_nights(start: date, end: date) -> list[date]:
    """Return nights (Mon 0 .. Sun 6) in [start, end) that are Mon–Thu (0–3)."""
    nights: list[date] = []
    d = start
    while d < end:
        if d.weekday() <= 3:
            nights.append(d)
        d += timedelta(days=1)
    return nights


def _filter_weekday_only(openings: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for op in openings:
        start = _parse_date(op.get("start_date"))
        end_raw = op.get("end_date")
        end = _parse_date(end_raw) if end_raw else start + timedelta(days=int(op.get("nights", 1)))
        if _weekday_nights(start, end):
            kept.append(op)
    return kept


def handle_alert(payload: dict) -> dict:
    """Entry point for webhook POSTs. Returns a small summary for logging."""
    metadata = payload.get("metadata") or {}
    openings = payload.get("openings") or payload.get("availability") or []

    if metadata.get("weekdays_only"):
        openings = _filter_weekday_only(openings)
        payload = {**payload, "openings": openings}

    if not openings:
        return {"status": "skipped", "reason": "no matching openings after filtering"}

    formatted = format_alert(payload)

    cg = payload.get("campground") or {}
    cg_name = cg.get("name", "Unknown campground")
    first = openings[0]
    dates_str = f"{first.get('start_date')} → {first.get('end_date', '?')}"
    nights = int(first.get("nights", 1))

    embed = availability_embed(
        campground_name=cg_name,
        dates=dates_str,
        nights=nights,
        booking_url=payload.get("booking_url"),
        summary=formatted.summary,
    )
    embed["fields"].append(
        {"name": "Urgency", "value": formatted.urgency, "inline": True}
    )
    if formatted.highlights:
        embed["fields"].append(
            {"name": "Highlights",
             "value": "\n".join(f"• {h}" for h in formatted.highlights),
             "inline": False}
        )

    post_to_discord(embeds=[embed])
    return {"status": "posted", "campground": cg_name, "openings": len(openings)}
