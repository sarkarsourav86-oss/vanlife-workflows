"""Workflow #1: northern Minnesota summer weekday campground finder.

What it does:
  1. Searches Campflare for campgrounds inside a bbox over northern Minnesota
     with vanlife-friendly filters (standard + RV sites, water, toilets).
  2. Takes the top N (up to 12, Campflare's alert cap) and creates a single
     Availability Alert covering Jun–Aug of this year and next.
  3. When Campflare fires the webhook, `webhook_handler.handle_alert`
     post-filters for weekday-only windows and posts a formatted Discord
     message via the LLM formatter.

Run modes:
  python -m src.workflows.mn_weekday_finder              # create/refresh alert
  python -m src.workflows.mn_weekday_finder --dry-run    # just print the search
"""

from __future__ import annotations

import argparse
from datetime import date

from ..campflare import (
    AvailabilityFilter,
    BoundingBox,
    CampflareClient,
    CampgroundSearchRequest,
    CreateAlertRequest,
)

# Rough bbox over northern MN: from ~Brainerd up to the Canadian border,
# from the Red River east to Lake Superior. Tune to taste.
NORTHERN_MN_BBOX = BoundingBox(
    min_latitude=46.5,
    max_latitude=49.4,
    min_longitude=-97.2,
    max_longitude=-89.5,
)


def summer_window(today: date | None = None) -> tuple[date, date]:
    """Return (start, end) covering this summer or next summer if we're past Aug."""
    today = today or date.today()
    year = today.year if today.month <= 8 else today.year + 1
    return date(year, 6, 1), date(year, 8, 31)


def find_candidates(client: CampflareClient, limit: int = 12) -> list:
    """Search for up to `limit` MN campgrounds that suit weekday getaways."""
    start, end = summer_window()
    req = CampgroundSearchRequest(
        bbox=NORTHERN_MN_BBOX,
        campsite_kinds=["standard", "rv"],
        amenities=["toilets", "water"],
        status="open",
        kind="established",
        limit=limit,
        availability=AvailabilityFilter(
            start_date=start,
            end_date=end,
            nights=2,
            status=["available"],
        ),
    )
    return client.search_campgrounds(req)


def create_weekday_alert(
    client: CampflareClient,
    campground_ids: list[str],
    webhook_override_url: str | None = None,
) -> dict:
    """Create a Campflare Availability Alert for the summer window.

    Note: Campflare alerts don't support weekday-only filtering server-side,
    so we post-filter in webhook_handler. The alert runs until cancelled or
    until the end date passes.
    """
    start, end = summer_window()
    req = CreateAlertRequest(
        parameters=AvailabilityFilter(
            start_date=start,
            end_date=end,
            nights=1,
            status=["available"],
            campsite_kinds=["standard", "rv"],
        ),
        campground_ids=campground_ids[:12],
        metadata={"workflow": "mn_weekday_finder", "weekdays_only": True},
        webhook_override_url=webhook_override_url,
    )
    return client.create_alert(req)


def main(dry_run: bool = False) -> None:
    from dotenv import load_dotenv
    import os
    load_dotenv()

    with CampflareClient() as client:
        candidates = find_candidates(client)
        print(f"Found {len(candidates)} candidate campgrounds:")
        for cg in candidates:
            print(f"  - {cg.name} ({cg.id})")

        if dry_run:
            return

        if not candidates:
            print("No candidates; skipping alert creation.")
            return

        alert = create_weekday_alert(
            client,
            campground_ids=[c.id for c in candidates],
            webhook_override_url=os.environ.get("CAMPFLARE_WEBHOOK_URL") or None,
        )
        print(f"Alert created: {alert}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Search only; don't create an alert.")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
