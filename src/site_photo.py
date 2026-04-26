"""Fetch site-level info (photo + shade attribute) from recreation.gov.

Public surface:
    get_site_info(reservation_url, campsite_name) -> SiteInfo | None

SiteInfo carries:
    photo_url: best-effort hero image for the matched site.
    shade: "Yes" / "No" / None — recreation.gov's Shade attribute.

Strategy:
  1. Parse a recreation.gov facility_id (or direct site_id) from the URL.
     - https://www.recreation.gov/camping/campgrounds/{facility_id}     -> facility
     - https://www.recreation.gov/camping/campsites/{site_id}           -> direct site
  2. If we have a direct site_id, hit the site-detail search and pull both
     preview_image_url and the Shade attribute.
  3. Otherwise fetch every campsite under that facility (one HTTP call,
     cached forever in Modal Dict by facility_id), match campsite_name to
     a site, and pull the same fields.

Caching: two layers.
  - rec-gov-facility-sites: facility_id -> list of campsite dicts (full).
    Refreshed never; recreation.gov adds sites rarely.
  - rec-gov-site-info: f"{facility_id}|{normalized_name}" -> {photo_url, shade}.
    Cuts the per-alert lookup to one Dict read.

Returns None on any failure; webhook handler treats this as optional
decoration. Recreation.gov has no documented public API, so endpoint
shapes can shift — failures should never break alert delivery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

REC_GOV_BASE = "https://www.recreation.gov"
SEARCH_CAMPSITES_PATH = "/api/search/campsites"
USER_AGENT = "Mozilla/5.0 (vanlife-workflows; +github.com/sarkarsourav86-oss/vanlife-workflows)"

# Recreation.gov returns image paths under cdn.recreation.gov; size suffix
# (_700.webp / _200.webp / etc) controls dimensions. _700 is good enough for
# Discord embed previews and well under the 8MB embed limit.

_FACILITY_RE = re.compile(r"/camping/campgrounds/(\d+)")
_SITE_RE = re.compile(r"/camping/campsites/(\d+)")


@dataclass(frozen=True)
class SiteInfo:
    photo_url: str | None
    shade: str | None  # "Yes" / "No" / None when missing


def _extract_ids(reservation_url: str) -> tuple[str | None, str | None]:
    """Return (facility_id, site_id) extracted from a recreation.gov URL.

    Either or both can be None — Campflare may give us a campground URL
    (no site_id) or a site-direct URL (no facility_id).
    """
    if not reservation_url:
        return None, None
    try:
        parsed = urlparse(reservation_url)
        if "recreation.gov" not in parsed.netloc:
            return None, None
        path = parsed.path
    except Exception:
        return None, None

    fac_m = _FACILITY_RE.search(path)
    site_m = _SITE_RE.search(path)
    return (fac_m.group(1) if fac_m else None,
            site_m.group(1) if site_m else None)


def _fetch_site_by_id(site_id: str) -> dict | None:
    """Fetch a single campsite directly by its recreation.gov ID."""
    try:
        r = httpx.get(
            f"{REC_GOV_BASE}{SEARCH_CAMPSITES_PATH}",
            params={"fq": f"campsite_id:{site_id}", "size": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        sites = (r.json() or {}).get("campsites") or []
        return sites[0] if sites else None
    except Exception:
        return None


def _fetch_facility_sites(facility_id: str) -> list[dict]:
    """Fetch every campsite under a recreation.gov facility. ~342 max in practice."""
    try:
        r = httpx.get(
            f"{REC_GOV_BASE}{SEARCH_CAMPSITES_PATH}",
            params={"fq": f"asset_id:{facility_id}", "size": 1000},
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
        )
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("campsites") or []
    except Exception:
        return []


def _normalize_name(name: str | None) -> str:
    """Lowercase + strip 'site' prefix + collapse whitespace.

    Campflare uses 'Site 12' / '12' / 'Ash Ridge' interchangeably; recreation.gov
    just uses '12' or 'Ash Ridge'. Normalize both ends to compare.
    """
    if not name:
        return ""
    s = name.strip().lower()
    if s.startswith("site "):
        s = s[5:]
    return " ".join(s.split())


def _match_site(sites: list[dict], campsite_name: str | None) -> dict | None:
    """Find the site in `sites` whose name matches `campsite_name`.

    Strategy: exact normalized match, then prefix match (handles "Loop A 12"
    vs "12"). If multiple sites match (recreation.gov has duplicate names —
    e.g. two sites both called "10" at Colter Bay), returns the first one;
    the photo will be approximately right.
    """
    if not campsite_name or not sites:
        return None
    target = _normalize_name(campsite_name)
    if not target:
        return None

    exact: list[dict] = []
    contains: list[dict] = []
    for s in sites:
        norm = _normalize_name(s.get("name"))
        if not norm:
            continue
        if norm == target:
            exact.append(s)
        elif target in norm or norm in target:
            contains.append(s)

    if exact:
        return exact[0]
    if contains:
        return contains[0]
    return None


def _facility_sites_cache():
    """Modal Dict of facility_id -> list[site dict]. None outside Modal."""
    try:
        import modal
        return modal.Dict.from_name("rec-gov-facility-sites", create_if_missing=True)
    except Exception:
        return None


def _info_cache():
    """Modal Dict of cache_key -> {photo_url, shade}. None outside Modal."""
    try:
        import modal
        return modal.Dict.from_name("rec-gov-site-info", create_if_missing=True)
    except Exception:
        return None


def _site_dict_to_info(site: dict | None) -> SiteInfo | None:
    if not site:
        return None
    photo = site.get("preview_image_url")
    photo_url = photo if isinstance(photo, str) and photo else None
    shade: str | None = None
    for attr in (site.get("attributes") or []):
        if attr.get("attribute_name") == "Shade":
            v = attr.get("attribute_value")
            if isinstance(v, str) and v:
                shade = v
            break
    if photo_url is None and shade is None:
        return None
    return SiteInfo(photo_url=photo_url, shade=shade)


def get_site_info(reservation_url: str, campsite_name: str | None) -> SiteInfo | None:
    """Best-effort lookup of recreation.gov site info for an alert.

    Returns None for non-recreation.gov reservation URLs (state parks,
    private campgrounds), for facilities we can't find, for sites we
    can't match by name, and for any HTTP/parse error.
    """
    facility_id, site_id = _extract_ids(reservation_url)
    if not facility_id and not site_id:
        return None

    info_cache = _info_cache()

    # Direct site_id path: shortest, most accurate.
    if site_id:
        cache_key = f"site:{site_id}"
        if info_cache is not None:
            cached = info_cache.get(cache_key)
            if cached:
                return SiteInfo(
                    photo_url=cached.get("photo_url"),
                    shade=cached.get("shade"),
                )
        info = _site_dict_to_info(_fetch_site_by_id(site_id))
        if info and info_cache is not None:
            try:
                info_cache[cache_key] = {"photo_url": info.photo_url, "shade": info.shade}
            except Exception:
                pass
        return info

    # Facility-only path: fetch site list (cached), match by name.
    norm = _normalize_name(campsite_name)
    cache_key = f"{facility_id}|{norm}"
    if info_cache is not None:
        cached = info_cache.get(cache_key)
        if cached:
            return SiteInfo(
                photo_url=cached.get("photo_url"),
                shade=cached.get("shade"),
            )

    sites_cache = _facility_sites_cache()
    sites: list[dict] | None = None
    if sites_cache is not None:
        sites = sites_cache.get(facility_id)

    if sites is None:
        sites = _fetch_facility_sites(facility_id)
        if sites and sites_cache is not None:
            try:
                sites_cache[facility_id] = sites
            except Exception:
                pass

    if not sites:
        return None

    info = _site_dict_to_info(_match_site(sites, campsite_name))
    if info and info_cache is not None:
        try:
            info_cache[cache_key] = {"photo_url": info.photo_url, "shade": info.shade}
        except Exception:
            pass
    return info


if __name__ == "__main__":
    # Smoke test: pick a known recreation.gov campground and try a site name.
    import sys
    url = "https://www.recreation.gov/camping/campgrounds/258830"  # Colter Bay
    name = sys.argv[1] if len(sys.argv) > 1 else "12"
    print(f"reservation_url: {url}")
    print(f"campsite_name:   {name!r}")
    print(f"site_info:       {get_site_info(url, name)}")
