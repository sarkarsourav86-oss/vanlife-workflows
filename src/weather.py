"""Weather primitives: Open-Meteo forecast + geocoding, OpenRouteService road routing.

Public surface:
    get_forecast(lat, lon, hours) -> list[HourlyCondition]
    geocode(location_str) -> tuple[float, float] | None
    get_route_waypoints(origin, dest, step_miles) -> list[tuple[float, float]]
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

WIND_DANGER_MPH = 30.0
GUST_WARN_MPH = 40.0

# WMO weather interpretation codes that constitute severe/dangerous driving conditions.
_SEVERE_WMO = frozenset([
    63, 64, 65,          # moderate/heavy rain
    66, 67,              # freezing rain
    71, 73, 75, 77,      # snow
    80, 81, 82,          # rain showers (heavy)
    85, 86,              # snow showers
    95, 96, 99,          # thunderstorm
])

_WMO_LABELS: dict[int, str] = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Heavy rain showers", 82: "Violent rain showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}


@dataclass
class HourlyCondition:
    time: datetime
    windspeed_mph: float
    windgusts_mph: float
    weathercode: int
    description: str
    is_dangerous: bool


def get_forecast(lat: float, lon: float, hours: int = 12) -> list[HourlyCondition]:
    """Fetch hourly wind + weather from Open-Meteo (no API key required)."""
    forecast_days = max(1, math.ceil(hours / 24))
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "windspeed_10m,windgusts_10m,weathercode",
        "wind_speed_unit": "mph",
        "forecast_days": forecast_days,
        "timezone": "auto",
    }
    resp = httpx.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    speeds = hourly.get("windspeed_10m") or []
    gusts = hourly.get("windgusts_10m") or []
    codes = hourly.get("weathercode") or []

    now = datetime.now(timezone.utc)
    out: list[HourlyCondition] = []
    for i, t_str in enumerate(times):
        if len(out) >= hours:
            break
        try:
            t = datetime.fromisoformat(t_str)
            if t.tzinfo is None:
                tz_offset = data.get("utc_offset_seconds", 0)
                from datetime import timedelta
                t = t.replace(tzinfo=timezone(timedelta(seconds=tz_offset)))
        except ValueError:
            continue
        if t < now:
            continue

        speed = float(speeds[i]) if i < len(speeds) and speeds[i] is not None else 0.0
        gust = float(gusts[i]) if i < len(gusts) and gusts[i] is not None else 0.0
        code = int(codes[i]) if i < len(codes) and codes[i] is not None else 0
        label = _WMO_LABELS.get(code, f"Code {code}")
        dangerous = speed >= WIND_DANGER_MPH or code in _SEVERE_WMO

        out.append(HourlyCondition(
            time=t,
            windspeed_mph=speed,
            windgusts_mph=gust,
            weathercode=code,
            description=label,
            is_dangerous=dangerous,
        ))

    return out


def geocode(location: str) -> tuple[float, float] | None:
    """Resolve a place name or 'lat,lon' string to (lat, lon). No API key needed.

    Accepts:
      - "lat,lon" literals (e.g. "46.78,-92.10")
      - Plain city names ("Duluth")
      - "City ST" or "City, ST" US format — strips state abbreviation on retry
        since Open-Meteo geocoding doesn't understand US state codes
    """
    location = location.strip()

    # Try lat,lon literal first
    if "," in location:
        parts = location.split(",", 1)
        try:
            return float(parts[0].strip()), float(parts[1].strip())
        except ValueError:
            pass

    def _query(name: str) -> tuple[float, float] | None:
        url = "https://geocoding-api.open-meteo.com/v1/search"
        resp = httpx.get(url, params={"name": name, "count": 1, "language": "en", "format": "json"}, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return None
        r = results[0]
        return float(r["latitude"]), float(r["longitude"])

    result = _query(location)
    if result:
        return result

    # Retry: strip a trailing 2-letter state abbreviation ("Duluth MN" -> "Duluth")
    words = location.split()
    if len(words) >= 2 and len(words[-1]) == 2 and words[-1].isalpha():
        city_only = " ".join(words[:-1])
        result = _query(city_only)
        if result:
            return result

    # Retry: strip trailing comma-separated part ("Duluth, MN" already handled above
    # by lat,lon split — but "Glacier National Park, MT" needs this)
    if "," in location:
        city_only = location.split(",")[0].strip()
        return _query(city_only)

    return None


def geocode_bbox(location: str, radius_miles: float = 100.0) -> tuple[float, float, float, float] | None:
    """Geocode a location string and return a (min_lat, max_lat, min_lon, max_lon) bounding box.

    Uses Nominatim (OSM) which correctly handles US state names, national parks,
    and cities — unlike Open-Meteo geocoding which only knows populated places.
    Returns None if geocoding fails.
    """
    try:
        resp = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": location, "format": "json", "limit": 1, "addressdetails": 0},
            headers={"User-Agent": "vanlife-workflows/1.0 sourav.sarkar@ilmservice.com"},
            timeout=10,
            follow_redirects=True,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None
        lat = float(results[0]["lat"])
        lon = float(results[0]["lon"])
    except Exception:
        return None
    # 1 degree lat ~ 69 miles; 1 degree lon ~ 69 * cos(lat) miles
    lat_delta = radius_miles / 69.0
    lon_delta = radius_miles / (69.0 * math.cos(math.radians(lat)))
    return (lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta)


def reverse_geocode(lat: float, lon: float) -> str:
    """Return a human-readable location label for (lat, lon) via Nominatim (OSM).

    Tries city → town → village → hamlet → municipality in order. For genuinely
    rural points with no named place, falls back to "County area, ST" so the
    driver knows the region without a misleading city name.
    """
    fallback = f"{lat:.2f}°N {abs(lon):.2f}°{'W' if lon < 0 else 'E'}"
    try:
        resp = httpx.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 10, "addressdetails": 1},
            headers={"User-Agent": "vanlife-workflows/1.0 sourav.sarkar@ilmservice.com"},
            timeout=8,
            follow_redirects=True,
        )
        resp.raise_for_status()
        addr = resp.json().get("address") or {}
        state = (addr.get("ISO3166-2-lvl4") or "").split("-")[-1]
        place = (
            addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("hamlet")
            or addr.get("municipality")
        )
        county = addr.get("county")
        if place and state:
            return f"{place}, {state}"
        if place:
            return place
        if county and state:
            return f"{county} area, {state}"
        if county:
            return f"{county} area"
    except Exception:
        pass
    return fallback


def get_route_waypoints(
    origin: tuple[float, float],
    dest: tuple[float, float],
    step_miles: float = 50.0,
) -> list[tuple[float, float]]:
    """Get road-accurate waypoints via OpenRouteService, sampled every ~step_miles.

    Requires ORS_API_KEY env var. Falls back to straight-line interpolation if
    unavailable (e.g., local testing without the secret).
    """
    api_key = os.environ.get("ORS_API_KEY")
    if not api_key:
        return _interpolate_straight_line(origin, dest, step_miles)

    url = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
    # ORS takes [lon, lat] order
    body = {"coordinates": [[origin[1], origin[0]], [dest[1], dest[0]]]}
    try:
        resp = httpx.post(
            url,
            json=body,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        coords = resp.json()["features"][0]["geometry"]["coordinates"]
        # coords is list of [lon, lat]
        latlon_path = [(c[1], c[0]) for c in coords]
        return _sample_polyline(latlon_path, step_miles)
    except Exception as e:
        print(f"[weather] ORS routing failed ({e}), falling back to straight line")
        return _interpolate_straight_line(origin, dest, step_miles)


# --- Internal helpers ---------------------------------------------------------

_EARTH_RADIUS_MILES = 3958.8


def _haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def _sample_polyline(
    path: list[tuple[float, float]], step_miles: float
) -> list[tuple[float, float]]:
    """Pick one representative point per step_miles along a polyline."""
    if not path:
        return []
    sampled = [path[0]]
    accumulated = 0.0
    for i in range(1, len(path)):
        seg = _haversine_miles(path[i - 1], path[i])
        accumulated += seg
        if accumulated >= step_miles:
            sampled.append(path[i])
            accumulated = 0.0
    if sampled[-1] != path[-1]:
        sampled.append(path[-1])
    return sampled


def _interpolate_straight_line(
    origin: tuple[float, float],
    dest: tuple[float, float],
    step_miles: float,
) -> list[tuple[float, float]]:
    total = _haversine_miles(origin, dest)
    n_steps = max(1, int(total / step_miles))
    points = []
    for i in range(n_steps + 1):
        t = i / n_steps
        lat = origin[0] + t * (dest[0] - origin[0])
        lon = origin[1] + t * (dest[1] - origin[1])
        points.append((lat, lon))
    return points
