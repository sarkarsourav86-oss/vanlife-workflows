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

    model_config = {"extra": "allow"}  # tolerate fields we haven't modeled yet


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

    def __exit__(self, *exc):
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
        """POST /campgrounds/bulk-availability — max 25 IDs per call."""
        if len(campground_ids) > 25:
            raise ValueError("bulk_availability accepts at most 25 campground_ids")
        return self._post(
            "/campgrounds/bulk-availability",
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
