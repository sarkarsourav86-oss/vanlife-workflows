"""Build the local dispersed-camping SQLite DB from an iOverlander JSON export.

Reads the JSON, enriches every pin with a PAD-US ownership lookup against the
Esri-hosted USGS mirror, and writes everything to ``dispersed.db``.

PAD-US calls run concurrently (default 10 in flight) — at ~250ms latency each,
the bottleneck is wall-clock, not server politeness; 10 in flight gets us
~20 pins/sec without 429s in our testing.

Two caches make re-runs cheap:
  - PAD-US results are keyed by lat/lng rounded to 3 decimals (~100m grid),
    so neighboring pins share a single network call.
  - Pins already enriched in a previous run (matched by id) are skipped unless
    ``--force`` is passed.

Usage:
    python -m scripts.build_dispersed_db                     # default JSON path
    python -m scripts.build_dispersed_db --src path/to.json
    python -m scripts.build_dispersed_db --bbox 46,-98,49.5,-89   # MN only
    python -m scripts.build_dispersed_db --limit 100             # smoke test
    python -m scripts.build_dispersed_db --force                 # re-enrich all
    python -m scripts.build_dispersed_db \\
        --categories "Wild Camping,Informal Campsite,Overnight Prohibited"
    python -m scripts.build_dispersed_db --concurrency 20

Tracks PAD-US calls through ``cost_tracker.log_api_call`` for consistency with
the rest of the codebase, even though PAD-US is free.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

from src.cost_tracker import log_api_call

PADUS_URL = (
    "https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/"
    "Manager_Name_PADUS/FeatureServer/0/query"
)
USER_AGENT = "vanlife-workflows/0.1 (sourav.sarkar@ilmservice.com)"
DEFAULT_SRC = Path("IOverlanderData/places20260404-15-7rje7n.json")
DEFAULT_DB = Path("dispersed.db")

# PAD-US fields we keep. Schema reference:
# https://www.usgs.gov/programs/gap-analysis-project/science/pad-us-data-overview
PADUS_OUT_FIELDS = "Mang_Name,Mang_Type,Loc_Mang,Unit_Nm,Loc_Nm,Pub_Access,Des_Tp"

DEFAULT_CONCURRENCY = 10


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pins (
            id TEXT PRIMARY KEY,
            name TEXT,
            category TEXT,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            date_verified TEXT,
            padus_status TEXT NOT NULL,
            padus_manager TEXT,
            padus_manager_type TEXT,
            padus_unit_name TEXT,
            padus_designation TEXT,
            padus_pub_access TEXT,
            ingested_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS pins_category_idx ON pins(category);
        CREATE INDEX IF NOT EXISTS pins_latlon_idx ON pins(lat, lon);

        CREATE VIRTUAL TABLE IF NOT EXISTS pins_rtree USING rtree(
            id,
            min_lat, max_lat,
            min_lon, max_lon
        );

        CREATE TABLE IF NOT EXISTS padus_cache (
            cell_key TEXT PRIMARY KEY,    -- '{lat3:.3f},{lon3:.3f}'
            status TEXT NOT NULL,         -- 'match' | 'no_match' | 'error'
            payload TEXT,                 -- JSON of the PAD-US attributes (or error msg)
            cached_at REAL NOT NULL
        );
    """)
    # rtree's `id` column is INTEGER; we use rowid trickery via a separate id column
    # in the join table. SQLite rtree requires INTEGER for the first column, so we
    # store a stable hash. Rebuild if the column type doesn't match expectations.
    # (The CREATE VIRTUAL TABLE above with `id` as the first col makes it INTEGER.
    # We'll cast our string id to int via a hash when inserting.)


def stable_id(name: str, lat: float, lon: float) -> str:
    """iOverlander records have no stable id; hash the tuple we trust."""
    h = hashlib.sha1(f"{name}|{lat:.5f}|{lon:.5f}".encode("utf-8")).hexdigest()
    return h[:16]


def rtree_int_id(string_id: str) -> int:
    """SQLite R*Tree wants an INTEGER first column. Map the 16-hex-char id to int.

    16 hex chars = 64 bits. SQLite INTEGER is 8 bytes signed; mask to 63 bits to
    avoid sign-bit weirdness.
    """
    return int(string_id, 16) & ((1 << 63) - 1)


