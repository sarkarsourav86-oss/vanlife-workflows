"""One-off probe: take recent northern-MN iOverlander Wild Camping pins,
look each one up in PAD-US (ownership) and Overpass (nearest road),
print results so we can judge how well the iOverlander -> PAD-US -> OSM
stack performs end-to-end.

Run:
    python -m scripts.probe_dispersed_stack
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

PADUS = "https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/Manager_Name_PADUS/FeatureServer/0/query"
OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "vanlife-workflows-probe/0.1 (sourav.sarkar@ilmservice.com)"


def query_padus(lat: float, lon: float) -> dict | None:
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "Mang_Name,Mang_Type,Loc_Mang,Unit_Nm,Loc_Nm,Pub_Access,Des_Tp",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        r = httpx.get(PADUS, params=params, timeout=15, headers={"User-Agent": UA})
        feats = r.json().get("features", [])
        return feats[0].get("attributes") if feats else None
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def query_nearest_road(lat: float, lon: float, radius_m: int = 300) -> list[dict]:
    q = f"""
    [out:json][timeout:30];
    (way[highway](around:{radius_m},{lat},{lon}););
    out tags center;
    """
    try:
        r = httpx.post(OVERPASS, data={"data": q},
                       headers={"User-Agent": UA}, timeout=60)
        r.raise_for_status()
        return r.json().get("elements", [])
    except Exception as e:
        return [{"_error": f"{type(e).__name__}: {e}"}]


def main():
    src = Path("IOverlanderData/places20260404-15-7rje7n.json")
    data = json.loads(src.read_text(encoding="utf-8"))

    wild_mn = [
        p for p in data
        if p.get("place_category", {}).get("name") == "Wild Camping"
        and 46.0 <= p.get("location", {}).get("latitude", 0) <= 49.4
        and -97.2 <= p.get("location", {}).get("longitude", 0) <= -89.5
    ]
    wild_mn.sort(key=lambda p: p.get("date_verified", ""), reverse=True)

    candidates = wild_mn[:5]
    print(f"Probing {len(candidates)} freshest northern-MN Wild Camping pins\n")

    for p in candidates:
        name = p["name"]
        lat = p["location"]["latitude"]
        lon = p["location"]["longitude"]
        verified = p.get("date_verified", "")[:10]
        desc = (p.get("description") or "").replace("\r", " ").replace("\n", " ")[:160]

        print(f"=== {name} ===")
        print(f"  ({lat:.4f}, {lon:.4f})  verified {verified}")
        print(f"  notes: {desc!r}")

        pad = query_padus(lat, lon)
        if pad is None:
            print("  PAD-US: NO MATCH (private/municipal/unmapped)")
        elif "_error" in pad:
            err = pad["_error"]
            print(f"  PAD-US: ERROR {err}")
        else:
            unit = pad.get("Unit_Nm") or pad.get("Loc_Nm") or "(unnamed unit)"
            access = pad.get("Pub_Access", "-")
            manager = pad.get("Mang_Name", "-")
            des = pad.get("Des_Tp", "-")
            print(f"  PAD-US: {manager} - {unit}")
            print(f"          designation={des}, public_access={access}")

        time.sleep(0.5)

        ways = query_nearest_road(lat, lon, radius_m=300)
        if ways and "_error" in ways[0]:
            err = ways[0]["_error"]
            print(f"  Roads: ERROR {err}")
        elif not ways:
            print("  Roads: NONE within 300m  <<< red flag")
        else:
            seen = set()
            shown = 0
            for w in ways:
                t = w.get("tags", {})
                hw = t.get("highway", "-")
                sig = (hw, t.get("name"), t.get("surface"))
                if sig in seen:
                    continue
                seen.add(sig)
                surface = t.get("surface", "-")
                track = t.get("tracktype", "-")
                access_road = t.get("access", "-")
                name_road = t.get("name") or t.get("ref") or "(unnamed)"
                print(f"  Road: highway={hw:14}  surface={surface:10}  "
                      f"tracktype={track:7}  access={access_road:8}  "
                      f"name={name_road[:30]!r}")
                shown += 1
                if shown >= 3:
                    break

        print()
        time.sleep(2.5)


if __name__ == "__main__":
    main()
