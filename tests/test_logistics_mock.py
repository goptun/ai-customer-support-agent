import httpx
import pytest

from support_agent.logistics import (
    CarrierUnavailable,
    LogisticsClient,
    ShipmentStatus,
    TrackingNotFound,
)
from support_agent.logistics_mock import create_app


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://logistics.test",
        transport=httpx.ASGITransport(app=create_app(timeout_delay_seconds=5)),
    )


def client() -> LogisticsClient:
    return LogisticsClient(http_client(), timeout_seconds=0.1)


@pytest.mark.parametrize(
    ("tracking_code", "status", "latest_description"),
    [
        ("BR100000002", ShipmentStatus.IN_TRANSIT, "Objeto em trânsito para a unidade de destino"),
        ("BR200000002", ShipmentStatus.DELAYED, "Entrega atrasada por problema operacional"),
        ("BR100000001", ShipmentStatus.DELIVERED, "Objeto entregue ao destinatário"),
        ("BR100000004", ShipmentStatus.LOST, "Objeto extraviado"),
        ("BR200000004", ShipmentStatus.RETURNED, "Objeto devolvido ao remetente"),
    ],
)
async def test_tracking_scenarios_are_deterministic_by_code(
    tracking_code: str, status: ShipmentStatus, latest_description: str
) -> None:
    first = await client().track(tracking_code)
    again = await client().track(tracking_code)

    assert first == again
    assert first.status is status
    assert first.latest_event.description == latest_description


async def test_failure_scenarios() -> None:
    with pytest.raises(CarrierUnavailable):
        await client().track("BR300000003")  # timeout
    with pytest.raises(CarrierUnavailable):
        await client().track("BR300000004")  # 500
    with pytest.raises(TrackingNotFound):
        await client().track("BR300000005")  # 404
    with pytest.raises(TrackingNotFound):
        await client().track("BR999999999")  # unknown code


@pytest.mark.parametrize(
    ("tracking_code", "status", "latest_at"),
    [
        ("DEMO-TRANSIT-20261009-100002", ShipmentStatus.IN_TRANSIT, "2026-10-11"),
        ("DEMO-DELIVERED-20261004-100004", ShipmentStatus.DELIVERED, "2026-10-08"),
    ],
)
async def test_demo_codes_track_from_the_dispatch_date_they_carry(
    tracking_code: str, status: ShipmentStatus, latest_at: str
) -> None:
    tracking = await client().track(tracking_code)

    assert tracking.status is status
    assert tracking.latest_event.at.date().isoformat() == latest_at


async def test_malformed_demo_code_is_unknown() -> None:
    with pytest.raises(TrackingNotFound):
        await client().track("DEMO-TRANSIT-2026130-1")


async def test_health_answers() -> None:
    async with http_client() as http:
        response = await http.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
