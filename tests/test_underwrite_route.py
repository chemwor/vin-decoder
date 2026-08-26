"""The /underwrite endpoint, end to end through the app with fakes."""

from __future__ import annotations

from app.nhtsa import NhtsaUnavailable, NhtsaVehicleNotListed
from tests.conftest import SAMPLE_VIN, make_rating, make_recall


def test_returns_decode_plus_assessment(client, fake_nhtsa):
    fake_nhtsa.recalls_result = [make_recall()]
    fake_nhtsa.rating_result = make_rating()

    body = client.get(f"/underwrite?vin={SAMPLE_VIN}").json()

    assert body["vin"] == SAMPLE_VIN
    assert body["make"] == "HONDA"
    assert body["underwriting"]["open_recall_count"] == 1
    assert body["safety_rating"]["overall"] == "5"
    assert len(body["recalls"]) == 1


def test_do_not_drive_surfaces_as_block(client, fake_nhtsa):
    fake_nhtsa.recalls_result = [make_recall("22V123000", park_it=True)]
    body = client.get(f"/underwrite?vin={SAMPLE_VIN}").json()
    assert body["underwriting"]["decision"] == "BLOCK"


def test_post_form_matches_get(client, fake_nhtsa):
    fake_nhtsa.recalls_result = [make_recall()]
    from_get = client.get(f"/underwrite?vin={SAMPLE_VIN}").json()
    from_post = client.post("/underwrite", json={"vin": SAMPLE_VIN}).json()
    assert from_get["underwriting"]["decision"] == from_post["underwriting"]["decision"]


def test_malformed_vin_is_422(client):
    assert client.get("/underwrite?vin=TOOSHORT").status_code == 422


# --- degradation -----------------------------------------------------------


def test_recall_outage_does_not_fail_the_request(client, fake_nhtsa):
    """An NHTSA outage makes the picture incomplete, not the request invalid."""
    fake_nhtsa.recalls_error = NhtsaUnavailable("boom")

    response = client.get(f"/underwrite?vin={SAMPLE_VIN}")

    assert response.status_code == 200
    body = response.json()
    assert body["underwriting"]["decision"] == "INSUFFICIENT_DATA"
    assert body["data_gaps"]


def test_rating_outage_still_assesses_recalls(client, fake_nhtsa):
    fake_nhtsa.rating_error = NhtsaUnavailable("boom")
    fake_nhtsa.recalls_result = [make_recall("22V123000", park_it=True)]

    body = client.get(f"/underwrite?vin={SAMPLE_VIN}").json()

    assert body["underwriting"]["decision"] == "BLOCK"
    assert body["safety_rating"] is None
    assert body["data_gaps"]


def test_unlisted_vehicle_explains_itself(client, fake_nhtsa):
    """The gap must not read like an outage -- the fix is a different name."""
    fake_nhtsa.recalls_error = NhtsaVehicleNotListed("nope")

    body = client.get(f"/underwrite?vin={SAMPLE_VIN}").json()

    assert body["underwriting"]["decision"] == "INSUFFICIENT_DATA"
    assert "factory model codes" in " ".join(body["data_gaps"])


# --- caching ---------------------------------------------------------------


def test_profile_is_fetched_once_per_year_make_model(client, fake_nhtsa):
    fake_nhtsa.recalls_result = [make_recall()]

    client.get(f"/underwrite?vin={SAMPLE_VIN}")
    client.get(f"/underwrite?vin={SAMPLE_VIN}")

    assert len(fake_nhtsa.recall_calls) == 1


def test_different_vins_of_same_vehicle_share_one_profile(client, fake_nhtsa):
    """The reason the profile cache is keyed by vehicle rather than by VIN."""
    fake_nhtsa.recalls_result = [make_recall()]

    client.get(f"/underwrite?vin={SAMPLE_VIN}")
    client.get("/underwrite?vin=1HGCM82633A999999")

    assert len(fake_nhtsa.recall_calls) == 1


