"""Shared test fixtures.

The vPIC client is replaced through FastAPI's `dependency_overrides` rather
than by patching httpx or intercepting sockets. That keeps the tests fast and
deterministic, and it exercises the same injection seam the app uses in
production, so a wiring mistake fails here instead of at runtime.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_nhtsa_client, get_vpic_client
from app.main import create_app
from app.nhtsa import Recall, SafetyRating
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


def make_recall(campaign: str = "20V001000", **overrides: Any) -> Recall:
    """A benign recall. Tests override only the field under test."""
    fields: dict[str, Any] = {
        "campaign_number": campaign,
        "component": "EQUIPMENT",
        "summary": "Summary",
        "consequence": "Consequence",
        "remedy": "Remedy",
        "manufacturer": "Maker",
        "report_received_date": date(2015, 1, 1),
        "park_it": False,
        "park_outside": False,
        "over_the_air_update": False,
    }
    fields.update(overrides)
    return Recall(**fields)


def make_rating(**overrides: Any) -> SafetyRating:
    fields: dict[str, Any] = {
        "vehicle_id": 1,
        "vehicle_description": "2003 Honda Accord 2-DR",
        "overall": "5",
        "overall_front": "4",
        "overall_side": "5",
        "rollover": "4",
        "rollover_possibility": 0.12,
        "electronic_stability_control": "Standard",
        "forward_collision_warning": "Optional",
        "lane_departure_warning": "Optional",
        "complaints_count": 10,
        "recalls_count": 2,
        "investigation_count": 0,
    }
    fields.update(overrides)
    return SafetyRating(**fields)


class FakeNhtsaClient:
    """Stands in for NhtsaClient. Counts calls so cache behaviour is testable."""

    def __init__(self) -> None:
        self.recall_calls: list[tuple[str, str, str]] = []
        self.rating_calls: list[tuple[str, str, str]] = []
        self.recalls_result: list[Recall] = []
        self.rating_result: SafetyRating | None = None
        self.recalls_error: Exception | None = None
        self.rating_error: Exception | None = None

    async def recalls(self, model_year: str, make: str, model: str) -> list[Recall]:
        self.recall_calls.append((model_year, make, model))
        if self.recalls_error is not None:
            raise self.recalls_error
        return self.recalls_result

    async def safety_rating(
        self, model_year: str, make: str, model: str, body_hint: str = ""
    ) -> SafetyRating | None:
        self.rating_calls.append((model_year, make, model))
        if self.rating_error is not None:
            raise self.rating_error
        return self.rating_result


@pytest.fixture
def fake_nhtsa() -> FakeNhtsaClient:
    return FakeNhtsaClient()


@pytest.fixture
def fake_vpic() -> FakeVpicClient:
    return FakeVpicClient()


@pytest.fixture
def app(tmp_path, fake_vpic, fake_nhtsa):
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
    application.dependency_overrides[get_nhtsa_client] = lambda: fake_nhtsa
    return application


@pytest.fixture
def client(app):
    # Context manager form so startup/shutdown (and therefore the DB and HTTP
    # client lifecycle) actually run.
    with TestClient(app) as test_client:
        yield test_client
