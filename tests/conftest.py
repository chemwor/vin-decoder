"""Shared test fixtures.

The vPIC client is replaced through FastAPI's `dependency_overrides` rather
than by patching httpx or intercepting sockets. That keeps the tests fast and
deterministic, and it exercises the same injection seam the app uses in
production, so a wiring mistake fails here instead of at runtime.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_vpic_client
from app.main import create_app
from app.vpic import DecodedVin

SAMPLE_VIN = "1HGCM82633A004352"

SAMPLE_RAW: dict[str, Any] = {
    "VIN": SAMPLE_VIN,
    "Make": "HONDA",
    "Model": "Accord",
    "ModelYear": "2003",
    "BodyClass": "Coupe",
    "ErrorCode": "0",
    "ErrorText": "0 - VIN decoded clean. Check Digit (9th position) is correct",
}


class FakeVpicClient:
    """Stands in for VpicClient. Records calls; returns canned data or raises."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.raise_error: Exception | None = None
        self.result: DecodedVin | None = None

    async def decode(self, vin: str) -> DecodedVin:
        self.calls.append(vin)
        if self.raise_error is not None:
            raise self.raise_error
        if self.result is not None:
            return self.result
        return DecodedVin(
            vin=vin,
            make="HONDA",
            model="Accord",
            model_year="2003",
            body_class="Coupe",
            raw=dict(SAMPLE_RAW, VIN=vin),
        )


@pytest.fixture
def fake_vpic() -> FakeVpicClient:
    return FakeVpicClient()


@pytest.fixture
def app(tmp_path, fake_vpic):
    """A fully wired app with a throwaway on-disk SQLite file."""
    config = Settings(
        db_path=str(tmp_path / "test_cache.db"),
        vpic_base_url="http://vpic.test/api/vehicles",
        vpic_timeout_seconds=1.0,
        vpic_max_retries=0,
        cache_ttl_seconds=0,
        strict_vin_charset=False,
    )
    application = create_app(config)
    application.dependency_overrides[get_vpic_client] = lambda: fake_vpic
    return application


@pytest.fixture
def client(app):
    # Context manager form so startup/shutdown (and therefore the DB and HTTP
    # client lifecycle) actually run.
    with TestClient(app) as test_client:
        yield test_client
