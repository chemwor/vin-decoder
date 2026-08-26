"""Behaviour of /lookup: validation, cache-aside flow, upstream failures."""

from __future__ import annotations

import pytest

from app.vpic import VinNotDecodable, VpicUnavailable
from tests.conftest import SAMPLE_VIN


def test_miss_then_hit_calls_upstream_once(client, fake_vpic):
    first = client.get("/lookup", params={"vin": SAMPLE_VIN})
    assert first.status_code == 200
    assert first.json() == {
        "vin": SAMPLE_VIN,
        "make": "HONDA",
        "model": "Accord",
        "model_year": "2003",
        "body_class": "Coupe",
        "cached_result": False,
    }

    second = client.get("/lookup", params={"vin": SAMPLE_VIN})
    assert second.status_code == 200
    assert second.json()["cached_result"] is True

    # The whole point of the cache: one upstream call for two requests.
    assert fake_vpic.calls == [SAMPLE_VIN]


def test_post_and_get_are_equivalent(client, fake_vpic):
    client.post("/lookup", json={"vin": SAMPLE_VIN})
    posted = client.post("/lookup", json={"vin": SAMPLE_VIN}).json()
    got = client.get("/lookup", params={"vin": SAMPLE_VIN}).json()
    assert posted == got
    assert fake_vpic.calls == [SAMPLE_VIN]


def test_vin_is_normalized_before_use_as_cache_key(client, fake_vpic):
    """Lowercase and padded input must not create a second cache row."""
    client.get("/lookup", params={"vin": SAMPLE_VIN.lower()})
    response = client.post("/lookup", json={"vin": f"  {SAMPLE_VIN}  "})

    assert response.json()["vin"] == SAMPLE_VIN
    assert response.json()["cached_result"] is True
    assert fake_vpic.calls == [SAMPLE_VIN]


@pytest.mark.parametrize(
    "bad_vin",
    [
        "",
        "TOOSHORT",
        "1HGCM82633A00435",  # 16 chars
        "1HGCM82633A0043525",  # 18 chars
        "1HGCM82633A00435!",  # non-alphanumeric
        "1HGCM82633A 04352",  # embedded space
    ],
)
def test_malformed_vin_is_rejected_on_both_verbs(client, fake_vpic, bad_vin):
    assert client.get("/lookup", params={"vin": bad_vin}).status_code == 422
    assert client.post("/lookup", json={"vin": bad_vin}).status_code == 422
    # Nothing malformed should ever reach the upstream API.
    assert fake_vpic.calls == []


def test_missing_vin_parameter_is_a_422(client):
    assert client.get("/lookup").status_code == 422
    assert client.post("/lookup", json={}).status_code == 422


def test_upstream_outage_returns_502_and_caches_nothing(client, fake_vpic):
    fake_vpic.raise_error = VpicUnavailable("connection refused")

    response = client.get("/lookup", params={"vin": SAMPLE_VIN})
    assert response.status_code == 502
    assert "unavailable" in response.json()["detail"].lower()

    # A failed fetch must not poison the cache with an empty row.
    assert client.get("/health").json()["cached_vins"] == 0


def test_undecodable_vin_returns_422_and_caches_nothing(client, fake_vpic):
    fake_vpic.raise_error = VinNotDecodable(SAMPLE_VIN, "11 - Incorrect Model Year")

    response = client.get("/lookup", params={"vin": SAMPLE_VIN})
    assert response.status_code == 422
    assert response.json()["vin"] == SAMPLE_VIN

    assert client.get("/health").json()["cached_vins"] == 0


def test_recovery_after_outage(client, fake_vpic):
    """A transient upstream failure must not leave the VIN permanently broken."""
    fake_vpic.raise_error = VpicUnavailable("timeout")
    assert client.get("/lookup", params={"vin": SAMPLE_VIN}).status_code == 502

    fake_vpic.raise_error = None
    assert client.get("/lookup", params={"vin": SAMPLE_VIN}).status_code == 200
