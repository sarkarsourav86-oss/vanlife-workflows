"""One-shot MN state-parks availability checker.

Unlike `region_finder` (which creates Campflare alerts and waits for webhooks
to fire), this workflow is a *synchronous query*. The user asks "can I camp
at any MN state park on these dates?", we hit Campflare's bulk-availability
endpoint for our pinned park list, format the answer, and return.

No Campflare alerts created. No state. No webhooks. Just a search.

Why a hardcoded campground list (instead of a bbox search)? Campflare's
``/campgrounds/search`` endpoint *samples* — repeated identical bbox queries
return overlapping but non-equal subsets. limit=50 returns 25 MN state parks
each call, but the *which 25* rotates. limit>=100 silently caps at ~20. There
is no agency_id filter or pagination cursor. So a single sweep can never give
us deterministic "all MN state parks." Discovered 2026-04-27 via a multi-call
union; baking that union into a constant. Re-sweep by hand once a year.

Public surface:
    find_availability(start, nights) -> list[ParkAvailability]
    build_embed(results, start, nights) -> dict   # Discord embed
    parse_date(s) -> date | None
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..campflare import Campground, CampflareClient

# Pinned list of vanlife-friendly MN state-park campgrounds, harvested by a
# multi-call bbox union + by-name searches. Re-run the harvest if MN DNR adds
# new parks or you want to re-validate.
_MN_STATE_PARK_IDS: tuple[str, ...] = (
    "afton-state-park-campground-minnesotastateparks-050",
    "banning-campground-minnesotastateparks-987",
    "baptism-river-campground-minnesotastateparks-760",
    "bear-head-lake-campground-minnesotastateparks-810",
    "blue-mounds-campground-minnesotastateparks-814",
    "cascade-river-campground-minnesotastateparks-875",
    "chase-point-campground-minnesotastateparks-856",
    "crow-wing-campground-minnesotastateparks-822",
    "douglas-lodge-minnesotastateparks-765",
    "father-hennepin-state-park-campground-minnesotastateparks-824",
    "forestville-mystery-cave-state-park-main-campground-minnesotastateparks-878",
    "frontenac-state-park-campground-minnesotastateparks-879",
    "gooseberry-falls-campground-minnesotastateparks-990",
    "hayes-lake-campground-minnesotastateparks-925",
    "jay-cooke-state-park-campground-minnesotastateparks-884",
    "la-salle-lake-campground-minnesotastateparks-005",
    "lake-bemidji-campground-minnesotastateparks-841",
    "lake-carlos-state-park-campground-minnesotastateparks-795",
    "lake-ozawindib-minnesotastateparks-025",
    "lindbergh-campground-minnesotastateparks-988",
    "meadowbrook-area-campground-minnesotastateparks-871",
    "mille-lacs-kathio-petaga-campground-minnesotastateparks-904",
    "nerstrand-big-woods-state-park-campground-minnesotastateparks-074",
    "old-mill-state-park-campground-minnesotastateparks-909",
    "pine-ridge-campground-minnesotastateparks-839",
    "portsmouth-campground-minnesotastateparks-000",
    "riverview-campground-minnesotastateparks-807",
    "sakatah-lake-campground-minnesotastateparks-070",
    "sibley-state-park-campground-minnesotastateparks-776",
    "side-lake-campground-minnesotastateparks-903",
    "split-rock-creek-state-park-campground-minnesotastateparks-069",
    "temperance-river-state-park-campground-minnesotastateparks-755",
    "vermilion-ridge-campground-minnesotastateparks-040",
    "white-fox-campground-minnesotastateparks-906",
    "wild-river-campground-minnesotastateparks-895",
)

# Campflare's bulk_availability caps at 25 IDs per call. We have 35; batched.
_BULK_MAX = 25


@dataclass
class ParkAvailability:
    campground: Campground
    available_nights: list[date]   # which of the requested nights are bookable
    fully_open: bool               # True iff all requested nights have any inventory


# --- Search ------------------------------------------------------------------


def _fetch_park_metadata(client: CampflareClient, ids: list[str]) -> dict[str, Campground]:
    """Fetch ``Campground`` records for each pinned ID via the single-campground
    endpoint. We need the name + reservation_url for the embed; the bulk-
    availability response only carries IDs.
    """
    out: dict[str, Campground] = {}
    for cid in ids:
        try:
            out[cid] = client.get_campground(cid)
        except Exception as e:
            # Skip; surface in logs but don't break the whole query.
            print(f"[mn_parks] failed to load {cid}: {e}")
    return out


def find_availability(start: date, nights: int = 1) -> list[ParkAvailability]:
    """Return parks with availability data over [start, start+nights).

    A park is included iff *at least one* requested night shows a campsite as
    available. Parks with zero availability are dropped from the result.
    """
    if nights < 1:
        raise ValueError("nights must be >= 1")
    end = start + timedelta(days=nights)
    requested_nights = [start + timedelta(days=i) for i in range(nights)]

    ids = list(_MN_STATE_PARK_IDS)

    with CampflareClient() as client:
        # Batch the bulk_availability calls (cap=25 ids each).
        # Response shape: {"campgrounds": [{"campground_id", "campsite_availability": [
        #     {"campsite_id", "availability": {date: status}}, ...
        # ]}]}
        avail_by_id: dict[str, list[dict]] = {}
        for i in range(0, len(ids), _BULK_MAX):
            batch = ids[i : i + _BULK_MAX]
            resp = client.bulk_availability(
                campground_ids=batch, start_date=start, end_date=end,
            )
            for entry in resp.get("campgrounds") or []:
                cid = entry.get("campground_id")
                if cid:
                    avail_by_id[cid] = entry.get("campsite_availability") or []

        # Only fetch metadata for IDs that *had* availability — saves N-K
        # detail-endpoint calls when most parks are booked.
        ids_with_data = [
            cid for cid in ids
            if any(_is_available(s) for site in avail_by_id.get(cid, [])
                   for s in (site.get("availability") or {}).values())
        ]
        parks_meta = _fetch_park_metadata(client, ids_with_data)

    out: list[ParkAvailability] = []
    for cid in ids:
        cg = parks_meta.get(cid)
        if not cg:
            continue
        sites = avail_by_id.get(cid) or []
        nights_available = _extract_available_nights(sites, requested_nights)
        if nights_available:
            out.append(ParkAvailability(
                campground=cg,
                available_nights=nights_available,
                fully_open=(len(nights_available) == nights),
            ))
    out.sort(key=lambda p: p.campground.name or "")
    return out


def _extract_available_nights(
    sites: list[dict], requested_nights: list[date],
) -> list[date]:
    """Collect the dates with at least one bookable campsite across the
    campground's site list.

    Each site has shape ``{"campsite_id": ..., "availability": {date: status}}``
    where status is "available" / "reserved" / "first-come-first-serve" /
    "not-yet-released" / etc. A date is considered open if any site shows it
    as available."""
    requested_set = {d.isoformat() for d in requested_nights}
    found: set[str] = set()
    for site in sites:
        avail = site.get("availability") or {}
        if not isinstance(avail, dict):
            continue
        for d, status in avail.items():
            d_norm = d[:10]
            if d_norm in requested_set and _is_available(status):
                found.add(d_norm)
    return sorted(date.fromisoformat(d) for d in found)


def _is_available(status) -> bool:
    """Accept a few flavors of 'available' Campflare may return."""
    if isinstance(status, str):
        return status.lower() in ("available", "open", "first-come-first-serve")
    return False


# --- Date parsing ------------------------------------------------------------


def parse_date(s: str) -> date | None:
    """Accept YYYY-MM-DD, MM/DD/YYYY, or relative strings like 'next friday'.

    Keeping it minimal for now — relative-date parsing would call into
    dateparser/parsedatetime; out of scope for v1."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# --- Discord embed -----------------------------------------------------------


