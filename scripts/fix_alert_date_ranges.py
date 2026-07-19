"""One-off: cancel all existing user_watch alerts and recreate them with correct
daily_ranges(). Campflare was silently ignoring ending_date, so every alert
was only watching its single starting_date. This fixes all users in the Modal Dict.

Run: python -m scripts.fix_alert_date_ranges
"""
from __future__ import annotations

import json
from datetime import date

from dotenv import load_dotenv
load_dotenv()

import modal
from src.campflare import (
    AvailabilityFilter, CampflareClient, CreateAlertRequest,
)
from src.workflows.region_finder import daily_ranges

user_alerts_state = modal.Dict.from_name("user-alerts", create_if_missing=False)

ALL_MONTHS = tuple(range(1, 13))


def fix_all() -> None:
    for user_id, alerts in user_alerts_state.items():
        updated = []
        for entry in alerts:
            old_id = entry["alert_id"]
            park = entry["park"]
            start = date.fromisoformat(entry["start"])
            end = date.fromisoformat(entry["end"])
            nights = entry.get("nights", 1)
            campsite_kind = entry.get("campsite_kind")
            weekdays_only = entry.get("weekdays_only", False)
            campground_ids = entry["campground_ids"]

            kinds = [campsite_kind] if campsite_kind else None
            ranges = daily_ranges(start, end, nights, ALL_MONTHS)

            print(f"\n[{user_id[:8]}] {park} ({start} to {end}, {nights}n)")
            print(f"  old alert: {old_id[:8]}  date_ranges: {len(ranges)} days")

            with CampflareClient() as client:
                # Cancel old
                try:
                    client.cancel_alert(old_id)
                    print(f"  cancelled {old_id[:8]}")
                except Exception as e:
                    print(f"  cancel failed (may already be cancelled): {e}")

                # Recreate
                webhook_url = "https://sarkarsourav86--vanlife-workflows-campflare-webhook.modal.run"
                new_alert = client.create_alert(CreateAlertRequest(
                    campground_ids=campground_ids,
                    parameters=AvailabilityFilter(
                        date_ranges=ranges,
                        status=["available"],
                        campsite_kinds=kinds,
                    ),
                    metadata={
                        "workflow": "user_watch",
                        "discord_user_id": user_id,
                        "park": park,
                        "weekdays_only": weekdays_only,
                    },
                    webhook_override_url=webhook_url,
                ))
            new_id = new_alert.get("id") or new_alert.get("alert_id") or str(new_alert)
            print(f"  new alert: {new_id[:8]}")

            updated.append({**entry, "alert_id": new_id})

        user_alerts_state[user_id] = updated
        print(f"\nUpdated Modal Dict for user {user_id[:8]}: {len(updated)} alerts")


if __name__ == "__main__":
    fix_all()
