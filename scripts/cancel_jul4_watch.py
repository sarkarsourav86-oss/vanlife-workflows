"""Cancel every Campflare alert in the `watch-date-alerts` Modal Dict.

Run after Jul 4 weekend (or anytime you want to stop the watch). Pairs
with `scripts/create_jul4_watch.py`. Idempotent — already-cancelled alerts
are skipped silently.

Usage:
  python -m scripts.cancel_jul4_watch                # dry-run
  python -m scripts.cancel_jul4_watch --apply        # actually cancel
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from src.campflare import CampflareClient

DICT_NAME = "watch-date-alerts"


def main(apply: bool) -> None:
    load_dotenv()
    import modal

    state = modal.Dict.from_name(DICT_NAME, create_if_missing=True)
    items = list(state.items())
    if not items:
        print(f"No entries in `{DICT_NAME}`. Nothing to cancel.")
        return

    print(f"Mode: {'APPLY' if apply else 'DRY RUN'}")
    print(f"Found {len(items)} watch alerts:")
    print()

    with CampflareClient() as c:
        for label, aid in items:
            try:
                a = c.get_alert(aid)
            except Exception as e:
                print(f"  {label} | {aid}: GET error -> {e}")
                continue

            already = bool(a.get("canceled_at"))
            tag = "ALREADY CANCELLED" if already else "ACTIVE"
            print(f"  {label} | {aid}: {tag}")

            if apply and not already:
                try:
                    c.cancel_alert(aid)
                    print(f"    -> cancelled")
                except Exception as e:
                    print(f"    -> CANCEL ERROR: {e}")

    if apply:
        for label in list(state.keys()):
            del state[label]
        print()
        print(f"Cleared `{DICT_NAME}` Modal Dict.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually cancel. Default is dry-run.")
    args = parser.parse_args()
    main(apply=args.apply)
