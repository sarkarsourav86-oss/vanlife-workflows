"""Typed Campflare API client.

Wraps the endpoints we need for Phase 1 workflows: searching campgrounds,
bulk-checking availability, creating/cancelling alerts, and fetching notices.

Docs: https://docs-v2.campflare.com/welcome
Base URL: https://api.campflare.com/v2
Auth: `Authorization: <api-key>` (no "Bearer " prefix).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from .cost_tracker import log_api_call

BASE_URL = "https://api.campflare.com/v2"

CampsiteKind = Literal[
    "standard", "rv", "tent-only", "cabin", "management",
    "group", "walk-to", "equestrian", "water-access",
]
Amenity = Literal[
    "toilets", "trash", "camp-store", "dump-station", "wifi",
    "pets-allowed", "showers", "fires-allowed", "water",
    "electric-hookups", "water-hookups", "sewer-hookups",
]
AvailabilityStatus = Literal[
    "available", "reserved", "closed",
    "first-come-first-serve", "not-yet-released", "unknown",
]


class BoundingBox(BaseModel):
    min_latitude: float
    max_latitude: float
    min_longitude: float
    max_longitude: float


class DateRange(BaseModel):
    starting_date: date
    ending_date: date | None = None
    nights: int = 1


class AvailabilityFilter(BaseModel):
    """Nested filter passed to /campgrounds/search and /alert/create.

    Note: Campflare uses `date_ranges` with `starting_date`/`ending_date`/`nights`
    per range — NOT a flat start_date/end_date/nights at this level.
    """
    date_ranges: list[DateRange]
    status: list[AvailabilityStatus] = Field(default_factory=lambda: ["available"])
    campsite_kinds: list[CampsiteKind] | None = None
    min_rv_length: float | None = None
    min_trailer_length: float | None = None


class CampgroundSearchRequest(BaseModel):
    query: str | None = None
    limit: int = 20
    bbox: BoundingBox | None = None
    land_id: str | None = None
    amenities: list[Amenity] | None = None
    campsite_kinds: list[CampsiteKind] | None = None
    minimum_rv_length: float | None = None
    big_rig_friendly: bool | None = None
    cell_service: list[Literal["verizon", "att", "t-mobile"]] | None = None
    status: Literal["open", "closed"] | None = None
    kind: Literal["established", "dispersed"] | None = None
    availability: AvailabilityFilter | None = None


class Campground(BaseModel):
    id: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    reservation_url: str | None = None

    model_config = {"extra": "allow"}  # tolerate fields we haven't modeled yet


class Campsite(BaseModel):
    id: str
    name: str
    kind: str | None = None
    kind_listed: str | None = None
    loop_name: str | None = None
    electric_hookups: bool | None = None
    water_hookups: bool | None = None
    sewer_hookups: bool | None = None
    ada_accessible: bool | None = None
    driveway_length: float | None = None
    max_rv_length: float | None = None
    pull_through: bool | None = None
    max_people: int | None = None
    price: dict | None = None
    photos: list[dict] = Field(default_factory=list)

    model_config = {"extra": "allow"}

    def photo_url(self) -> str | None:
        """Return best available photo URL: Campflare CDN first, then original_url."""
        for p in self.photos:
            url = p.get("large_url") or p.get("medium_url") or p.get("small_url") or p.get("original_url")
            if url:
                return url
        return None


class CreateAlertRequest(BaseModel):
    parameters: AvailabilityFilter
    campground_ids: list[str] = Field(max_length=12)
    metadata: dict | None = None
    webhook_override_url: str | None = None


class CampflareClient:
    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        self.api_key = api_key or os.environ["CAMPFLARE_API_KEY"]
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _post(self, path: str, json: dict) -> dict:
        with log_api_call("campflare", f"POST {path}"):
            r = self._client.post(path, json=json)
            r.raise_for_status()
            return r.json()

    def _get(self, path: str, params: dict | None = None) -> dict:
        with log_api_call("campflare", f"GET {path}"):
            r = self._client.get(path, params=params)
            r.raise_for_status()
            return r.json()

    def _delete(self, path: str) -> dict:
        with log_api_call("campflare", f"DELETE {path}"):
            r = self._client.delete(path)
            r.raise_for_status()
            return r.json() if r.content else {}

    def get_campground(self, campground_id: str) -> Campground:
        """GET /campground/{id} — fetch a single campground's full record."""
        data = self._get(f"/campground/{campground_id}")
        return Campground.model_validate(data)

    def get_campsites(self, campground_id: str) -> list[Campsite]:
        """GET /campground/{id}/campsites — all campsites with per-site attributes."""
        with log_api_call("campflare", f"GET /campground/{campground_id}/campsites"):
            r = self._client.get(f"/campground/{campground_id}/campsites")
            r.raise_for_status()
            return [Campsite.model_validate(c) for c in r.json()]

    def search_lands(self, query: str, limit: int = 5) -> list[dict]:
        """GET /lands/search — find parks/regions by name. Returns raw dicts with id, name, kind."""
        data = self._get("/lands/search", params={"query": query, "limit": limit})
        return data.get("lands") or []

    def search_campgrounds_by_land(self, land_id: str, limit: int = 20) -> list[Campground]:
        """POST /campgrounds/search filtered to a specific land (park/region)."""
        data = self._post("/campgrounds/search", {"land_id": land_id, "limit": limit})
        results = data.get("results") or data.get("campgrounds") or []
        return [Campground.model_validate(c) for c in results]

    def search_campgrounds(self, req: CampgroundSearchRequest) -> list[Campground]:
        """POST /campgrounds/search — at least one filter must be set."""
        payload = req.model_dump(exclude_none=True, mode="json")
        data = self._post("/campgrounds/search", payload)
        results = data.get("results", data.get("campgrounds", data))
        return [Campground.model_validate(c) for c in results]

    def bulk_availability(
        self,
        campground_ids: list[str],
        start_date: date,
        end_date: date,
    ) -> dict:
        """POST /campgrounds/availability — max 25 IDs per call.

        Despite the natural name, the endpoint is `/campgrounds/availability`
        (no `bulk-` prefix). `/campgrounds/bulk-availability` 404s.

        Returns ``{campgrounds: [{campground_id, campsite_availability: [
            {campsite_id, availability: {YYYY-MM-DD: "available"|"reserved"|...}}
        ]}]}``.
        """
        if len(campground_ids) > 25:
            raise ValueError("bulk_availability accepts at most 25 campground_ids")
        return self._post(
            "/campgrounds/availability",
            {
                "campground_ids": campground_ids,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )

    def create_alert(self, req: CreateAlertRequest) -> dict:
        """POST /alert/create — returns alert metadata including id."""
        return self._post("/alert/create", req.model_dump(exclude_none=True, mode="json"))

    def get_alert(self, alert_id: str) -> dict:
        """GET /alert/{id} — note the action-suffix scheme does NOT apply here."""
        return self._get(f"/alert/{alert_id}")

    def cancel_alert(self, alert_id: str) -> dict:
        """POST /alert/{id}/cancel — returns the alert with canceled_at set."""
        return self._post(f"/alert/{alert_id}/cancel", {})

    def test_alert(self, alert_id: str) -> dict:
        """POST /alert/{id}/test — sends a simulated webhook."""
        return self._post(f"/alert/{alert_id}/test", {})

    def search_notices(
        self,
        bbox: BoundingBox | None = None,
        point: tuple[float, float] | None = None,
        kind: list[Literal["weather", "fire", "closure", "safety", "access"]] | None = None,
        severity: list[Literal["info", "minor", "moderate", "severe", "extreme"]] | None = None,
    ) -> list[dict]:
        payload: dict = {}
        if bbox:
            payload["bbox"] = bbox.model_dump()
        if point:
            payload["point"] = f"{point[0]},{point[1]}"
        if kind:
            payload["kind"] = kind
        if severity:
            payload["severity"] = severity
        data = self._post("/notices/search", payload)
        return data.get("results", data.get("notices", []))
