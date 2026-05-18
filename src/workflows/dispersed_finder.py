"""On-the-road dispersed-camping finder.

Single-shot workflow (no Campflare alert loop): given a lat/lng + radius,
return the top dispersed-camping pins from the local SQLite DB enriched with
PAD-US ownership, with optional Starlink sky-visibility scoring on the top
few candidates.

Differs from the alert-loop workflows ([region_finder.py]):
  - No Campflare alert created — dispersed isn't reservable inventory.
  - No webhook round-trip — query is synchronous.
  - Reads from a local DB built by `scripts/build_dispersed_db.py`, not the
    Campflare API.

Public surface:
    find_spots(lat, lng, radius_miles=30) -> list[Spot]
    build_embed(spots, origin) -> dict   # Discord embed
    parse_location(s) -> tuple[float, float] | None  # accepts "lat,lng" or place name
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ..dispersed_db import Pin, find_blacklist_pins_near, search_radius

CANDIDATE_CATEGORIES = ["Wild Camping", "Informal Campsite"]

# Drop pins not verified within this many years. iOverlander stale entries
# (campground closed, road washed out) are the main risk; 2y is generous
# enough that most legitimately-quiet spots stay in.
MAX_VERIFIED_AGE_DAYS = 365 * 2

# A candidate within this radius of an "Overnight Prohibited" pin gets dropped.
BLACKLIST_RADIUS_M = 200.0

# How many candidates to return in the embed.
TOP_N = 5

# Of those, how many get the (paid) Starlink sky score.
TOP_N_STARLINK = 3


@dataclass
class Spot:
    pin: Pin
    distance_miles: float
    starlink: dict | None = None  # {score, reasoning, confidence} or None


# --- Search ------------------------------------------------------------------


def _verified_recently(pin: Pin) -> bool:
    if not pin.date_verified:
        return False
    try:
        d = datetime.fromisoformat(pin.date_verified.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - d).days
    return age <= MAX_VERIFIED_AGE_DAYS


def _alerts_url_for(pin: Pin) -> str | None:
    """Best-effort agency-alerts URL based on PAD-US manager.

    USFS units have a stable per-forest alerts page; everything else gets a
    generic agency page or None. Surfacing this lets the user click through
    to current closures/orders, which the workflow does not auto-check.
    """
    mgr = (pin.padus_manager or "").upper()
    if mgr == "USFS":
        # We don't have the forest slug; link to the FS alerts portal.
        return "https://www.fs.usda.gov/visit/know-before-you-go"
    if mgr == "BLM":
        return "https://www.blm.gov/programs/recreation/recreation-activities"
    if mgr in ("SDNR", "SFW"):
        # Catch-all state DNR/wildlife-area pages would need state-by-state
        # lookup. Skip for v1.
        return None
    return None


def find_spots(lat: float, lng: float, radius_miles: float = 30.0) -> list[Spot]:
    """Return ranked dispersed-camping candidates near (lat, lng).

    Filter chain:
      1. categories ∈ {Wild Camping, Informal Campsite}
      2. PAD-US matched AND public access is Open
      3. verified within MAX_VERIFIED_AGE_DAYS
      4. no Overnight Prohibited pin within BLACKLIST_RADIUS_M
      5. nearest-first, capped at TOP_N
    """
    raw = search_radius(lat, lng, radius_miles=radius_miles, categories=CANDIDATE_CATEGORIES)

    spots: list[Spot] = []
    for pin, dist in raw:
        if not pin.is_open_access_public_land:
            continue
        if not _verified_recently(pin):
            continue
        if find_blacklist_pins_near(pin.lat, pin.lon, radius_meters=BLACKLIST_RADIUS_M):
            continue
        spots.append(Spot(pin=pin, distance_miles=dist))
        if len(spots) >= TOP_N:
            break
    return spots


def enrich_with_starlink(spots: list[Spot]) -> list[Spot]:
    """Add Starlink sky scores to the top TOP_N_STARLINK spots in place."""
    from ..starlink_score import get_starlink_score
    for spot in spots[:TOP_N_STARLINK]:
        try:
            score = get_starlink_score(spot.pin.id, spot.pin.lat, spot.pin.lon)
            if score:
                spot.starlink = {
                    "score": score.score,
                    "reasoning": score.reasoning,
                    "confidence": score.confidence,
                }
        except Exception as e:
            print(f"[dispersed_finder] starlink_score failed for {spot.pin.name}: {e}")
    return spots


# --- Location parsing --------------------------------------------------------


def _parse_latlng(s: str) -> tuple[float, float] | None:
    """Accept '47.9,-91.87' style strings; tolerate spaces."""
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 2:
        return None
    try:
        lat, lng = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def _geocode(s: str) -> tuple[float, float] | None:
    """Nominatim geocoding. Free, no key — 1 req/sec policy."""
    r = httpx.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": s, "format": "json", "limit": 1},
        headers={"User-Agent": "vanlife-workflows/0.1 (sourav.sarkar@ilmservice.com)"},
        timeout=15,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def parse_location(s: str) -> tuple[float, float] | None:
    """Resolve user input to (lat, lng). Tries lat,lng first, then geocodes."""
    s = (s or "").strip()
    if not s:
        return None
    coords = _parse_latlng(s)
    if coords:
        return coords
    return _geocode(s)


# --- Discord embed -----------------------------------------------------------


_STARLINK_EMOJI = {"good": "🛰️", "marginal": "📶", "poor": "🚫"}


def _gmaps_url(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps?q={lat},{lng}"


def _gearth_url(lat: float, lng: float) -> str:
    """Google Earth web URL with a tilted 3D satellite camera.
    Trailing tuple = altitude(a), distance(d), yaw(y), heading(h), tilt(t),
    roll(r); this combo gives a ~45° tilted view ~1km out."""
    return f"https://earth.google.com/web/@{lat},{lng},500a,1000d,35y,0h,45t,0r"


def _format_age(date_verified: str | None) -> str:
    if not date_verified:
        return "unknown"
    try:
        d = datetime.fromisoformat(date_verified.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    days = (datetime.now(timezone.utc) - d).days
    if days < 60:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def build_embed(spots: list[Spot], origin: tuple[float, float]) -> dict:
    """Single Discord embed listing the candidates."""
    if not spots:
        return {
            "title": "🏕️ No dispersed spots found",
            "description": (
                f"Searched a 30-mile radius around `{origin[0]:.4f}, {origin[1]:.4f}`. "
                f"Try a wider radius or different starting point."
            ),
            "color": 0xE74C3C,
        }

    lines: list[str] = []
    for i, spot in enumerate(spots, 1):
        p = spot.pin
        unit = p.padus_unit_name or p.padus_manager or "public land"
        sl = ""
        if spot.starlink:
            emoji = _STARLINK_EMOJI.get(spot.starlink["score"], "")
            sl = f" {emoji} {spot.starlink['score']}"
        age = _format_age(p.date_verified)
        gmaps = _gmaps_url(p.lat, p.lon)
        gearth = _gearth_url(p.lat, p.lon)
        alerts = _alerts_url_for(p)
        alerts_link = f" · [alerts]({alerts})" if alerts else ""
        lines.append(
            f"**{i}. {p.name}** — {spot.distance_miles:.1f} mi (straight line){sl}\n"
            f"   {unit} · verified {age}\n"
            f"   [maps]({gmaps}) · [satellite]({gearth}){alerts_link}"
        )

    return {
        "title": f"🏕️ Dispersed spots near {origin[0]:.4f}, {origin[1]:.4f}",
        "description": "\n\n".join(lines),
        "color": 0x2ECC71,
        "footer": {
            "text": "Spots are open-access public land per PAD-US. "
                    "Verify on-the-ground signage before camping."
        },
    }


# --- CLI for local testing ---------------------------------------------------


def main() -> None:
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("location", help="'lat,lng' or place name")
    parser.add_argument("--radius", type=float, default=30.0, help="miles")
    parser.add_argument("--starlink", action="store_true",
                        help="Fetch Starlink scores for the top results")
    args = parser.parse_args()

    coords = parse_location(args.location)
    if not coords:
        print(f"Could not resolve location: {args.location!r}")
        return
    lat, lng = coords
    print(f"Searching {args.radius}mi around ({lat:.4f}, {lng:.4f})")

    spots = find_spots(lat, lng, args.radius)
    print(f"Found {len(spots)} spots after filtering")
    if args.starlink and spots:
        if "ANTHROPIC_API_KEY" in os.environ:
            spots = enrich_with_starlink(spots)
        else:
            print("(skipping --starlink: ANTHROPIC_API_KEY not set)")

    for i, spot in enumerate(spots, 1):
        p = spot.pin
        sl = f" starlink={spot.starlink['score']}" if spot.starlink else ""
        print(
            f"  {i}. [{spot.distance_miles:5.1f}mi] {p.name[:40]:40}  "
            f"{p.padus_manager or '?':6}  {p.padus_unit_name or '?'}{sl}"
        )


if __name__ == "__main__":
    main()
