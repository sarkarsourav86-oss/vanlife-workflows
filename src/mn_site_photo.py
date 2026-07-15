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


def _fetch_units(facility_id: int) -> dict[str, int]:
    """Return {normalized_site_name: UnitId} for a facility. Empty dict on failure."""
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
        return {
            _normalize_name(u.get("Name")): u["UnitId"]
            for u in units.values()
            if u.get("UnitId") and u.get("Name")
        }
    except Exception:
        return {}


def _get_units(facility_id: int) -> dict[str, int]:
    """Return unit name→UnitId map, using Modal Dict cache when available."""
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


def _match_unit_id(units: dict[str, int], campsite_name: str) -> int | None:
    """Find UnitId by matching campsite_name against the units dict."""
    target = _normalize_name(campsite_name)
    if not target:
        return None

    # Exact match first
    if target in units:
        return units[target]

    # Substring match: handles "Drive-In #16" matching "16" and vice versa
    for name, uid in units.items():
        if target in name or name in target:
            return uid

    return None


def get_mn_site_photo(campground_id: str, campsite_name: str | None) -> str | None:
    """Return a photo URL for an MN state park campsite, or None.

    Only works for campgrounds whose Campflare ID ends with -minnesotastateparks-{facilityId}.
    Returns None for non-MN-state-park campgrounds and for any lookup failure.
    """
    if not campsite_name:
        return None

    facility_id = _parse_facility_id(campground_id or "")
    if facility_id is None:
        return None

    units = _get_units(facility_id)
    if not units:
        return None

    unit_id = _match_unit_id(units, campsite_name)
    if unit_id is None:
        return None

    return f"{IMG_BASE}/{unit_id}.jpg"


if __name__ == "__main__":
    import sys

    cg_id = sys.argv[1] if len(sys.argv) > 1 else "temperance-river-state-park-campground-minnesotastateparks-783"
    site = sys.argv[2] if len(sys.argv) > 2 else "Drive-In #16"
    print(f"campground_id:  {cg_id}")
    print(f"campsite_name:  {site!r}")
    print(f"photo_url:      {get_mn_site_photo(cg_id, site)}")
