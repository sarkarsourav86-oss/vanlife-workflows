"""Workflow #1: MN North Shore + Ely / BWCAW gateway weekday finder.

What it does:
  1. Searches Campflare for campgrounds in a tight bbox covering the North
     Shore of Lake Superior (Two Harbors -> Grand Portage), Ely, and the
     BWCAW gateway lakes. Filters out cabins, group sites, walk/cart-in,
     remote, and out-of-state results.
  2. Curates a top-12 list with a hand-ranked priority order so iconic
     state-park flagships and BWCAW entry-point campgrounds aren't crowded
     out by less-relevant nearby USFS fillers.
  3. Creates one Campflare Availability Alert for the next year, restricted
     to May-Oct starting days.
  4. Webhook handler post-filters for Mon-Thu nights (`weekdays_only` in
     metadata) and posts a Discord embed.

Why drop the availability filter from the search:
  Iconic state parks (Tettegouche, Split Rock, Cascade River) are booked
  100% solid for the summer the moment release windows open. The whole
  *point* of an alert is to catch cancellations on those sites. Filtering
  the search by `availability=...` excludes them entirely. Watch them all,
  let the alert fire when something opens.

Run modes:
  python -m src.workflows.mn_weekday_finder              # rotate alert
  python -m src.workflows.mn_weekday_finder --dry-run    # search only

State (Modal Dict `mn-weekday-alerts`): {"mn_weekday": alert_id}. Singleton —
re-running cancels the previous alert before creating a fresh one.

NOTE: file is still named mn_weekday_finder.py for now; the bbox and
filters here are Northshore-specific. A future refactor (see memory:
project_region_finder_refactor_deferred) collapses this and
np_camping_finder into a single config-driven engine where this workflow
gets renamed to "northshore" properly.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from ..campflare import (
    AvailabilityFilter,
    BoundingBox,
    Campground,
    CampflareClient,
    CampgroundSearchRequest,
    CreateAlertRequest,
    DateRange,
)

# North Shore of Lake Superior + Ely + BWCAW gateway lakes.
# Two Harbors (~47.0) up to Canadian border (~48.1); Lake Vermilion / Ely on
# the west (~-92.3) over to Grand Portage / Pigeon River (~-89.5).
NORTHSHORE_BBOX = BoundingBox(
    min_latitude=46.7,
    max_latitude=48.1,
    min_longitude=-92.3,
    max_longitude=-89.5,
)

# Months we actually want to camp. Northern MN in Jan-Mar is mostly
# closed/frozen; restricting at alert-creation time cuts noise.
ACTIVE_MONTHS = [5, 6, 7, 8, 9, 10]
WINDOW_DAYS = 365

# Substrings in the Campground name (lowercase) that disqualify a result for a
# vanlifer. The Campflare campsite_kinds taxonomy doesn't separate "Camper
# Cabins" from "Standard" within a state park — the names do.
EXCLUDE_NAME_SUBSTRINGS = (
    "cabin",
    "guest house",
    "group",
    "cart-in",
    "walk-in",
    "walk in",
    "remote",
    "yurt",
    "boat-in",
    "back country",
    "backcountry",
)

# Hand-curated priority order. Earlier entries win the 12 alert slots when
# the search returns more candidates than the alert cap. Iconic state-park
# flagships first (highest cancellation value because they're booked solid),
# then BWCAW gateways, then USFS fillers. Missing-from-live-search entries
# are skipped silently — Campflare's index is incomplete for some parks
# (e.g. Tettegouche and Split Rock only index cabin/cart-in/remote variants
# and the drive-up campgrounds aren't searchable, so they can't appear here).
PRIORITY_IDS = [
    # State park flagships
    "gooseberry-falls-campground-minnesotastateparks-990",
    "temperance-river-state-park-campground-minnesotastateparks-755",
    "cascade-river-campground-minnesotastateparks-875",
    "judge-c-r-magney-campground-minnesotastateparks-840",
    "bear-head-lake-campground-minnesotastateparks-810",
    "baptism-river-campground-minnesotastateparks-760",
    # BWCAW gateways
    "fall-lake-283",
    "fenske-lake-campground-840",
    "south-kawishiwi-river-843",
    "east-bearskin-lake-campground-165",
    "flour-lake-campground-429",
    "sawbill-lake-campground-superior-national-forest-147",
    "crescent-lake-mn-146",
    "mcdougal-lake-campground-957",
    # Lake Vermilion / Ely-area state park
    "vermilion-ridge-campground-minnesotastateparks-040",
    # Superior NF fillers
    "cadotte-lake-189",
    "birch-lake-campground-841",
    "ninemile-lake-campground-958",
    "whiteface-reservoir-188",
    "iron-lake-427",
]


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


def _is_vanlifer_friendly(cg: Campground) -> bool:
    """Drop cabin / group / walk-in / remote listings by name substring."""
    name = (cg.name or "").lower()
    return not any(sub in name for sub in EXCLUDE_NAME_SUBSTRINGS)


def _curate(candidates: list[Campground], limit: int = 12) -> list[Campground]:
    """Return up to `limit` campgrounds, ordered by PRIORITY_IDS first.

    Anything in the search results that isn't in PRIORITY_IDS gets appended
    after the priority list (so we don't silently drop new things Campflare
    surfaces). Drops cabin/group/walk-in by name. Dedup by id is automatic
    because Campflare returns unique ids.
    """
    by_id = {cg.id: cg for cg in candidates if _is_vanlifer_friendly(cg)}
    ordered: list[Campground] = []
    for cid in PRIORITY_IDS:
        if cid in by_id:
            ordered.append(by_id.pop(cid))
    # Append anything left over (non-priority survivors of the name filter).
    ordered.extend(by_id.values())
    return ordered[:limit]


def find_candidates(client: CampflareClient, limit: int = 12) -> list[Campground]:
    """Search the Northshore bbox, drop cabin/group/etc by name, prioritize.

    No `availability=...` filter on the search — we want to watch booked-solid
    iconic sites for cancellations, and an availability filter would exclude
    them. Up to 50 raw results; the curate step trims to `limit`.
    """
    req = CampgroundSearchRequest(
        bbox=NORTHSHORE_BBOX,
        campsite_kinds=["standard", "rv"],
        amenities=["toilets", "water"],
        limit=50,
    )
    return _curate(client.search_campgrounds(req), limit=limit)


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
