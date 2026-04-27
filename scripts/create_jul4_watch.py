"""Create Campflare watch alerts for Jul 4 weekend 2026 across MN state parks
+ Black Hills NF.

One-off setup script. Each Campflare alert caps at 12 campground IDs, so we
split across multiple alerts grouped by area. Alerts are idempotent only
through their stored IDs in the `watch-date-alerts` Modal Dict — re-running
this script blindly creates *new* alerts in addition to existing ones.

Run after the weekend with `scripts/cancel_jul4_watch.py` to clean up.

Usage:
  python -m scripts.create_jul4_watch                 # dry-run; lists alerts to create
  python -m scripts.create_jul4_watch --apply         # actually create them
"""

from __future__ import annotations

import argparse
import os
from datetime import date

from dotenv import load_dotenv

from src.campflare import (
    AvailabilityFilter,
    CampflareClient,
    CreateAlertRequest,
    DateRange,
)

# Three date ranges per alert covering the realistic Jul 4 weekend stay
# shapes. Earliest check-in is Friday — Thursday arrivals not workable.
DATE_RANGES = [
    DateRange(starting_date=date(2026, 7, 3), nights=2),  # Fri+Sat, Sun checkout
    DateRange(starting_date=date(2026, 7, 3), nights=3),  # Fri+Sat+Sun, Mon checkout
    DateRange(starting_date=date(2026, 7, 4), nights=2),  # Sat+Sun, Mon checkout
]

# Curated list grouped by area. Each group <= 12 ids (Campflare alert cap).
# Selection criterion: MN state parks within ~5 hrs of Twin Cities + Black
# Hills NF. Skips cabin/group/walk-in/boat-in variants by listing the
# drive-up campground IDs explicitly.
GROUPS: dict[str, list[str]] = {
    "MN North Shore": [
        "gooseberry-falls-campground-minnesotastateparks-990",
        "temperance-river-state-park-campground-minnesotastateparks-755",
        "cascade-river-campground-minnesotastateparks-875",
        "bear-head-lake-campground-minnesotastateparks-810",
        "baptism-river-campground-minnesotastateparks-760",
        "jay-cooke-state-park-campground-minnesotastateparks-884",
        "beatrice-lake-campground-minnesotastateparks-902",
    ],
    "MN Central + South": [
        "interstate-state-park-campground-minnesotastateparks-881",
        "banning-campground-minnesotastateparks-987",
        "wild-river-campground-minnesotastateparks-895",
        "sunrise-campground-minnesotastateparks-802",
        "father-hennepin-state-park-campground-minnesotastateparks-824",
        "sakatah-lake-campground-minnesotastateparks-070",
        "nerstrand-big-woods-state-park-campground-minnesotastateparks-074",
        "flandrau-state-park-rustic-campground-minnesotastateparks-992",
        "ogechie-campground-minnesotastateparks-791",
        "sibley-state-park-campground-minnesotastateparks-776",
        "lake-carlos-state-park-campground-minnesotastateparks-795",
        "lindbergh-campground-minnesotastateparks-988",
    ],
    "MN North + Itasca area": [
        "lake-bemidji-campground-minnesotastateparks-841",
        "lake-ozawindib-minnesotastateparks-025",
        "pine-ridge-campground-minnesotastateparks-839",
        "ladys-slipper-campground-minnesotastateparks-757",
        "blue-mounds-campground-minnesotastateparks-814",
    ],
    "Black Hills NF": [
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
    ],
}

WATCH_LABEL = "Jul 4 2026 weekend"
DICT_NAME = "watch-date-alerts"


def main(apply: bool) -> None:
    load_dotenv()

    print(f"Mode: {'APPLY' if apply else 'DRY RUN'}")
    print(f"Date ranges: {[(r.starting_date.isoformat(), r.nights) for r in DATE_RANGES]}")
    print()

    # Sanity-check: all groups under cap, all ids unique.
    seen: set[str] = set()
    for group, ids in GROUPS.items():
        if len(ids) > 12:
            raise SystemExit(f"Group {group!r} has {len(ids)} ids; Campflare cap is 12")
        for cid in ids:
            if cid in seen:
                raise SystemExit(f"Duplicate id across groups: {cid}")
            seen.add(cid)
    print(f"Validated {len(GROUPS)} groups, {len(seen)} unique campground ids.")
    print()

    if not apply:
        for group, ids in GROUPS.items():
            print(f"{group} ({len(ids)} ids):")
            for cid in ids:
                print(f"  - {cid}")
        print()
        print("Re-run with --apply to actually create alerts.")
        return

    import modal

    state = modal.Dict.from_name(DICT_NAME, create_if_missing=True)
    webhook_url = os.environ.get("CAMPFLARE_WEBHOOK_URL") or None

    new_ids: dict[str, str] = {}
    with CampflareClient() as c:
        for group, ids in GROUPS.items():
            req = CreateAlertRequest(
                parameters=AvailabilityFilter(
                    date_ranges=DATE_RANGES,
                    status=["available"],
                    campsite_kinds=["standard", "rv"],
                ),
                campground_ids=ids,
                metadata={
                    "workflow": "watch_date",
                    "watch_label": WATCH_LABEL,
                    "group": group,
                },
                webhook_override_url=webhook_url,
            )
            alert = c.create_alert(req)
            aid = alert["id"]
            print(f"  {group}: created alert {aid}")
            new_ids[group] = aid
            state[f"{WATCH_LABEL} | {group}"] = aid

    print()
    print(f"Created {len(new_ids)} alerts. Stored in `{DICT_NAME}` Modal Dict.")
    print(f"Run `python -m scripts.cancel_jul4_watch --apply` after Jul 6 to clean up.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually create alerts. Default is dry-run.")
    args = parser.parse_args()
    main(apply=args.apply)