def cell_key(lat: float, lon: float) -> str:
    return f"{round(lat, 3):.3f},{round(lon, 3):.3f}"


def cache_get(conn: sqlite3.Connection, lat: float, lon: float) -> tuple[str, dict | None] | None:
    """Return cached PAD-US result if present, else None."""
    key = cell_key(lat, lon)
    row = conn.execute(
        "SELECT status, payload FROM padus_cache WHERE cell_key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    status, payload = row
    attrs = json.loads(payload) if payload and status == "match" else None
    return status, attrs


def cache_put(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    status: str,
    attrs: dict | None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO padus_cache(cell_key, status, payload, cached_at) "
        "VALUES (?, ?, ?, ?)",
        (cell_key(lat, lon), status, json.dumps(attrs) if attrs else None, time.time()),
    )


async def lookup_padus_remote_async(
    client: httpx.AsyncClient, lat: float, lon: float
) -> tuple[str, dict | None]:
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": PADUS_OUT_FIELDS,
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        with log_api_call("padus", "Manager_Name_PADUS/query"):
            r = await client.get(PADUS_URL, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return "error", {"error": f"{type(e).__name__}: {e}"}

    feats = data.get("features", [])
    if not feats:
        return "no_match", None
    return "match", feats[0].get("attributes", {})


def upsert_pin(
    conn: sqlite3.Connection,
    pin_id: str,
    name: str,
    category: str,
    lat: float,
    lon: float,
    date_verified: str | None,
    pad_status: str,
    pad_attrs: dict | None,
) -> None:
    attrs = pad_attrs or {}
    conn.execute(
        """
        INSERT INTO pins(
            id, name, category, lat, lon, date_verified,
            padus_status, padus_manager, padus_manager_type,
            padus_unit_name, padus_designation, padus_pub_access,
            ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            category = excluded.category,
            lat = excluded.lat,
            lon = excluded.lon,
            date_verified = excluded.date_verified,
            padus_status = excluded.padus_status,
            padus_manager = excluded.padus_manager,
            padus_manager_type = excluded.padus_manager_type,
            padus_unit_name = excluded.padus_unit_name,
            padus_designation = excluded.padus_designation,
            padus_pub_access = excluded.padus_pub_access,
            ingested_at = excluded.ingested_at
        """,
        (
            pin_id, name, category, lat, lon, date_verified,
            pad_status,
            attrs.get("Mang_Name"),
            attrs.get("Mang_Type"),
            attrs.get("Unit_Nm") or attrs.get("Loc_Nm"),
            attrs.get("Des_Tp"),
            attrs.get("Pub_Access"),
            time.time(),
        ),
    )

    rid = rtree_int_id(pin_id)
    conn.execute("DELETE FROM pins_rtree WHERE id = ?", (rid,))
    conn.execute(
        "INSERT INTO pins_rtree(id, min_lat, max_lat, min_lon, max_lon) "
        "VALUES (?, ?, ?, ?, ?)",
        (rid, lat, lat, lon, lon),
    )


def existing_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT id FROM pins")}


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float] | None) -> bool:
    if bbox is None:
        return True
    min_lat, min_lon, max_lat, max_lon = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


async def enrich_one(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
) -> tuple[str, dict | None]:
    """Resolve PAD-US for (lat,lon) — cache hit returns immediately, miss
    grabs a semaphore slot and calls the API."""
    cached = cache_get(conn, lat, lon)
    if cached is not None:
        return cached
    async with sem:
        # Re-check cache: a parallel task may have populated it while we waited.
        cached = cache_get(conn, lat, lon)
        if cached is not None:
            return cached
        return await lookup_padus_remote_async(client, lat, lon)


