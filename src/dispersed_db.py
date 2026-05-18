"""Read-only access to the local dispersed-camping database.

The DB is built by ``scripts/build_dispersed_db.py`` from an iOverlander JSON
export, enriched per-pin with a PAD-US ownership lookup. This module is the
*query* side — it never writes.

Schema (see build script for the canonical definition):

    pins(
        id TEXT PRIMARY KEY,        -- iOverlander uses no stable id; we hash
        name TEXT,
        category TEXT,              -- 'Wild Camping', 'Informal Campsite', etc.
        lat REAL,
        lon REAL,
        date_verified TEXT,         -- ISO date, may be NULL

        padus_status TEXT,          -- 'match' | 'no_match' | 'error' | 'pending'
        padus_manager TEXT,         -- 'USFS', 'BLM', 'SDNR', ...
        padus_manager_type TEXT,    -- 'FED' | 'STAT' | 'LOC' | ...
        padus_unit_name TEXT,       -- 'Superior National Forest'
        padus_designation TEXT,     -- 'NF', 'SRMA', 'NP', ...
        padus_pub_access TEXT,      -- 'OA' (open) | 'RA' (restricted) | 'XA' (closed)

        ingested_at REAL            -- unix timestamp of the last enrichment
    );

    pins_rtree(id, min_lat, max_lat, min_lon, max_lon)  -- R*Tree spatial index

Bbox queries hit the R*Tree first (sub-ms even at full-US scale), then join
back to the main table.
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

def _db_path() -> Path:
    """Read $DISPERSED_DB_PATH each call so callers can override it after this
    module has been imported (e.g. Modal workers setting the env var inside
    the function body before triggering imports)."""
    return Path(os.environ.get("DISPERSED_DB_PATH", "dispersed.db"))


# Open-access flags from PAD-US. OA = open access (USFS, BLM, most state forests).
# RA = restricted (permits, hours, zones). XA = closed.
OPEN_ACCESS_VALUES = {"OA"}

# PAD-US manager types that allow dispersed camping. FED covers USFS/BLM/NPS;
# STAT covers state forests and state wildlife areas. CITY/CNTY/LOC tag
# municipal/county parks where overnight camping is generally prohibited even
# though they're "open access" daytime — exclude them from dispersed results.
DISPERSED_MANAGER_TYPES = {"FED", "STAT"}


@dataclass
class Pin:
    id: str
    name: str
    category: str
    lat: float
    lon: float
    date_verified: str | None
    padus_status: str
    padus_manager: str | None
    padus_manager_type: str | None
    padus_unit_name: str | None
    padus_designation: str | None
    padus_pub_access: str | None

    @property
    def is_open_access_public_land(self) -> bool:
        """True iff PAD-US matched, access is open, AND it's federal/state land
        (not a municipal/county park, where overnight camping is typically
        prohibited even on otherwise-public ground)."""
        return (
            self.padus_status == "match"
            and self.padus_pub_access in OPEN_ACCESS_VALUES
            and self.padus_manager_type in DISPERSED_MANAGER_TYPES
        )


def _conn() -> sqlite3.Connection:
    db_path = _db_path()
    if not db_path.exists():
        raise FileNotFoundError(
            f"Dispersed DB not found at {db_path}. Run "
            "`python -m scripts.build_dispersed_db` to create it."
        )
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _row_to_pin(r: sqlite3.Row) -> Pin:
    return Pin(
        id=r["id"],
        name=r["name"],
        category=r["category"],
        lat=r["lat"],
        lon=r["lon"],
        date_verified=r["date_verified"],
        padus_status=r["padus_status"],
        padus_manager=r["padus_manager"],
        padus_manager_type=r["padus_manager_type"],
        padus_unit_name=r["padus_unit_name"],
        padus_designation=r["padus_designation"],
        padus_pub_access=r["padus_pub_access"],
    )


def search_radius(
    lat: float,
    lon: float,
    radius_miles: float = 30.0,
    categories: list[str] | None = None,
) -> list[tuple[Pin, float]]:
    """Find pins within ``radius_miles`` of (lat, lon), sorted nearest-first.

    Returns a list of ``(pin, distance_miles)`` tuples. The R*Tree pre-filter
    uses a degree-based bounding box (over-selecting at high latitudes is fine —
    the haversine pass corrects it), then the haversine post-filter trims to a
    true circle.

    ``categories`` is an optional whitelist (e.g. ``["Wild Camping",
    "Informal Campsite"]``); ``None`` means all categories.
    """
    # 1 degree latitude ≈ 69 miles. 1 degree longitude shrinks with cos(lat),
    # so over-pad east-west to be safe.
    dlat = radius_miles / 69.0
    dlon = radius_miles / (69.0 * max(math.cos(math.radians(lat)), 0.01))

    # Direct bbox filter on the pins table. We have an R*Tree but joining it
    # back to pins requires re-deriving the integer rtree-id from the TEXT
    # primary key, and at 32K rows the bbox prefilter on (lat, lon) is sub-ms
    # without it. Skip the rtree.
    sql = """
        SELECT * FROM pins
        WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
    """
    params: list = [lat - dlat, lat + dlat, lon - dlon, lon + dlon]

    if categories:
        placeholders = ",".join("?" * len(categories))
        sql += f" AND category IN ({placeholders})"
        params.extend(categories)

    with _conn() as c:
        rows = c.execute(sql, params).fetchall()

    out: list[tuple[Pin, float]] = []
    for r in rows:
        d = _haversine_miles(lat, lon, r["lat"], r["lon"])
        if d <= radius_miles:
            out.append((_row_to_pin(r), d))
    out.sort(key=lambda t: t[1])
    return out


def find_blacklist_pins_near(
    lat: float, lon: float, radius_meters: float = 200.0
) -> list[Pin]:
    """Return any 'Overnight Prohibited' pins within ``radius_meters``.

    Used as a filter: a candidate pin within this radius of a prohibited pin
    should be dropped or flagged.
    """
    radius_miles = radius_meters / 1609.34
    hits = search_radius(
        lat, lon, radius_miles=radius_miles, categories=["Overnight Prohibited"]
    )
    return [pin for pin, _ in hits]


def stats() -> dict[str, int]:
    """Quick counts for sanity-checking an ingest run."""
    with _conn() as c:
        rows = c.execute(
            "SELECT category, padus_status, COUNT(*) AS n "
            "FROM pins GROUP BY category, padus_status"
        ).fetchall()
    return {f"{r['category']}/{r['padus_status']}": r["n"] for r in rows}


if __name__ == "__main__":
    # Quick demo: search around a known point in northern MN.
    import json
    print(json.dumps(stats(), indent=2))
    print()
    print("--- Search 30mi around Ely, MN (47.9, -91.87) ---")
    for pin, dist in search_radius(47.9, -91.87, radius_miles=30)[:10]:
        flag = "✓" if pin.is_open_access_public_land else " "
        print(f"  {flag} {dist:5.1f}mi  [{pin.category}]  {pin.name[:40]:40}  "
              f"{pin.padus_manager or '-':6}  {pin.padus_pub_access or '-'}")
