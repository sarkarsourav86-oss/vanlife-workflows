"""Availability poller for user_watch alerts.

Supplements Campflare's webhook by actively polling bulk_availability every N
minutes. Deduplication is handled via a `notification-cache` Modal Dict keyed
by `{alert_id}|{site_id}|{date}` — so neither the poller nor the Campflare
webhook can DM the user twice for the same opening on the same day.

Public surface:
    poll_user_alerts(user_alerts) -> notifications
        Core logic — pure function, no Modal or Discord imports.

    run_poll(user_alerts_state, notif_cache) -> dict
        Orchestration: reads state, calls poll_user_alerts, fires handle_alert
        (skipping already-cached entries), writes new cache entries, prunes stale
        ones. Called by Modal cron and on alert creation.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..campflare import CampflareClient


def _date_range_for_alert(entry: dict) -> tuple[date, date]:
    start = date.fromisoformat(entry["start"])
    end = date.fromisoformat(entry["end"])
    return start, end


def poll_user_alerts(
    user_alerts: dict[str, list[dict]],
) -> list[dict]:
    """Poll bulk_availability for all active user alerts.

    Returns a list of webhook-shaped notification dicts ready for handle_alert().
    No dedup is applied here — that happens in run_poll via notif_cache.
    """
    today = date.today()

    cg_to_alerts: dict[str, list[tuple[str, dict]]] = {}
    for user_id, alerts in user_alerts.items():
        for entry in alerts:
            cg_ids = entry.get("campground_ids") or (
                [entry["campground_id"]] if entry.get("campground_id") else []
            )
            for cg_id in cg_ids:
                cg_to_alerts.setdefault(cg_id, []).append((user_id, entry))

    if not cg_to_alerts:
        return []

    all_cg_ids = list(cg_to_alerts.keys())

    all_starts, all_ends = [], []
    for alerts in user_alerts.values():
        for entry in alerts:
            s, e = _date_range_for_alert(entry)
            all_starts.append(s)
            all_ends.append(e)

    window_start = min(all_starts, default=today)
    window_end = min(max(all_ends, default=today), today + timedelta(days=365))

    site_name_map: dict[str, dict[str, str]] = {}
    reservation_url_map: dict[str, str | None] = {}
    availability: dict[str, dict] = {}

    with CampflareClient() as client:
        for cg_id in all_cg_ids:
            try:
                cg = client.get_campground(cg_id)
                reservation_url_map[cg_id] = cg.reservation_url
            except Exception:
                reservation_url_map[cg_id] = None
            try:
                sites = client.get_campsites(cg_id)
                site_name_map[cg_id] = {s.id: s.name for s in sites}
            except Exception:
                site_name_map[cg_id] = {}

        for i in range(0, len(all_cg_ids), 25):
            chunk = all_cg_ids[i:i + 25]
            result = client.bulk_availability(chunk, window_start, window_end)
            for cg_entry in result.get("campgrounds") or []:
                cg_id = cg_entry.get("campground_id") or cg_entry.get("id")
                if not cg_id:
                    continue
                uuid_to_name = site_name_map.get(cg_id, {})
                site_map: dict[str, dict] = {}
                for site in cg_entry.get("campsite_availability") or []:
                    site_uuid = site.get("campsite_id") or site.get("id")
                    if not site_uuid:
                        continue
                    site_name = uuid_to_name.get(site_uuid, site_uuid)
                    site_map[site_name] = site.get("availability") or {}
                availability[cg_id] = site_map

    notifications: list[dict] = []

    for cg_id, site_map in availability.items():
        for site_id, date_statuses in site_map.items():
            for date_str, status in date_statuses.items():
                if status != "available":
                    continue

                for user_id, entry in cg_to_alerts.get(cg_id, []):
                    entry_start, entry_end = _date_range_for_alert(entry)
                    opening_date = date.fromisoformat(date_str)
                    if not (entry_start <= opening_date <= entry_end):
                        continue

                    nights = entry.get("nights", 1)
                    if nights > 1:
                        consecutive = all(
                            date_statuses.get(
                                (opening_date + timedelta(days=i)).isoformat()
                            ) == "available"
                            for i in range(1, nights)
                        )
                        if not consecutive:
                            continue

                    alert_id = entry.get("alert_id")
                    notifications.append({
                        "alert_id": alert_id,
                        "notification_id": f"poll-{cg_id}|{site_id}|{date_str}",
                        "campground_id": cg_id,
                        "campground_name": entry.get("campground_name") or entry.get("park", cg_id),
                        "campsite_name": site_id,
                        "reservation_url": reservation_url_map.get(cg_id),
                        "date_range": {
                            "starting_date": date_str,
                            "nights": nights,
                        },
                        "metadata": {
                            "workflow": "user_watch",
                            "discord_user_id": user_id,
                            "park": entry.get("campground_name") or entry.get("park"),
                            "weekdays_only": entry.get("weekdays_only", False),
                            "weekends_only": entry.get("weekends_only", False),
                            "campsite": entry.get("campsite"),
                        },
                    })

    return notifications


def _cache_key(alert_id: str, site_id: str, date_str: str) -> str:
    return f"{alert_id}|{site_id}|{date_str}"


def run_poll(user_alerts_state: Any, notif_cache: Any) -> dict:
    """Orchestration layer: poll, dedup via notif_cache, fire handle_alert."""
    from .webhook_handler import handle_alert

    today = date.today().isoformat()
    user_alerts = dict(user_alerts_state.items())

    # Build set of cache keys that already fired today for fast lookup.
    cached_today = {k for k, v in notif_cache.items() if v == today}

    notifications = poll_user_alerts(user_alerts)

    active_count = sum(len(a) for a in user_alerts.values())
    cg_names = sorted({
        entry.get("campground_name") or entry.get("park", "?")
        for alerts in user_alerts.values()
        for entry in alerts
    })

    print(f"[poll] checking {active_count} alerts across: {', '.join(cg_names)}")

    dm_sent = 0
    filtered = 0
    deduped = 0
    results = []

    for payload in notifications:
        alert_id = payload["alert_id"]
        site_id = payload["campsite_name"]
        date_str = payload["date_range"]["starting_date"]
        key = _cache_key(alert_id, site_id, date_str)

        if key in cached_today:
            deduped += 1
            continue

        try:
            result = handle_alert(payload)
            results.append(result)
            status = result.get("status")
            if status == "dm_sent":
                dm_sent += 1
                notif_cache[key] = today
                print(f"[poll] DM sent: {payload['campground_name']} site={site_id} date={date_str}")
            elif status == "skipped":
                filtered += 1
                print(f"[poll] filtered: {payload['campground_name']} site={site_id} date={date_str} reason={result.get('reason')}")
        except Exception as e:
            results.append({"status": "error", "error": str(e)})
            print(f"[poll] error: {e}")

    # Prune cache entries from previous days.
    for k, v in list(notif_cache.items()):
        if v != today:
            try:
                del notif_cache[k]
            except Exception:
                pass

    return {
        "active_alerts": active_count,
        "campgrounds_polled": len({
            cg_id
            for alerts in user_alerts.values()
            for entry in alerts
            for cg_id in (
                entry.get("campground_ids") or (
                    [entry["campground_id"]] if entry.get("campground_id") else []
                )
            )
        }),
        "new_openings": len(notifications),
        "dm_sent": dm_sent,
        "filtered": filtered,
        "deduped": deduped,
        "watching": ", ".join(cg_names),
        "notifications": results,
    }
