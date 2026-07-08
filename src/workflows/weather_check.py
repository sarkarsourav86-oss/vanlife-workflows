"""Weather check workflow: route forecast + storm-wait monitor.

Public surface:
    check_route(origin, destination) -> RouteCheckResult
    build_weather_embed(result) -> dict               # Discord embed
    start_weather_watch(lat, lon, label, post_fn)     # blocks until clear, calls post_fn with embed

CLI:
    python -m src.workflows.weather_check "Duluth MN" "Glacier NP"
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..weather import HourlyCondition, _SEVERE_WMO, geocode, get_forecast, get_route_waypoints, reverse_geocode

WIND_DANGER_MPH = 30.0
GUST_WARN_MPH = 40.0
WATCH_POLL_SECONDS = 1800   # 30-min polling interval in weather_watch


@dataclass
class DangerousSegment:
    lat: float
    lon: float
    label: str                    # nearest place name or "lat,lon"
    mile_marker: int              # approx miles from origin along route
    peak_wind_mph: float
    peak_gust_mph: float
    worst_weathercode: int
    worst_description: str
    dangerous_hours: list[HourlyCondition]


@dataclass
class RouteCheckResult:
    origin: str
    destination: str
    origin_coords: tuple[float, float] | None
    dest_coords: tuple[float, float] | None
    waypoints_checked: int
    total_miles: int
    dangerous_segments: list[DangerousSegment]
    all_clear: bool
    worst_segment: DangerousSegment | None


def check_route(origin: str, destination: str) -> RouteCheckResult:
    """Geocode origin+dest, fetch road waypoints, check weather at each."""
    from ..weather import _haversine_miles

    o_coords = geocode(origin)
    d_coords = geocode(destination)

    if not o_coords or not d_coords:
        return RouteCheckResult(
            origin=origin, destination=destination,
            origin_coords=o_coords, dest_coords=d_coords,
            waypoints_checked=0, total_miles=0, dangerous_segments=[],
            all_clear=False, worst_segment=None,
        )

    waypoints = get_route_waypoints(o_coords, d_coords, step_miles=50)
    dangerous: list[DangerousSegment] = []

    # Compute cumulative miles for each waypoint
    cumulative = [0.0]
    for i in range(1, len(waypoints)):
        cumulative.append(cumulative[-1] + _haversine_miles(waypoints[i - 1], waypoints[i]))
    total_miles = int(cumulative[-1]) if cumulative else 0

    for i, (lat, lon) in enumerate(waypoints):
        conditions = get_forecast(lat, lon, hours=12)
        bad = [c for c in conditions if c.is_dangerous]
        if not bad:
            continue

        peak_wind = max(c.windspeed_mph for c in bad)
        peak_gust = max(c.windgusts_mph for c in bad)
        worst = max(bad, key=lambda c: (c.windspeed_mph, c.weathercode in _SEVERE_WMO))

        dangerous.append(DangerousSegment(
            lat=lat, lon=lon,
            label=reverse_geocode(lat, lon),
            mile_marker=int(cumulative[i]),
            peak_wind_mph=peak_wind,
            peak_gust_mph=peak_gust,
            worst_weathercode=worst.weathercode,
            worst_description=worst.description,
            dangerous_hours=bad,
        ))

    worst_seg = max(dangerous, key=lambda s: s.peak_wind_mph) if dangerous else None

    return RouteCheckResult(
        origin=origin, destination=destination,
        origin_coords=o_coords, dest_coords=d_coords,
        waypoints_checked=len(waypoints),
        total_miles=total_miles,
        dangerous_segments=dangerous,
        all_clear=not dangerous,
        worst_segment=worst_seg,
    )


def build_weather_embed(result: RouteCheckResult) -> list[dict]:
    """Build Discord embeds from a RouteCheckResult. Returns a list (may be >1 for long routes)."""
    title = f"Route Weather — {result.origin} -> {result.destination}"

    if not result.origin_coords or not result.dest_coords:
        failed = result.origin if not result.origin_coords else result.destination
        return [{"title": title, "description": f"Could not geocode **{failed}**. Try `City, State` or `lat,lon`.", "color": 0xE74C3C}]

    miles_str = f" ({result.total_miles} mi total)" if result.total_miles else ""

    if result.all_clear:
        return [{
            "title": title,
            "description": (
                f"All clear{miles_str} -- {result.waypoints_checked} segments checked.\n"
                "No sustained winds >= 30 mph or severe weather in the next 12 hours."
            ),
            "color": 0x2ECC71,
            "footer": {"text": "Open-Meteo forecast · OpenRouteService routing"},
        }]

    def _make_field(seg: DangerousSegment) -> dict:
        hours_str = ", ".join(
            f"{c.time.strftime('%H:%M')} {c.description} {c.windspeed_mph:.0f}mph"
            for c in seg.dangerous_hours[:3]
        )
        if len(seg.dangerous_hours) > 3:
            hours_str += f" +{len(seg.dangerous_hours) - 3} more hrs"
        return {
            "name": f"⚠️ {seg.label} (mi ~{seg.mile_marker})",
            "value": (
                f"Wind **{seg.peak_wind_mph:.0f} mph** · Gusts {seg.peak_gust_mph:.0f} mph\n"
                f"{hours_str}"
            ),
            "inline": False,
        }

    n_segs = len(result.dangerous_segments)
    ws = result.worst_segment
    desc = (
        f"**{n_segs} dangerous segment{'s' if n_segs != 1 else ''}** on your {result.total_miles}-mile route.\n"
        f"Worst: **{ws.peak_wind_mph:.0f} mph winds** near {ws.label} (mi ~{ws.mile_marker}) — {ws.worst_description}."
    )
    footer = {"text": f"{result.total_miles} mi · {result.waypoints_checked} segments checked · Open-Meteo + OpenRouteService"}

    # Discord allows up to 25 fields per embed and 10 embeds per message.
    # Split into chunks of 25 so all segments are always shown.
    _FIELDS_PER_EMBED = 25
    all_fields = [_make_field(seg) for seg in result.dangerous_segments]
    chunks = [all_fields[i:i + _FIELDS_PER_EMBED] for i in range(0, len(all_fields), _FIELDS_PER_EMBED)]

    embeds = []
    for i, chunk in enumerate(chunks):
        embed: dict = {
            "title": title if i == 0 else f"{title} (cont.)",
            "color": 0xE74C3C,
            "fields": chunk,
        }
        if i == 0:
            embed["description"] = desc
        if i == len(chunks) - 1:
            embed["footer"] = footer
        embeds.append(embed)

    return embeds


def build_clearing_embed(lat: float, lon: float, label: str, waited_since: datetime) -> dict:
    """Embed posted when a weather watch detects conditions have cleared."""
    waited_min = int((datetime.now(timezone.utc) - waited_since).total_seconds() / 60)
    return {
        "title": "✅ Weather Clearing — Safe to Drive",
        "description": (
            f"Conditions near **{label}** have improved.\n"
            f"Wind below {WIND_DANGER_MPH:.0f} mph and no severe weather in the next 2 hours.\n"
            f"*(Waited {waited_min} min)*"
        ),
        "color": 0x2ECC71,
        "footer": {"text": "Open-Meteo forecast"},
    }


def build_still_waiting_embed(lat: float, lon: float, label: str, conditions: list[HourlyCondition]) -> dict:
    """Periodic status embed while waiting out a storm."""
    worst = max(conditions, key=lambda c: c.windspeed_mph) if conditions else None
    if worst:
        desc = (
            f"Still dangerous near **{label}**.\n"
            f"Current worst: **{worst.windspeed_mph:.0f} mph** wind — {worst.description}\n"
            f"Checking again in 30 min."
        )
    else:
        desc = f"Monitoring weather near **{label}**. Checking every 30 min."
    return {
        "title": "⏳ Waiting Out the Storm",
        "description": desc,
        "color": 0xF39C12,
        "footer": {"text": "Open-Meteo forecast"},
    }


def start_weather_watch(
    lat: float,
    lon: float,
    label: str,
    post_fn,           # callable(embed: dict) -> None
    *,
    max_polls: int = 48,    # 48 × 30 min = 24 h max
    poll_seconds: int = WATCH_POLL_SECONDS,
) -> None:
    """Poll weather at (lat, lon) until the next 2 hours look safe, then call post_fn.

    Blocks the calling thread/coroutine. Designed to run inside a Modal function
    with timeout=3600 (or longer for extended storms — set max_polls accordingly).
    """
    started = datetime.now(timezone.utc)

    for poll in range(max_polls):
        if poll > 0:
            time.sleep(poll_seconds)

        conditions = get_forecast(lat, lon, hours=2)
        if not conditions:
            # No forecast data — wait and retry
            continue

        still_bad = [c for c in conditions if c.is_dangerous]

        if not still_bad:
            embed = build_clearing_embed(lat, lon, label, started)
            post_fn(embed)
            return

        # Post a status update every ~2 hours (every 4 polls) so user knows it's still watching
        if poll > 0 and poll % 4 == 0:
            embed = build_still_waiting_embed(lat, lon, label, still_bad)
            post_fn(embed)

    # Timed out — post a final "still bad" message
    conditions = get_forecast(lat, lon, hours=2)
    still_bad = [c for c in conditions if c.is_dangerous] if conditions else []
    embed = build_still_waiting_embed(lat, lon, label, still_bad)
    post_fn(embed)


# --- Local CLI ----------------------------------------------------------------

def main() -> None:
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("origin", help="Starting location")
    parser.add_argument("destination", help="Ending location")
    args = parser.parse_args()

    print(f"Checking weather: {args.origin!r} -> {args.destination!r}")
    result = check_route(args.origin, args.destination)
    print(f"Waypoints checked: {result.waypoints_checked}")
    print(f"All clear: {result.all_clear}")
    if result.dangerous_segments:
        for seg in result.dangerous_segments:
            print(f"  [!] {seg.label}: {seg.peak_wind_mph:.0f} mph wind, {seg.worst_description}")

    embeds = build_weather_embed(result)
    import json
    print(f"\n--- Discord embeds ({len(embeds)}) ---")
    print(json.dumps(embeds, indent=2, default=str))


if __name__ == "__main__":
    main()