def _reservation_url(cg: Campground) -> str | None:
    """Pull the booking URL Campflare carries on the campground record."""
    d = cg.model_dump()
    return d.get("reservation_url") or d.get("booking_url")


def _city(cg: Campground) -> str | None:
    """Address city from location.address.city."""
    addr = (cg.model_dump().get("location") or {}).get("address") or {}
    return addr.get("city")


def _format_nights(nights_avail: list[date], requested: list[date]) -> str:
    """Render which nights are open. If all requested are open, return '*all'.
    Otherwise list mm/dd of the open ones."""
    if set(nights_avail) == set(requested):
        return "all requested"
    return ", ".join(d.strftime("%a %m/%d") for d in nights_avail)


# Discord caps embed.description at 4096 chars; leave headroom for safety.
_EMBED_DESC_LIMIT = 3800


def build_embeds(
    results: list[ParkAvailability], start: date, nights: int,
) -> list[dict]:
    """Build one or more Discord embed dicts.

    Splits across multiple embeds when the description would exceed Discord's
    4096-char limit. Discord allows up to 10 embeds per message; with 35 MN
    parks at ~120 chars/line, two embeds is plenty.
    """
    end = start + timedelta(days=nights)
    requested = [start + timedelta(days=i) for i in range(nights)]
    window_label = (
        start.strftime("%a %m/%d") if nights == 1
        else f"{start.strftime('%m/%d')} → {(end - timedelta(days=1)).strftime('%m/%d')} ({nights}n)"
    )
    title = f"🏞️ MN State Parks — {window_label}"

    if not results:
        return [{
            "title": title,
            "description": "No availability across MN state parks for that window.",
            "color": 0xE74C3C,
        }]

    lines: list[str] = []
    for r in results:
        cg = r.campground
        nights_str = _format_nights(r.available_nights, requested)
        marker = "✅" if r.fully_open else "⚠️"
        url = _reservation_url(cg)
        link = f" — [reserve]({url})" if url else ""
        city = _city(cg)
        loc_str = f" *(near {city})*" if city else ""
        lines.append(f"{marker} **{cg.name}**{loc_str} — {nights_str}{link}")

    # Greedy chunking: append lines to the current chunk until adding another
    # would overflow, then start a new chunk.
    chunks: list[list[str]] = [[]]
    cur_len = 0
    for line in lines:
        # +1 accounts for the joining newline
        if cur_len + len(line) + 1 > _EMBED_DESC_LIMIT and chunks[-1]:
            chunks.append([])
            cur_len = 0
        chunks[-1].append(line)
        cur_len += len(line) + 1

    footer_text = (
        f"{len(results)} parks with openings. ✅ = all nights open · "
        "⚠️ = some nights open."
    )
    embeds: list[dict] = []
    for i, chunk in enumerate(chunks):
        embed: dict = {
            "title": title if i == 0 else f"{title} (cont.)",
            "description": "\n".join(chunk),
            "color": 0x2ECC71,
        }
        # Footer only on the last embed so the count isn't repeated.
        if i == len(chunks) - 1:
            embed["footer"] = {"text": footer_text}
        embeds.append(embed)
    return embeds


# Back-compat shim — older imports still work, returns the first embed only.
def build_embed(
    results: list[ParkAvailability], start: date, nights: int,
) -> dict:
    return build_embeds(results, start, nights)[0]


# --- Local CLI ---------------------------------------------------------------


def main() -> None:
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--nights", type=int, default=1)
    args = parser.parse_args()

    start = parse_date(args.start)
    if not start:
        print(f"Could not parse date: {args.start!r}")
        return

    print(f"Searching MN state parks for {start} + {args.nights}n ...")
    results = find_availability(start, nights=args.nights)
    print(f"Parks with openings: {len(results)}")
    for r in results:
        print(f"  {'[full]' if r.fully_open else '[part]':6} "
              f"{r.campground.name[:50]:50} "
              f"{[d.strftime('%a %m/%d') for d in r.available_nights]}")


if __name__ == "__main__":
    main()
