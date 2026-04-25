"""National Park campground availability workflow.

Run-once-and-forget: searches each configured park for campgrounds, creates
one Campflare alert per park, persists the alert IDs in a Modal Dict so the
next run can cancel them before re-creating fresh ones.

Add a park: append a NationalPark entry to PARKS below. Bbox should loosely
enclose the park; Campflare matches campgrounds whose coordinates fall inside.

Run modes:
  python -m modal run modal_app.py::np_finder           # creates/rotates alerts
  python -m src.workflows.np_camping_finder --dry-run   # search only, local

State (Modal Dict `np-camping-alerts`): {park_name: alert_id}. Re-running
cancels every previously-tracked alert before creating new ones — simpler
than reconciling park-by-park, and matches the "rotate the slate" intent.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date

from ..campflare import (
    AvailabilityFilter,
    BoundingBox,
    CampflareClient,
    CampgroundSearchRequest,
    CreateAlertRequest,
    DateRange,
)


@dataclass(frozen=True)
class NationalPark:
    name: str
    bbox: BoundingBox


PARKS: list[NationalPark] = [
    NationalPark(
        name="Glacier",
        bbox=BoundingBox(min_latitude=48.2, max_latitude=49.0,
                         min_longitude=-114.5, max_longitude=-113.2),
    ),
    NationalPark(
        name="Yellowstone",
        bbox=BoundingBox(min_latitude=44.1, max_latitude=45.1,
                         min_longitude=-111.2, max_longitude=-109.8),
    ),
    NationalPark(
        name="Theodore Roosevelt",
        bbox=BoundingBox(min_latitude=46.8, max_latitude=47.7,
                         min_longitude=-103.8, max_longitude=-103.0),
    ),
    NationalPark(
        name="Badlands",
        bbox=BoundingBox(min_latitude=43.5, max_latitude=44.0,
                         min_longitude=-102.5, max_longitude=-101.6),
    ),
    NationalPark(
        name="Grand Teton",
        bbox=BoundingBox(min_latitude=43.5, max_latitude=44.1,
                         min_longitude=-110.9, max_longitude=-110.4),
    ),
]


def summer_window(today: date | None = None) -> tuple[date, date]:
    """Return (start, end) covering this summer or next summer if past August."""
    today = today or date.today()
    year = today.year if today.month <= 8 else today.year + 1
    return date(year, 6, 1), date(year, 8, 31)


def find_park_campgrounds(client: CampflareClient, park: NationalPark, limit: int = 12) -> list:
    """Search for up to `limit` campgrounds within the park's bbox with summer availability."""
    start, end = summer_window()
    req = CampgroundSearchRequest(
        bbox=park.bbox,
        campsite_kinds=["standard", "rv", "tent-only"],
        limit=limit,
        availability=AvailabilityFilter(
            date_ranges=[DateRange(starting_date=start, ending_date=end, nights=1)],
            status=["available"],
        ),
    )
    return client.search_campgrounds(req)


def create_park_alert(
    client: CampflareClient,
    park: NationalPark,
    campground_ids: list[str],
    webhook_override_url: str | None = None,
) -> dict:
    """Create one Campflare alert covering this park's campgrounds."""
    start, end = summer_window()
    req = CreateAlertRequest(
        parameters=AvailabilityFilter(
            date_ranges=[DateRange(starting_date=start, ending_date=end, nights=1)],
            status=["available"],
            campsite_kinds=["standard", "rv", "tent-only"],
        ),
        campground_ids=campground_ids[:12],
        metadata={"workflow": "np_camping_finder", "park": park.name},
        webhook_override_url=webhook_override_url,
    )
    return client.create_alert(req)


def cancel_previous_alerts(client: CampflareClient, alert_ids: list[str]) -> None:
    """Cancel every previously-tracked alert. Already-cancelled ones are ignored."""
    for aid in alert_ids:
        try:
            got = client.get_alert(aid)
            if got.get("canceled_at"):
                print(f"  - {aid}: already cancelled")
                continue
            client.cancel_alert(aid)
            print(f"  - {aid}: cancelled")
        except Exception as e:
            print(f"  - {aid}: error ({e})")


def run(
    state: dict[str, str] | None = None,
    webhook_override_url: str | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """Rotate alerts for every park. Returns the new {park_name: alert_id} mapping.

    `state` is the previous mapping (from Modal Dict or a local file); if provided,
    every alert ID in it is cancelled before new alerts are created. Pass `state={}`
    on a fresh run.
    """
    state = state or {}
    new_state: dict[str, str] = {}

    with CampflareClient() as client:
        if state:
            print(f"Cancelling {len(state)} previously-tracked alerts:")
            cancel_previous_alerts(client, list(state.values()))

        for park in PARKS:
            print(f"\n[{park.name}] searching...")
            campgrounds = find_park_campgrounds(client, park)
            if not campgrounds:
                print(f"  no campgrounds found in bbox; skipping")
                continue
            print(f"  {len(campgrounds)} campgrounds:")
            for cg in campgrounds:
                print(f"    - {cg.name} ({cg.id})")

            if dry_run:
                continue

            alert = create_park_alert(
                client, park,
                campground_ids=[c.id for c in campgrounds],
                webhook_override_url=webhook_override_url,
            )
            new_state[park.name] = alert["id"]
            print(f"  alert created: {alert['id']}")

    return new_state


def main(dry_run: bool = False) -> None:
    """Local entry point. Persists state to a JSON file next to this module."""
    import json
    import os
    from pathlib import Path

    from dotenv import load_dotenv
    load_dotenv()

    state_file = Path(os.environ.get("NP_ALERTS_STATE", "np_alerts_state.json"))
    state: dict[str, str] = {}
    if state_file.exists():
        state = json.loads(state_file.read_text())

    new_state = run(
        state=state,
        webhook_override_url=os.environ.get("CAMPFLARE_WEBHOOK_URL") or None,
        dry_run=dry_run,
    )

    if not dry_run:
        state_file.write_text(json.dumps(new_state, indent=2))
        print(f"\nState written to {state_file}: {len(new_state)} alerts")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Search only; don't create or cancel alerts.")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
