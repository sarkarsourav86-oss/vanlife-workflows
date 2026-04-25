"""Estimate whether Starlink will work at a campground from satellite imagery.

Pulls a 3x3 mosaic of ESRI World Imagery tiles around the campground's lat/lng
(zoom 17, ~450m square) and asks Claude Sonnet vision to rate sky visibility.
Result is cached in a Modal Dict keyed by campground_id — canopy doesn't change,
so we pay per campground exactly once.

Public API:
    get_starlink_score(campground_id, lat, lng) -> StarlinkScore | None

Returns None on any failure (ESRI down, vision call rate-limited, malformed
response). The webhook handler treats the score as optional decoration —
never crashes if it's missing.

ESRI World Imagery tile URL scheme:
    https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}

Free, no API key, no rate limit you'll hit at this volume.
"""

from __future__ import annotations

import base64
import io
import math
import os
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from .cost_tracker import log_llm_call

MODEL = "claude-sonnet-4-6"
ZOOM = 17
TILE_GRID = 3  # 3x3 mosaic = ~450m square at zoom 17 in mid-latitudes
TILE_SIZE_PX = 256

ESRI_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)

SYSTEM_PROMPT = """You are evaluating whether Starlink satellite internet will
work at a campground based on satellite imagery. Starlink dishes need a clear
view of roughly 100 degrees of sky — heavy tree canopy, dense forest, deep
canyons, or tall ridges to the north all degrade or block service.

Rate the campground:
  - "good": Open meadow, clearing, desert, plains, lakeshore — wide-open sky.
  - "marginal": Partial canopy, scattered trees, edges of clearings, mild
    terrain. Some sites within may work, others may not.
  - "poor": Dense forest, heavy canopy, deep canyon, or steep terrain to the
    north blocking the sky.

Be honest about confidence: imagery resolution and seasonal variation make
this judgment hard. Use "low" when canopy density or terrain is hard to
read from the image.
"""


class StarlinkScore(BaseModel):
    score: Literal["good", "marginal", "poor"]
    reasoning: str = Field(description="One sentence explaining the rating.")
    confidence: Literal["high", "medium", "low"]


def _latlng_to_tile(lat: float, lng: float, zoom: int) -> tuple[int, int]:
    """Web Mercator tile coordinates for a lat/lng at zoom level."""
    n = 2 ** zoom
    x = int((lng + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def _fetch_mosaic(lat: float, lng: float) -> bytes | None:
    """Fetch a TILE_GRID x TILE_GRID mosaic of ESRI tiles centered on lat/lng.

    Returns PNG bytes of the stitched image, or None if any tile fetch fails.
    """
    try:
        from PIL import Image
    except ImportError:
        # Pillow not installed — fall back to single-tile (no stitching needed).
        return _fetch_single_tile(lat, lng)

    cx, cy = _latlng_to_tile(lat, lng, ZOOM)
    half = TILE_GRID // 2
    mosaic = Image.new("RGB", (TILE_GRID * TILE_SIZE_PX, TILE_GRID * TILE_SIZE_PX))

    with httpx.Client(timeout=15.0) as http:
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                url = ESRI_TILE_URL.format(z=ZOOM, x=cx + dx, y=cy + dy)
                r = http.get(url)
                if r.status_code != 200:
                    return None
                tile = Image.open(io.BytesIO(r.content)).convert("RGB")
                px = (dx + half) * TILE_SIZE_PX
                py = (dy + half) * TILE_SIZE_PX
                mosaic.paste(tile, (px, py))

    buf = io.BytesIO()
    mosaic.save(buf, format="PNG")
    return buf.getvalue()


def _fetch_single_tile(lat: float, lng: float) -> bytes | None:
    """Fallback when Pillow isn't available: just grab the center tile."""
    cx, cy = _latlng_to_tile(lat, lng, ZOOM)
    url = ESRI_TILE_URL.format(z=ZOOM, x=cx, y=cy)
    try:
        r = httpx.get(url, timeout=15.0)
        if r.status_code != 200:
            return None
        return r.content
    except Exception:
        return None


def _classify(image_bytes: bytes, campground_name: str | None = None) -> StarlinkScore | None:
    """Send the image to Sonnet and return a structured rating, or None on failure."""
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage, SystemMessage

    image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    llm = ChatAnthropic(model=MODEL, temperature=0).with_structured_output(
        StarlinkScore, include_raw=True
    )
    user_text = "Rate this campground's Starlink suitability."
    if campground_name:
        user_text = f"Rate Starlink suitability for: {campground_name}."

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=[
            {"type": "text", "text": user_text},
            {"type": "image", "source_type": "base64",
             "mime_type": "image/png", "data": image_b64},
        ]),
    ]

    try:
        result = llm.invoke(messages)
    except Exception as e:
        print(f"[starlink_score] vision call failed: {e}")
        return None

    raw = result.get("raw") if isinstance(result, dict) else None
    parsed = result.get("parsed") if isinstance(result, dict) else None
    if parsed is None:
        return None

    usage = getattr(raw, "usage_metadata", None) or {}
    try:
        log_llm_call(
            model=MODEL,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cached_input_tokens=(
                usage.get("input_token_details", {}).get("cache_read", 0)
                if isinstance(usage.get("input_token_details"), dict)
                else 0
            ),
            purpose="starlink_score",
        )
    except Exception:
        pass

    return parsed


