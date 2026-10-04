"""Client for the logistics carrier's tracking API."""

import asyncio
import enum
from datetime import datetime

import httpx
from pydantic import BaseModel, Field, ValidationError


class ShipmentStatus(enum.StrEnum):
    IN_TRANSIT = "in_transit"
    DELAYED = "delayed"
    DELIVERED = "delivered"
    LOST = "lost"
    RETURNED = "returned"


class TrackingEvent(BaseModel):
    at: datetime
    status: str
    description: str
    location: str | None = None


class Tracking(BaseModel):
    status: ShipmentStatus
    latest_event: TrackingEvent


class _TrackingResponse(BaseModel):
    status: ShipmentStatus
    events: list[TrackingEvent] = Field(min_length=1)


class CarrierUnavailable(Exception):
    """The carrier timed out, failed or answered something we can't read."""


class TrackingNotFound(Exception):
    """The carrier has no tracking for this code."""


class LogisticsClient:
    def __init__(self, http: httpx.AsyncClient, timeout_seconds: float) -> None:
        self._http = http
        self._timeout = timeout_seconds

    async def track(self, tracking_code: str) -> Tracking:
        try:
            # A total deadline, not httpx's per-operation timeouts: a slow trickle still fails.
            async with asyncio.timeout(self._timeout):
                response = await self._http.get(f"/tracking/{tracking_code}")
        except (TimeoutError, httpx.HTTPError) as exc:
            raise CarrierUnavailable(str(exc)) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise TrackingNotFound(tracking_code)
        if response.status_code != httpx.codes.OK:
            raise CarrierUnavailable(f"carrier answered {response.status_code}")
        try:
            body = _TrackingResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise CarrierUnavailable("unreadable carrier response") from exc
        return Tracking(status=body.status, latest_event=max(body.events, key=lambda e: e.at))
