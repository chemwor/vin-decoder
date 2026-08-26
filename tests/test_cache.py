"""Unit tests for the SQLite cache layer, independent of HTTP."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db import VinCache
from tests.conftest import SAMPLE_RAW, SAMPLE_VIN


def make_cache() -> VinCache:
    return VinCache(":memory:")


def test_get_missing_returns_none():
    assert make_cache().get(SAMPLE_VIN) is None


def test_upsert_then_get_round_trips_including_raw_payload():
    cache = make_cache()
    cache.upsert(SAMPLE_VIN, "HONDA", "Accord", "2003", "Coupe", SAMPLE_RAW)

    entry = cache.get(SAMPLE_VIN)
    assert entry is not None
    assert (entry.make, entry.model, entry.model_year, entry.body_class) == (
        "HONDA",
        "Accord",
        "2003",
        "Coupe",
    )
    assert entry.raw["ErrorCode"] == "0"
    assert entry.fetched_at.tzinfo is not None


def test_upsert_is_idempotent_and_updates_in_place():
    cache = make_cache()
    cache.upsert(SAMPLE_VIN, "HONDA", "Accord", "2003", "Coupe", SAMPLE_RAW)
    cache.upsert(SAMPLE_VIN, "HONDA", "Accord", "2003", "Sedan/Saloon", SAMPLE_RAW)

    assert cache.count() == 1
    assert cache.get(SAMPLE_VIN).body_class == "Sedan/Saloon"


def test_delete_returns_whether_a_row_existed():
    cache = make_cache()
    cache.upsert(SAMPLE_VIN, "HONDA", "Accord", "2003", "Coupe", SAMPLE_RAW)

    assert cache.delete(SAMPLE_VIN) is True
    assert cache.delete(SAMPLE_VIN) is False
    assert cache.get(SAMPLE_VIN) is None


def test_ttl_of_zero_never_expires():
    cache = make_cache()
    old = datetime.now(UTC) - timedelta(days=3650)
    entry = cache.upsert(SAMPLE_VIN, "HONDA", "Accord", "2003", "Coupe", SAMPLE_RAW, fetched_at=old)
    assert entry.is_expired(ttl_seconds=0) is False


def test_ttl_expires_old_entries():
    cache = make_cache()
    old = datetime.now(UTC) - timedelta(seconds=120)
    entry = cache.upsert(SAMPLE_VIN, "HONDA", "Accord", "2003", "Coupe", SAMPLE_RAW, fetched_at=old)
    assert entry.is_expired(ttl_seconds=60) is True
    assert entry.is_expired(ttl_seconds=600) is False


def test_all_rows_is_ordered_and_column_scoped():
    cache = make_cache()
    cache.upsert("ZZZZZZZZZZZZZZZZZ", "B", "b", "2020", "SUV", SAMPLE_RAW)
    cache.upsert("AAAAAAAAAAAAAAAAA", "A", "a", "2021", "Sedan", SAMPLE_RAW)

    rows = cache.all_rows()
    assert [r["vin"] for r in rows] == ["AAAAAAAAAAAAAAAAA", "ZZZZZZZZZZZZZZZZZ"]
    # raw_json is intentionally excluded from the export surface.
    assert "raw_json" not in rows[0]


def test_survives_reopen(tmp_path):
    path = str(tmp_path / "persist.db")
    cache = VinCache(path)
    cache.upsert(SAMPLE_VIN, "HONDA", "Accord", "2003", "Coupe", SAMPLE_RAW)
    cache.close()

    reopened = VinCache(path)
    assert reopened.get(SAMPLE_VIN).make == "HONDA"