def _cache():
    """Lazy import so the module is usable outside Modal (e.g., in tests).

    Returns the Modal Dict, or None when not running with Modal available.
    """
    try:
        import modal
        return modal.Dict.from_name("campground-starlink-scores", create_if_missing=True)
    except Exception:
        return None


def _coords_cache():
    """Cache of campground_id -> (lat, lng). Webhook payloads don't carry coords,
    so we look them up via Campflare /campground/{id} and cache forever.
    """
    try:
        import modal
        return modal.Dict.from_name("campground-coords", create_if_missing=True)
    except Exception:
        return None


def _lookup_coords(campground_id: str) -> tuple[float, float] | None:
    """Fetch campground coordinates from Campflare and cache them."""
    cache = _coords_cache()
    if cache is not None:
        cached = cache.get(campground_id)
        if cached:
            try:
                return float(cached["lat"]), float(cached["lng"])
            except Exception:
                pass

    api_key = os.environ.get("CAMPFLARE_API_KEY")
    if not api_key:
        return None
    try:
        r = httpx.get(
            f"https://api.campflare.com/v2/campground/{campground_id}",
            headers={"Authorization": api_key},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        loc = data.get("location") or {}
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        if lat is None or lng is None:
            return None
        if cache is not None:
            try:
                cache[campground_id] = {"lat": lat, "lng": lng}
            except Exception:
                pass
        return float(lat), float(lng)
    except Exception:
        return None


def get_starlink_score(
    campground_id: str,
    lat: float | None = None,
    lng: float | None = None,
    campground_name: str | None = None,
    *,
    force_refresh: bool = False,
) -> StarlinkScore | None:
    """Cached lookup; computes on miss. Returns None on any failure.

    If lat/lng are not supplied, looks them up from Campflare's /campground/{id}
    endpoint (also cached). Webhook payloads don't carry coordinates, so the
    common path is name-only.
    """
    cache = _cache()
    if cache is not None and not force_refresh:
        cached = cache.get(campground_id)
        if cached:
            try:
                return StarlinkScore.model_validate(cached)
            except Exception:
                pass  # corrupt cache entry; recompute

    if lat is None or lng is None:
        coords = _lookup_coords(campground_id)
        if coords is None:
            return None
        lat, lng = coords

    image_bytes = _fetch_mosaic(lat, lng)
    if image_bytes is None:
        return None

    score = _classify(image_bytes, campground_name)
    if score is None:
        return None

    if cache is not None:
        try:
            cache[campground_id] = score.model_dump()
        except Exception:
            pass

    return score


if __name__ == "__main__":
    # Manual smoke test against a known-open campground (Apgar in Glacier).
    from dotenv import load_dotenv
    load_dotenv()
    score = get_starlink_score(
        campground_id="apgar-campground-274",
        lat=48.5235, lng=-113.9974,
        campground_name="Apgar Campground (Glacier)",
    )
    print(score)
