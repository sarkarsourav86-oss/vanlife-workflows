"""Config-driven region finder.

Replaces per-region workflow files (the old mn_weekday_finder.py and
np_camping_finder.py) with one engine. Regions are data: a `Region`
dataclass instance per area we want to watch, all collected in `REGIONS`.
The same `run(region)` function works for every entry.

Public surface:
    REGIONS: dict[str, Region]
    run(region_name, previous_alert_id=None, webhook_override_url=None, dry_run=False) -> str | None
    find_candidates(client, region) -> list[Campground]

State (Modal Dict `region-alerts`): {region_name: alert_id}. One singleton
entry per region. Re-running a region cancels its previous alert before
creating a fresh one.

Adding a region: append a `Region` to REGIONS below. No new file, no new
slash command. `/refresh region:<name>` and `/status` pick it up via
autocomplete and the unified state Dict.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..campflare import (
    Amenity,
    AvailabilityFilter,
    BoundingBox,
    Campground,
    CampflareClient,
    CampgroundSearchRequest,
    CampsiteKind,
    CreateAlertRequest,
    DateRange,
)


@dataclass(frozen=True)
class Region:
    """Per-region config. All fields are data; behavior lives in the engine.

    The window is always "today + window_days, restricted to active_months."
    NP-style "Jun-Sep of current year" emerges naturally from
    active_months=[6,7,8,9] without special-casing.
    """
    name: str                                 # slug; dict key in REGIONS and Modal Dict
    display_name: str                         # human-readable for embeds + /status
    bbox: BoundingBox
    active_months: tuple[int, ...]            # alert only fires for openings whose start month is here
    weekdays_only: bool                       # if True, webhook handler post-filters Mon-Thu nights
    min_nights: int                           # minimum consecutive nights required
    campsite_kinds: tuple[CampsiteKind, ...]  # standard/rv/tent-only/etc
    priority_ids: tuple[str, ...] = ()        # hand-curated order; missing-from-search ids skipped silently
    exclude_name_substrings: tuple[str, ...] = ()  # name-substring filter (lowercase)
    amenities: tuple[Amenity, ...] = ()       # optional; empty = no amenity filter
    window_days: int = 365


# Substrings that disqualify a campground for vanlifers — Camper Cabins,
# walk-in tent sites, group-only listings, etc. Used by Northshore.
VANLIFE_EXCLUDE = (
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


REGIONS: dict[str, Region] = {
    "northshore": Region(
        name="northshore",
        display_name="MN North Shore + BWCAW gateways",
        bbox=BoundingBox(
            min_latitude=46.7, max_latitude=48.1,
            min_longitude=-92.3, max_longitude=-89.5,
        ),
        active_months=(5, 6, 7, 8, 9, 10),
        weekdays_only=True,
        min_nights=1,
        campsite_kinds=("standard", "rv"),
        priority_ids=(
            # State-park flagships
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
            # Ely-area state park
            "vermilion-ridge-campground-minnesotastateparks-040",
            # Superior NF fillers
            "cadotte-lake-189",
            "birch-lake-campground-841",
            "ninemile-lake-campground-958",
            "whiteface-reservoir-188",
            "iron-lake-427",
        ),
        exclude_name_substrings=VANLIFE_EXCLUDE,
    ),
    "glacier": Region(
        name="glacier",
        display_name="Glacier NP",
        bbox=BoundingBox(
            min_latitude=48.2, max_latitude=49.0,
            min_longitude=-114.5, max_longitude=-113.2,
        ),
        active_months=(6, 7, 8, 9),
        weekdays_only=False,
        min_nights=2,
        campsite_kinds=("standard", "rv", "tent-only"),
    ),
    "yellowstone": Region(
        name="yellowstone",
        display_name="Yellowstone NP",
        bbox=BoundingBox(
            min_latitude=44.1, max_latitude=45.1,
            min_longitude=-111.2, max_longitude=-109.8,
        ),
        active_months=(6, 7, 8, 9),
        weekdays_only=False,
        min_nights=2,
        campsite_kinds=("standard", "rv", "tent-only"),
    ),
    "theodore-roosevelt": Region(
        name="theodore-roosevelt",
        display_name="Theodore Roosevelt NP",
        bbox=BoundingBox(
            min_latitude=46.8, max_latitude=47.7,
            min_longitude=-103.8, max_longitude=-103.0,
        ),
        active_months=(6, 7, 8, 9),
        weekdays_only=False,
        min_nights=2,
        campsite_kinds=("standard", "rv", "tent-only"),
    ),
    "badlands": Region(
        name="badlands",
        display_name="Badlands NP",
        bbox=BoundingBox(
            min_latitude=43.5, max_latitude=44.0,
            min_longitude=-102.5, max_longitude=-101.6,
        ),
        active_months=(6, 7, 8, 9),
        weekdays_only=False,
        min_nights=2,
        campsite_kinds=("standard", "rv", "tent-only"),
    ),
    "grand-teton": Region(
        name="grand-teton",
        display_name="Grand Teton NP",
        bbox=BoundingBox(
            min_latitude=43.5, max_latitude=44.1,
            min_longitude=-110.9, max_longitude=-110.4,
        ),
        active_months=(6, 7, 8, 9),
        weekdays_only=False,
        min_nights=2,
        campsite_kinds=("standard", "rv", "tent-only"),
    ),
    # Black Hills NF + Spearfish Canyon (one region — Spearfish Canyon is the
    # northern part of the same forest). Spearfish Canyon's USFS sites
    # (Hanna, Rod and Gun, Timon, Strawberry) are first-come-first-served
    # only — no reservations means nothing for Campflare to watch, so they're
    # silently absent from results. Coverage focuses on the central Black
    # Hills reservable lakes (Pactola, Sheridan, Horsethief, Dalton, Roubaix).
    "black-hills": Region(
        name="black-hills",
        display_name="Black Hills NF (SD)",
        bbox=BoundingBox(
            min_latitude=43.4, max_latitude=44.65,
            min_longitude=-104.10, max_longitude=-103.20,
        ),
        active_months=(5, 6, 7, 8, 9, 10),
        weekdays_only=False,
        min_nights=1,
        campsite_kinds=("standard", "rv", "tent-only"),
        exclude_name_substrings=VANLIFE_EXCLUDE,
        priority_ids=(
            "pactola-reservoir-campground-078",
            "sheridan-lake-300",
            "horsethief-lake-campground-840",
            "dalton-lake-campground-434",
            "roubaix-lake-365",
            "elk-mountain-campground-878",
            "boxelder-forks-campground-184",
            "comanche-park-838",
            "bismark-lake-837",
            "oreville-campground-841",
            "dutchman-839",
            "cottonwood-springs-campground-432",
        ),
    ),
}


def window(region: Region, today: date | None = None) -> tuple[date, date]:
    """Rolling [today, today + window_days]."""
    today = today or date.today()
    return today, today + timedelta(days=region.window_days)


def daily_ranges(
    start: date,
    end: date,
    nights: int,
    months: tuple[int, ...],
) -> list[DateRange]:
    """One DateRange per starting day whose month is in `months`. Trims the
    last `nights - 1` days so a multi-night reservation doesn't bleed past
    `end`.
    """
    ranges: list[DateRange] = []
    last_start = end - timedelta(days=nights - 1)
    d = start
    while d <= last_start:
        if d.month in months:
            ranges.append(DateRange(starting_date=d, nights=nights))
        d += timedelta(days=1)
    return ranges


def _name_passes(cg: Campground, exclude: tuple[str, ...]) -> bool:
    if not exclude:
        return True
    name = (cg.name or "").lower()
    return not any(sub in name for sub in exclude)


def _curate(
    candidates: list[Campground],
    priority_ids: tuple[str, ...],
    exclude: tuple[str, ...],
    limit: int = 12,
) -> list[Campground]:
    """Order by priority_ids, then append survivors, drop name-excluded, cap at limit."""
    by_id = {cg.id: cg for cg in candidates if _name_passes(cg, exclude)}
    ordered: list[Campground] = []
    for cid in priority_ids:
        if cid in by_id:
            ordered.append(by_id.pop(cid))
    ordered.extend(by_id.values())
    return ordered[:limit]


def find_candidates(
    client: CampflareClient,
    region: Region,
    limit: int = 12,
) -> list[Campground]:
    """Search Campflare for the region, filter, prioritize, cap at limit.

    No `availability=...` filter on the search — we want booked-solid sites
    in the alert too, since cancellations are exactly what we want to catch.
    """
    req = CampgroundSearchRequest(
        bbox=region.bbox,
        campsite_kinds=list(region.campsite_kinds),
        amenities=list(region.amenities) if region.amenities else None,
        limit=50,
    )
    raw = client.search_campgrounds(req)
    return _curate(raw, region.priority_ids, region.exclude_name_substrings, limit=limit)


def _create_alert(
    client: CampflareClient,
    region: Region,
    campground_ids: list[str],
    webhook_override_url: str | None,
) -> dict:
    start, end = window(region)
    metadata: dict = {"workflow": "region_finder", "region": region.name}
    if region.weekdays_only:
        metadata["weekdays_only"] = True

    req = CreateAlertRequest(
        parameters=AvailabilityFilter(
            date_ranges=daily_ranges(start, end, region.min_nights, region.active_months),
            status=["available"],
            campsite_kinds=list(region.campsite_kinds),
        ),
        campground_ids=campground_ids[:12],
        metadata=metadata,
        webhook_override_url=webhook_override_url,
    )
    return client.create_alert(req)


def _cancel_previous(client: CampflareClient, alert_id: str) -> None:
    """Best-effort cancel; already-cancelled alerts are silently OK."""
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
    region_name: str,
    previous_alert_id: str | None = None,
    webhook_override_url: str | None = None,
    dry_run: bool = False,
) -> str | None:
    """Rotate the alert for one region. Returns the new alert ID or None."""
    region = REGIONS[region_name]

    with CampflareClient() as client:
        if previous_alert_id:
            print(f"[{region.display_name}] cancelling previous alert {previous_alert_id}:")
            _cancel_previous(client, previous_alert_id)

        candidates = find_candidates(client, region)
        print(f"[{region.display_name}] found {len(candidates)} candidates:")
        for cg in candidates:
            print(f"  - {cg.name} ({cg.id})")

        if dry_run or not candidates:
            return None

        alert = _create_alert(
            client, region,
            campground_ids=[c.id for c in candidates],
            webhook_override_url=webhook_override_url,
        )
        new_id = alert.get("id")
        print(f"[{region.display_name}] alert created: {new_id}")
        return new_id


def main() -> None:
    """Local entry point: dry-run a region by name."""
    import os
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("region", choices=sorted(REGIONS.keys()))
    parser.add_argument("--apply", action="store_true",
                        help="Actually create the alert. Default is dry-run.")
    args = parser.parse_args()

    run(
        region_name=args.region,
        previous_alert_id=None,
        webhook_override_url=os.environ.get("CAMPFLARE_WEBHOOK_URL") or None,
        dry_run=not args.apply,
    )


if __name__ == "__main__":
    main()
