"""Build a /status report across every Campflare alert we own.

Reads two Modal Dicts (`np-camping-alerts`, `mn-weekday-alerts`), hits
Campflare's GET /alert/{id} for each, and returns a single human-readable
string ready to send to Discord.

Pure function — Modal Dicts are passed in. Lets the caller (modal_app or
a local test) decide how to source state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from ..campflare import CampflareClient


def _fmt_date(raw: str | None) -> str:
    if not raw:
        return "?"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return raw


def _summarize_one(label: str, alert: dict) -> str:
    """One bullet line for one alert."""
    aid = alert.get("id", "?")
    canceled = alert.get("canceled_at")
    state = "CANCELLED" if canceled else "ACTIVE"

    params = alert.get("parameters") or {}
    ranges = params.get("date_ranges") or []
    if ranges:
        starts = sorted(r.get("starting_date") for r in ranges if r.get("starting_date"))
        if starts:
            window = f"{_fmt_date(starts[0])} -> {_fmt_date(starts[-1])}"
        else:
            window = "?"
    else:
        window = "?"

    n_cgs = len(alert.get("campground_ids") or [])
    return f"- **{label}** [{state}] `{aid}` -- {n_cgs} campgrounds, {window}"


def build_status_report(
    np_state: Mapping[str, str],
    mn_state: Mapping[str, str],
) -> str:
    """Return a Discord-friendly multi-line status string."""
    lines: list[str] = ["**Active Campflare alerts**", ""]

    if not np_state and not mn_state:
        return "No tracked alerts. Run `/refresh-mn` or `/refresh-np` to create some."

    with CampflareClient() as client:
        if mn_state:
            lines.append("__MN weekday finder__")
            for label, aid in mn_state.items():
                try:
                    alert = client.get_alert(aid)
                    lines.append(_summarize_one(label, alert))
                except Exception as e:
                    lines.append(f"- **{label}** [ERROR] `{aid}` -- {e}")
            lines.append("")

        if np_state:
            lines.append("__National Parks__")
            for park, aid in np_state.items():
                try:
                    alert = client.get_alert(aid)
                    lines.append(_summarize_one(park, alert))
                except Exception as e:
                    lines.append(f"- **{park}** [ERROR] `{aid}` -- {e}")

    return "\n".join(lines).strip()