def test_failure_is_not_cached_so_the_next_call_retries(client, fake_nhtsa):
    """A blip must not pin INSUFFICIENT_DATA for the whole TTL."""
    fake_nhtsa.recalls_error = NhtsaUnavailable("boom")
    assert (
        client.get(f"/underwrite?vin={SAMPLE_VIN}").json()["underwriting"]["decision"]
        == "INSUFFICIENT_DATA"
    )

    fake_nhtsa.recalls_error = None
    fake_nhtsa.recalls_result = []

    body = client.get(f"/underwrite?vin={SAMPLE_VIN}").json()

    assert body["underwriting"]["decision"] == "CLEAR"
    assert len(fake_nhtsa.recall_calls) == 2


def test_stored_null_recalls_never_rehydrate_as_clean():
    """Defensive: rows written before failures stopped being cached.

    Reading a stored NULL back as [] would turn a vehicle nobody checked into
    a cleared one, which is the failure mode this whole feature guards against.
    """
    from datetime import UTC, datetime

    from app.db import CachedProfile
    from app.service import _from_cached_profile

    stored = CachedProfile(
        profile_key="2015|HARLEY-DAVIDSON|STREET GLIDE",
        model_year="2015",
        make="HARLEY-DAVIDSON",
        model="Street Glide",
        recalls=None,
        ratings=None,
        fetched_at=datetime.now(UTC),
    )

    recalls, _rating, gaps = _from_cached_profile(stored)

    assert recalls is None
    assert gaps


def test_cached_result_flag_reflects_both_caches(client, fake_nhtsa):
    fake_nhtsa.recalls_result = []
    assert client.get(f"/underwrite?vin={SAMPLE_VIN}").json()["cached_result"] is False
    assert client.get(f"/underwrite?vin={SAMPLE_VIN}").json()["cached_result"] is True


# --- mechanical projection -------------------------------------------------


def test_mechanical_specs_come_from_the_stored_decode(client, fake_vpic, fake_nhtsa):
    from app.vpic import DecodedVin

    fake_vpic.result = DecodedVin(
        vin=SAMPLE_VIN,
        make="HONDA",
        model="Accord",
        model_year="2003",
        body_class="Coupe",
        raw={"EngineHP": "240", "BrakeSystemType": "Hydraulic", "Doors": "2"},
    )

    body = client.get(f"/underwrite?vin={SAMPLE_VIN}").json()

    powertrain = {i["label"]: i["value"] for i in body["mechanical"]["powertrain"]}
    safety = {i["label"]: i["value"] for i in body["mechanical"]["safety_equipment"]}
    assert powertrain["Horsepower"] == "240"
    assert safety["Brake system"] == "Hydraulic"


def test_risk_profile_is_included_and_separate_from_the_decision(client, fake_vpic, fake_nhtsa):
    """Classification travels with the decision but must not move it."""
    from app.vpic import DecodedVin

    fake_vpic.result = DecodedVin(
        vin=SAMPLE_VIN,
        make="TESLA",
        model="Model 3",
        model_year="2023",
        body_class="Sedan/Saloon",
        raw={
            "VehicleType": "PASSENGER CAR",
            "ElectrificationLevel": "BEV (Battery Electric Vehicle)",
            "BatteryType": "Lithium-Ion/Li-Ion",
        },
    )
    fake_nhtsa.recalls_result = []

    body = client.get(f"/underwrite?vin={SAMPLE_VIN}").json()

    assert body["risk_profile"]["claims_routing"]["queue"] == "PASSENGER_AUTO"
    assert body["risk_profile"]["energy_source"]["kind"] == "BEV"
    # Being an EV is a characteristic, not a defect: the decision stays CLEAR.
    assert body["underwriting"]["decision"] == "CLEAR"


def test_health_reports_profile_count(client, fake_nhtsa):
    fake_nhtsa.recalls_result = []
    client.get(f"/underwrite?vin={SAMPLE_VIN}")
    assert client.get("/health").json()["cached_profiles"] == 1
