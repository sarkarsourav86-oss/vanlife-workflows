"""Fetch site photos for MN state park campsites via the ReserveMN (UseDirect) API.

Public surface:
    get_mn_site_photo(campground_id, campsite_name) -> str | None

Returns a photo URL like:
    https://reservemn.usedirect.com/MinnesotaWeb/images/Minnesota/ParkImages/Units/{UnitId}.jpg

Strategy:
  1. Resolve the Campflare campground_id to a UseDirect PlaceId via a known
     mapping (searched once and cached in Modal Dict "mn-place-id-cache").
  2. Fetch all facilities for that PlaceId (GET fd/facilities, cached in
     "mn-facilities-cache" keyed by PlaceId).
  3. For each facility, fetch the unit grid (POST search/grid, cached in
     "mn-units-cache" keyed by FacilityId) to get UnitId per site name.
  4. Match campsite_name to a unit Name and build the photo URL.

The Campflare campground_id for MN state parks follows the pattern:
    {park-slug}-minnesotastateparks-{facilityId}
e.g. temperance-river-state-park-campground-minnesotastateparks-755

The trailing integer IS the UseDirect FacilityId, so step 1-2 can be skipped
by parsing it directly — making the photo lookup a single cached grid call.

All caches are Modal Dicts. Outside Modal (local dev) every call goes to the
network; that's fine for one-off testing.

Returns None on any failure; webhook handler treats photo as optional decoration.
"""

from __future__ import annotations

import re

import httpx

RDR_BASE = "https://mnrdr.usedirect.com/minnesotardr/rdr"
IMG_BASE = "https://reservemn.usedirect.com/MinnesotaWeb/images/Minnesota/ParkImages/Units"

_FACILITY_ID_RE = re.compile(r"-minnesotastateparks-(\d+)$")


def _parse_facility_id(campground_id: str) -> int | None:
    """Extract the UseDirect FacilityId from a Campflare campground_id."""
    m = _FACILITY_ID_RE.search(campground_id or "")
    return int(m.group(1)) if m else None


def _normalize_name(name: str | None) -> str:
    if not name:
        return ""
    return " ".join(name.strip().lower().split())


def _units_cache():
    try:
        import modal
        return modal.Dict.from_name("mn-units-cache", create_if_missing=True)
    except Exception:
        return None


def _fetch_units(facility_id: int) -> dict[str, dict]:
    """Return {normalized_site_name: {unit_id, lat, lon}} for a facility."""
    try:
        r = httpx.post(
            f"{RDR_BASE}/search/grid",
            json={"FacilityId": facility_id, "UnitTypeId": 0, "StartDate": "07/01/2026", "Nights": 1},
            headers={"Content-Type": "application/json"},
            timeout=15.0,
        )
        if r.status_code != 200:
            return {}
        units = r.json().get("Facility", {}).get("Units", {})
        result = {}
        for u in units.values():
            uid = u.get("UnitId")
            name = u.get("Name")
            if not uid or not name:
                continue
            map_info = u.get("MapInfo") or {}
            lat = map_info.get("Latitude") or None
            lon = map_info.get("Longitude") or None
            if lat == 0.0:
                lat = None
            if lon == 0.0:
                lon = None
            result[_normalize_name(name)] = {"unit_id": uid, "lat": lat, "lon": lon}
        return result
    except Exception:
        return {}


def _fetch_sibling_facility_ids(facility_id: int) -> list[int]:
    """Return all facility IDs that share the same PlaceId as facility_id."""
    try:
        r = httpx.get(f"{RDR_BASE}/fd/facilities", timeout=10.0)
        if r.status_code != 200:
            return []
        all_facilities = r.json()
        place_id = next(
            (f["PlaceId"] for f in all_facilities if f.get("FacilityId") == facility_id), None
        )
        if place_id is None:
            return []
        return [
            f["FacilityId"] for f in all_facilities
            if f.get("PlaceId") == place_id and f.get("FacilityId") != facility_id
        ]
    except Exception:
        return []


def _get_units(facility_id: int) -> dict[str, dict]:
    """Return unit name→unit record map, using Modal Dict cache when available."""
    cache = _units_cache()
    cache_key = str(facility_id)
    if cache is not None:
        cached = cache.get(cache_key)
        if cached:
            return cached
    units = _fetch_units(facility_id)
    if units and cache is not None:
        try:
            cache[cache_key] = units
        except Exception:
            pass
    return units


def _get_all_units_for_park(facility_id: int) -> dict[str, dict]:
    """Merge units from all facilities (loops) within the same park."""
    units = dict(_get_units(facility_id))
    for sibling_id in _fetch_sibling_facility_ids(facility_id):
        units.update(_get_units(sibling_id))
    return units


def _match_unit(units: dict[str, dict], campsite_name: str) -> dict | None:
    """Find unit record by matching campsite_name against the units dict."""
    target = _normalize_name(campsite_name)
    if not target:
        return None
    if target in units:
        return units[target]
    for name, rec in units.items():
        if target in name or name in target:
            return rec
    return None


def get_mn_site_coords(campground_id: str, campsite_name: str | None) -> tuple[float, float] | None:
    """Return (lat, lon) for an MN state park campsite, or None."""
    if not campsite_name:
        return None
    facility_id = _parse_facility_id(campground_id or "")
    if facility_id is None:
        return None
    units = _get_all_units_for_park(facility_id)
    rec = _match_unit(units, campsite_name)
    if rec and rec.get("lat") and rec.get("lon"):
        return rec["lat"], rec["lon"]
    return None


def get_mn_site_photo(campground_id: str, campsite_name: str | None) -> str | None:
    """Return a photo URL for an MN state park campsite, or None.

    Only works for campgrounds whose Campflare ID ends with -minnesotastateparks-{facilityId}.
    Searches all loops/facilities within the same park, so a Campflare campground that
    covers multiple UseDirect facilities (e.g. Upper + Lower) resolves correctly.
    """
    if not campsite_name:
        return None

    facility_id = _parse_facility_id(campground_id or "")
    if facility_id is None:
        return None

    units = _get_all_units_for_park(facility_id)
    rec = _match_unit(units, campsite_name)
    if not rec:
        return None
    return f"{IMG_BASE}/{rec['unit_id']}.jpg"


def fetch_mn_site_photo_bytes(photo_url: str) -> bytes | None:
    """Fetch an MN state park photo and re-encode as plain JPEG bytes.

    ReserveMN photos are MPO files (multi-picture stereoscopic JPEG) that
    Discord's image proxy refuses to render. Re-encoding with Pillow strips
    the MPO wrapper and produces a standard JPEG Discord can display.
    Returns None on any failure.
    """
    try:
        import io
        from PIL import Image
        r = httpx.get(photo_url, timeout=15.0)
        if r.status_code != 200:
            return None
        img = Image.open(io.BytesIO(r.content))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None


if __name__ == "__main__":
    import sys

    cg_id = sys.argv[1] if len(sys.argv) > 1 else "temperance-river-state-park-campground-minnesotastateparks-783"
    site = sys.argv[2] if len(sys.argv) > 2 else "Drive-In #16"
    print(f"campground_id:  {cg_id}")
    print(f"campsite_name:  {site!r}")
    print(f"photo_url:      {get_mn_site_photo(cg_id, site)}")