async def run_ingest(
    candidates: list[tuple[str, str, dict, float, float]],
    conn: sqlite3.Connection,
    concurrency: int,
) -> None:
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=20, limits=limits
    ) as client:
        t0 = time.time()
        last_progress = t0
        progress_every = 50

        async def process(idx: int, pid: str, name: str, p: dict, lat: float, lon: float):
            status, attrs = await enrich_one(sem, client, conn, lat, lon)
            return idx, pid, name, p, lat, lon, status, attrs

        tasks = [
            asyncio.create_task(process(i, pid, name, p, lat, lon))
            for i, (pid, name, p, lat, lon) in enumerate(candidates, 1)
        ]

        completed = 0
        for fut in asyncio.as_completed(tasks):
            idx, pid, name, p, lat, lon, status, attrs = await fut
            cache_put(conn, lat, lon, status, attrs)
            category = (p.get("place_category") or {}).get("name") or "(uncategorized)"
            verified = p.get("date_verified") or None
            upsert_pin(conn, pid, name, category, lat, lon, verified, status, attrs)
            completed += 1
            if completed % progress_every == 0:
                conn.commit()
                now = time.time()
                rate = progress_every / max(now - last_progress, 0.001)
                last_progress = now
                remaining = len(candidates) - completed
                eta_sec = remaining / max(rate, 0.001)
                print(
                    f"  [{completed:5}/{len(candidates)}]  "
                    f"{rate:.1f} pins/s  ETA {eta_sec/60:.1f}min",
                    flush=True,
                )

        conn.commit()
        elapsed = time.time() - t0
        print(
            f"Done. {len(candidates)} pins in {elapsed:.1f}s "
            f"({len(candidates)/max(elapsed,0.001):.1f}/s)"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC,
                    help="Path to iOverlander places JSON")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help="Output SQLite path")
    ap.add_argument("--bbox", type=str, default=None,
                    help="Restrict to bbox 'min_lat,min_lon,max_lat,max_lon'")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N pins (smoke testing)")
    ap.add_argument("--force", action="store_true",
                    help="Re-enrich pins that already exist in the DB")
    ap.add_argument("--categories", type=str, default=None,
                    help="Comma-separated category whitelist "
                         "(e.g. 'Wild Camping,Informal Campsite,Overnight Prohibited')")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"Max concurrent PAD-US requests (default {DEFAULT_CONCURRENCY})")
    args = ap.parse_args()

    bbox = None
    if args.bbox:
        parts = [float(x) for x in args.bbox.split(",")]
        if len(parts) != 4:
            print("--bbox must be 'min_lat,min_lon,max_lat,max_lon'", file=sys.stderr)
            return 2
        bbox = tuple(parts)  # type: ignore[assignment]

    categories = None
    if args.categories:
        categories = {c.strip() for c in args.categories.split(",") if c.strip()}

    print(f"Loading {args.src} ...")
    pins_in = json.loads(args.src.read_text(encoding="utf-8"))
    print(f"  {len(pins_in)} total pins in source")

    conn = sqlite3.connect(args.db)
    init_schema(conn)
    conn.commit()
    seen_before = existing_ids(conn) if not args.force else set()
    if seen_before:
        print(f"  {len(seen_before)} already in DB; will skip unless --force")

    candidates = []
    skipped_bbox = 0
    skipped_existing = 0
    skipped_category = 0
    for p in pins_in:
        loc = p.get("location") or {}
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        if lat is None or lon is None:
            continue
        if not in_bbox(lat, lon, bbox):
            skipped_bbox += 1
            continue
        if categories is not None:
            cat = (p.get("place_category") or {}).get("name")
            if cat not in categories:
                skipped_category += 1
                continue
        name = (p.get("name") or "").strip() or "(unnamed)"
        pid = stable_id(name, lat, lon)
        if pid in seen_before:
            skipped_existing += 1
            continue
        candidates.append((pid, name, p, lat, lon))

    if args.limit:
        candidates = candidates[: args.limit]

    print(
        f"  to process: {len(candidates)}  "
        f"(skipped {skipped_bbox} outside bbox, "
        f"{skipped_category} wrong category, "
        f"{skipped_existing} already enriched)"
    )
    print(f"  concurrency: {args.concurrency}")

    asyncio.run(run_ingest(candidates, conn, args.concurrency))

    print()
    print("--- Final stats ---")
    rows = conn.execute(
        "SELECT category, padus_status, COUNT(*) FROM pins "
        "GROUP BY category, padus_status ORDER BY 1, 2"
    ).fetchall()
    for cat, status, n in rows:
        print(f"  {cat:25}  {status:10}  {n}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
