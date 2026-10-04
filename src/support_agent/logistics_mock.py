"""Mock logistics carrier with deterministic scenarios keyed by tracking code.

Run with `uvicorn support_agent.logistics_mock:create_app --factory`. It stands in for
the external carrier, so it shares no code with the agent.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, status

# Same anchor as the seed, so tracking events line up with the seeded Orders.
ANCHOR = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

TIMEOUT = "timeout"
SERVER_ERROR = "server_error"

# (days from anchor, event status, description, location)
Event = tuple[float, str, str, str]

_POSTED = "posted", "Objeto postado", "São Paulo/SP"
_TRANSIT = "in_transit", "Objeto em trânsito para a unidade de destino", "Curitiba/PR"
_OUT = "out_for_delivery", "Objeto saiu para entrega", "Curitiba/PR"


def _track(status: str, *events: Event) -> dict[str, Any]:
    return {
        "status": status,
        "events": [
            {
                "at": (ANCHOR + timedelta(days=days)).isoformat(),
                "status": event_status,
                "description": description,
                "location": location,
            }
            for days, event_status, description, location in events
        ],
    }


SCENARIOS: dict[str, dict[str, Any] | str] = {
    # ORD-1001, delivered
    "BR100000001": _track(
        "delivered",
        (-17, *_POSTED),
        (-15, *_TRANSIT),
        (-13.2, *_OUT),
        (-13, "delivered", "Objeto entregue ao destinatário", "Curitiba/PR"),
    ),
    # ORD-1002, in transit
    "BR100000002": _track("in_transit", (-4, *_POSTED), (-1, *_TRANSIT)),
    # ORD-2002, delayed
    "BR200000002": _track(
        "delayed",
        (-27, *_POSTED),
        (-24, *_TRANSIT),
        (-16, "delayed", "Entrega atrasada por problema operacional", "Curitiba/PR"),
    ),
    # ORD-3002, delivered
    "BR300000002": _track(
        "delivered",
        (-57, *_POSTED),
        (-51, "delivered", "Objeto entregue ao destinatário", "Recife/PE"),
    ),
    # ORD-1004, lost
    "BR100000004": _track(
        "lost",
        (-10, *_POSTED),
        (-8, *_TRANSIT),
        (-3, "lost", "Objeto extraviado", "Curitiba/PR"),
    ),
    # ORD-2004, returned
    "BR200000004": _track(
        "returned",
        (-13, *_POSTED),
        (-9, "delivery_failed", "Destinatário ausente", "Porto Alegre/RS"),
        (-6, "returned", "Objeto devolvido ao remetente", "São Paulo/SP"),
    ),
    "BR300000003": TIMEOUT,  # ORD-3003
    "BR300000004": SERVER_ERROR,  # ORD-3004
    # ORD-3005 (BR300000005) is deliberately absent: the carrier answers 404.
}


def create_app(timeout_delay_seconds: float | None = None) -> FastAPI:
    delay = (
        timeout_delay_seconds
        if timeout_delay_seconds is not None
        else float(os.environ.get("TIMEOUT_DELAY_SECONDS", "30"))
    )
    app = FastAPI(title="Logistics carrier (mock)")

    @app.get("/tracking/{tracking_code}")
    async def tracking(tracking_code: str) -> dict[str, Any]:
        scenario = SCENARIOS.get(tracking_code)
        if scenario is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tracking code")
        if scenario == TIMEOUT:
            await asyncio.sleep(delay)
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "Carrier timed out")
        if isinstance(scenario, str):  # SERVER_ERROR
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Carrier error")
        return {"tracking_code": tracking_code, **scenario}

    return app
