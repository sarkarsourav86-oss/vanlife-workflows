"""One-off migration: copy old per-workflow Modal Dicts into `region-alerts`.

Old layout:
  - `mn-weekday-alerts`: {"mn_weekday": <id>}
  - `np-camping-alerts`: {"Glacier": <id>, "Yellowstone": <id>, ...}

New layout:
  - `region-alerts`: {"northshore": <id>, "glacier": <id>, ...}

The migration is idempotent: re-running is safe. It refuses to overwrite
existing region-alerts entries (so a re-run after partial /refresh activity
doesn't trample fresh state).

Usage:
  python -m scripts.migrate_alert_state              # dry run; prints planned writes
  python -m scripts.migrate_alert_state --apply      # actually write to region-alerts
"""

from __future__ import annotations

import argparse


# Mapping from old-Dict keys to new region-alerts keys.
KEY_MAP = {
    # mn-weekday-alerts -> region-alerts
    ("mn-weekday-alerts", "mn_weekday"): "northshore",

    # np-camping-alerts -> region-alerts
    ("np-camping-alerts", "Glacier"): "glacier",
    ("np-camping-alerts", "Yellowstone"): "yellowstone",
    ("np-camping-alerts", "Theodore Roosevelt"): "theodore-roosevelt",
    ("np-camping-alerts", "Badlands"): "badlands",
    ("np-camping-alerts", "Grand Teton"): "grand-teton",
}


def main(apply: bool) -> None:
    import modal

    target = modal.Dict.from_name("region-alerts", create_if_missing=True)
    sources = {
        "mn-weekday-alerts": modal.Dict.from_name("mn-weekday-alerts", create_if_missing=True),
        "np-camping-alerts": modal.Dict.from_name("np-camping-alerts", create_if_missing=True),
    }

    print(f"Mode: {'APPLY' if apply else 'DRY RUN (no writes)'}")
    print()

    target_existing = dict(target.items())
    print(f"Existing region-alerts: {len(target_existing)} entries")
    for k, v in target_existing.items():
        print(f"  {k}: {v}")
    print()

    plan: list[tuple[str, str, str]] = []  # (new_key, old_key, alert_id)
    for src_name, src in sources.items():
        for old_key, alert_id in src.items():
            new_key = KEY_MAP.get((src_name, old_key))
            if new_key is None:
                print(f"  SKIP unknown {src_name}[{old_key!r}] = {alert_id}")
                continue
            plan.append((new_key, f"{src_name}[{old_key!r}]", alert_id))

    print(f"Planned writes: {len(plan)}")
    skipped = 0
    written = 0
    for new_key, source_label, alert_id in plan:
        if new_key in target_existing:
            print(f"  KEEP region-alerts[{new_key!r}] = {target_existing[new_key]}  (already set; from {source_label}={alert_id} ignored)")
            skipped += 1
            continue
        print(f"  WRITE region-alerts[{new_key!r}] = {alert_id}  (from {source_label})")
        if apply:
            target[new_key] = alert_id
            written += 1

    print()
    print(f"Done. Written: {written}, kept-existing: {skipped}, planned: {len(plan)}")
    print()
    print("After verifying, the old Dicts can be left in place — they're harmless")
    print("once the new code reads exclusively from region-alerts.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Default is dry-run.")
    args = parser.parse_args()
    main(apply=args.apply)
