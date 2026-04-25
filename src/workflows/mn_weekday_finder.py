"""Workflow #1: northern Minnesota weekday campground finder.

What it does:
  1. Searches Campflare for campgrounds inside a bbox over northern MN with
     vanlife-friendly filters (standard + RV sites, water, toilets).
  2. Takes the top N (up to 12, Campflare's alert cap) and creates a single
     Availability Alert covering the next year.
  3. When Campflare fires the webhook, `webhook_handler.handle_alert`
     post-filters for weekday-only Mon-Thu nights (and the configured month
     window) and posts a formatted Discord embed.

Run modes:
  python -m src.workflows.mn_weekday_finder              # rotate alert
  python -m src.workflows.mn_weekday_finder --dry-run    # search only

State (Modal Dict `mn-weekday-alerts`): {"mn_weekday": alert_id}. Singleton —
re-running cancels the previous alert before creating a fresh one. Mirrors
the np_camping_finder pattern.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from ..campflare import (
    AvailabilityFilter,
    BoundingBox,
    CampflareClient,
    CampgroundSearchRequest,
    CreateAlertRequest,
    DateRange,
)

NORTHERN_MN_BBOX = BoundingBox(
    min_latitude=46.5,
    max_latitude=49.4,
    min_longitude=-97.2,
    max_longitude=-89.5,
)

# Months we actually want to camp. Webhook handler post-filters with this list.
# Northern MN in Jan-Mar is mostly closed/frozen; restricting cuts noise.
ACTIVE_MONTHS = [5, 6, 7, 8, 9, 10]
WINDOW_DAYS = 365


def year_window(today: date | None = None) -> tuple[date, date]:
    """Return (today, today + 365 days)."""
    today = today or date.today()
    return today, today + timedelta(days=WINDOW_DAYS)


def daily_ranges(
    start: date,
    end: date,
    nights: int = 1,
    months: list[int] | None = None,
) -> list[DateRange]:
    """One DateRange per starting day. Campflare drops `ending_date` on
    /alert/create and only watches `starting_date`, so we enumerate.

    If `months` is supplied, only days whose month is in the list are emitted —
    cheaper alert, fewer wasted webhooks for off-season nights.
    """
    ranges: list[DateRange] = []
    d = start
    while d <= end:
        if months is None or d.month in months:
            ranges.append(DateRange(starting_date=d, nights=nights))
        d += timedelta(days=1)
    return ranges


def find_candidates(client: CampflareClient, limit: int = 12) -> list:
    """Search for up to `limit` MN campgrounds.

    Don't combine status="open" + kind="established" — they intersect to zero
    against real data. Bbox + amenities + kinds + availability is enough.
    """
    start, end = year_window()
    req = CampgroundSearchRequest(
        bbox=NORTHERN_MN_BBOX,
        campsite_kinds=["standard", "rv"],
        amenities=["toilets", "water"],
        limit=limit,
        availability=AvailabilityFilter(
            date_ranges=[DateRange(starting_date=start, ending_date=end, nights=2)],
            status=["available"],
        ),
    )
    return client.search_campgrounds(req)


def create_weekday_alert(
    client: CampflareClient,
    campground_ids: list[str],
    webhook_override_url: str | None = None,
) -> dict:
    """Create one Campflare Availability Alert for the year window."""
    start, end = year_window()
    req = CreateAlertRequest(
        parameters=AvailabilityFilter(
            date_ranges=daily_ranges(start, end, nights=1, months=ACTIVE_MONTHS),
            status=["available"],
            campsite_kinds=["standard", "rv"],
        ),
        campground_ids=campground_ids[:12],
        metadata={
            "workflow": "mn_weekday_finder",
            "weekdays_only": True,
        },
        webhook_override_url=webhook_override_url,
    )
    return client.create_alert(req)


def cancel_previous_alert(client: CampflareClient, alert_id: str) -> None:
    try:
        got = client.get_alert(alert_id)
        if got.get("canceled_at"):
            print(f"  - {alert_id}: already cancelled")
            return
        client.cancel_alert(alert_id)
        print(f"  - {alert_id}: cancelled")
    except Exception as e:
        print(f"  - {alert_id}: error ({e})")


def run(
    previous_alert_id: str | None = None,
    webhook_override_url: str | None = None,
    dry_run: bool = False,
) -> str | None:
    """Rotate the MN alert. Returns the new alert ID, or None on dry-run/empty."""
    with CampflareClient() as client:
        if previous_alert_id:
            print(f"Cancelling previous alert {previous_alert_id}:")
            cancel_previous_alert(client, previous_alert_id)

        candidates = find_candidates(client)
        print(f"Found {len(candidates)} candidate campgrounds:")
        for cg in candidates:
            print(f"  - {cg.name} ({cg.id})")

        if dry_run or not candidates:
            return None

        alert = create_weekday_alert(
            client,
            campground_ids=[c.id for c in candidates],
            webhook_override_url=webhook_override_url,
        )
        print(f"Alert created: {alert.get('id')}")
        return alert.get("id")


def main(dry_run: bool = False) -> None:
    """Local entry point. No state persistence locally — the Modal-side
    `refresh_mn_alert` function manages the Modal Dict.
    """
    import os
    from dotenv import load_dotenv
    load_dotenv()

    run(
        previous_alert_id=None,
        webhook_override_url=os.environ.get("CAMPFLARE_WEBHOOK_URL") or None,
        dry_run=dry_run,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Search only; don't create an alert.")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
