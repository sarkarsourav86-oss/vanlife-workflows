"""Build a /status report across every Campflare alert we own.

Reads the unified `region-alerts` Modal Dict ({region_name: alert_id}),
hits Campflare's GET /alert/{id} for each, and returns a Discord-friendly
multi-line string.

Pure function — the state dict is passed in. Lets the caller (modal_app
or a local test) decide how to source state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from ..campflare import CampflareClient
from .region_finder import REGIONS


def _fmt_date(raw: str | None) -> str:
    if not raw:
        return "?"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return raw


def _summarize_one(label: str, alert: dict) -> str:
    aid = alert.get("id", "?")
    canceled = alert.get("canceled_at")
    state = "CANCELLED" if canceled else "ACTIVE"

    params = alert.get("parameters") or {}
    ranges = params.get("date_ranges") or []
    starts = sorted(r.get("starting_date") for r in ranges if r.get("starting_date"))
    if starts:
        window_str = f"{_fmt_date(starts[0])} -> {_fmt_date(starts[-1])}"
    else:
        window_str = "?"

    n_cgs = len(alert.get("campground_ids") or [])
    return f"- **{label}** [{state}] `{aid}` -- {n_cgs} campgrounds, {window_str}"


def build_status_report(state: Mapping[str, str]) -> str:
    """Return a Discord-friendly status string for every region in `state`.

    Region names not in REGIONS still print (so orphan keys are visible)
    but use the raw key as the label.
    """
    if not state:
        return "No tracked alerts. Run `/refresh region:<name>` to create one."

    lines: list[str] = ["**Active Campflare alerts**", ""]

    with CampflareClient() as client:
        for region_name, aid in state.items():
            label = REGIONS[region_name].display_name if region_name in REGIONS else region_name
            try:
                alert = client.get_alert(aid)
                lines.append(_summarize_one(label, alert))
            except Exception as e:
                lines.append(f"- **{label}** [ERROR] `{aid}` -- {e}")

    return "\n".join(lines).strip()
