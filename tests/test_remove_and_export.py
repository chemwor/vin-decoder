"""Behaviour of /remove and /export."""

from __future__ import annotations

import io

import pyarrow.parquet as pq

from app.export import PARQUET_SCHEMA
from tests.conftest import SAMPLE_VIN

OTHER_VIN = "5YJ3E1EA6PF384836"


def test_remove_reports_true_then_false(client):
    client.get("/lookup", params={"vin": SAMPLE_VIN})

    first = client.post("/remove", json={"vin": SAMPLE_VIN})
    assert first.status_code == 200
    assert first.json() == {"vin": SAMPLE_VIN, "cache_delete_success": True}

    # Deleting again is a successful request that deleted nothing.
    second = client.post("/remove", json={"vin": SAMPLE_VIN})
    assert second.status_code == 200
    assert second.json()["cache_delete_success"] is False


def test_remove_forces_a_refetch(client, fake_vpic):
    client.get("/lookup", params={"vin": SAMPLE_VIN})
    client.post("/remove", json={"vin": SAMPLE_VIN})

    response = client.get("/lookup", params={"vin": SAMPLE_VIN})
    assert response.json()["cached_result"] is False
    assert fake_vpic.calls == [SAMPLE_VIN, SAMPLE_VIN]


def test_remove_via_delete_verb(client):
    client.get("/lookup", params={"vin": SAMPLE_VIN})
    response = client.request("DELETE", "/remove", params={"vin": SAMPLE_VIN})
    assert response.json()["cache_delete_success"] is True


def test_remove_rejects_malformed_vin(client):
    assert client.post("/remove", json={"vin": "nope"}).status_code == 422


def test_export_of_empty_cache_is_still_valid_parquet(client):
    response = client.get("/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.apache.parquet"
    assert "attachment;" in response.headers["content-disposition"]

    table = pq.read_table(io.BytesIO(response.content))
    assert table.num_rows == 0
    # Schema must be identical whether or not there is data in it.
    assert table.schema.equals(PARQUET_SCHEMA)


def test_export_round_trips_cached_rows(client):
    client.get("/lookup", params={"vin": SAMPLE_VIN})
    client.get("/lookup", params={"vin": OTHER_VIN})

    table = pq.read_table(io.BytesIO(client.get("/export").content))
    assert table.num_rows == 2
    assert table.schema.equals(PARQUET_SCHEMA)

    rows = table.to_pylist()
    assert sorted(r["vin"] for r in rows) == sorted([SAMPLE_VIN, OTHER_VIN])
    assert all(r["make"] == "HONDA" for r in rows)
    assert all(r["fetched_at"] for r in rows)


def test_export_reflects_removals(client):
    client.get("/lookup", params={"vin": SAMPLE_VIN})
    client.get("/lookup", params={"vin": OTHER_VIN})
    client.post("/remove", json={"vin": SAMPLE_VIN})

    table = pq.read_table(io.BytesIO(client.get("/export").content))
    assert [r["vin"] for r in table.to_pylist()] == [OTHER_VIN]


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["cached_vins"] == 0
