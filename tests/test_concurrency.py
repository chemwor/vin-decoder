"""Concurrency behaviour: request coalescing on a cold cache.

Uses httpx.ASGITransport to drive the app in-process with real concurrency,
which TestClient's synchronous interface cannot do.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.db import VinCache
from app.dependencies import get_vpic_client
from app.main import create_app
from app.service import _single_flight
from app.vpic import DecodedVin
from tests.conftest import SAMPLE_RAW, SAMPLE_VIN


class SlowVpicClient:
    """Simulates upstream latency so requests genuinely overlap."""

    def __init__(self, delay: float = 0.05) -> None:
        self.calls: list[str] = []
        self.delay = delay

    async def decode(self, vin: str) -> DecodedVin:
        self.calls.append(vin)
        await asyncio.sleep(self.delay)
        return DecodedVin(vin, "HONDA", "Accord", "2003", "Coupe", dict(SAMPLE_RAW))


@pytest.fixture
def async_app(tmp_path):
    config = Settings(db_path=str(tmp_path / "concurrent.db"))
    app = create_app(config)
    # ASGITransport does not run lifespan, so wire state up by hand.
    app.state.cache = VinCache(config.db_path)
    app.state.vpic = None
    yield app
    app.state.cache.close()


@pytest.mark.asyncio
async def test_concurrent_misses_hit_upstream_once(async_app):
    vpic = SlowVpicClient()
    async_app.dependency_overrides[get_vpic_client] = lambda: vpic

    transport = httpx.ASGITransport(app=async_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            *[client.get("/lookup", params={"vin": SAMPLE_VIN}) for _ in range(10)]
        )

    assert all(r.status_code == 200 for r in responses)
    # The whole point: ten simultaneous cold requests, one upstream call.
    assert vpic.calls == [SAMPLE_VIN]

    cached_flags = [r.json()["cached_result"] for r in responses]
    assert cached_flags.count(False) == 1  # the one that actually fetched
    assert cached_flags.count(True) == 9


@pytest.mark.asyncio
async def test_distinct_vins_are_not_serialized(async_app):
    """Coalescing is per-VIN; different VINs must still run in parallel."""
    vpic = SlowVpicClient(delay=0.1)
    async_app.dependency_overrides[get_vpic_client] = lambda: vpic
    vins = [
        "1HGCM82633A004352",
        "5YJ3E1EA6PF384836",
        "1FTFW1ET9DFC10312",
        "1C4RJFBG2FC625797",
    ]

    transport = httpx.ASGITransport(app=async_app)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await asyncio.gather(*[client.get("/lookup", params={"vin": v}) for v in vins])
    elapsed = loop.time() - started

    assert sorted(vpic.calls) == sorted(vins)
    # Serialized would be ~0.4s; concurrent is ~0.1s.
    assert elapsed < 0.3


@pytest.mark.asyncio
async def test_single_flight_registry_does_not_leak(async_app):
    vpic = SlowVpicClient(delay=0.01)
    async_app.dependency_overrides[get_vpic_client] = lambda: vpic

    transport = httpx.ASGITransport(app=async_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await asyncio.gather(*[client.get("/lookup", params={"vin": SAMPLE_VIN}) for _ in range(5)])

    assert _single_flight.in_flight() == 0
