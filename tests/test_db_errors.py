"""Storage failures surface as 503 with something actionable in the body.

Motivated by a real incident: the cache file ended up owned by another user,
so the process could read it but not write it. Cached VINs answered 200 and
only cache *misses* failed -- with a bare "Internal Server Error" that said
nothing about permissions. These tests pin the fix.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_cache
from tests.conftest import SAMPLE_VIN


class BrokenCache:
    """A cache whose every operation fails the way SQLite fails."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            raise self._error

        return fail


def client_with_cache_error(app, error: Exception) -> TestClient:
    app.dependency_overrides[get_cache] = lambda: BrokenCache(error)
    return TestClient(app)


def test_readonly_database_is_503_naming_permissions(app):
    error = sqlite3.OperationalError("attempt to write a readonly database")

    with client_with_cache_error(app, error) as client:
        response = client.get(f"/lookup?vin={SAMPLE_VIN}")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "not writable" in detail
    assert "permissions" in detail


def test_locked_database_is_503_and_invites_a_retry(app):
    error = sqlite3.OperationalError("database is locked")

    with client_with_cache_error(app, error) as client:
        response = client.get(f"/lookup?vin={SAMPLE_VIN}")

    assert response.status_code == 503
    assert "retry" in response.json()["detail"].lower()
    assert response.headers["retry-after"] == "1"


def test_unknown_operational_error_still_gets_a_usable_message(app):
    error = sqlite3.OperationalError("disk I/O error")

    with client_with_cache_error(app, error) as client:
        response = client.get(f"/lookup?vin={SAMPLE_VIN}")

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]


def test_the_error_body_does_not_leak_the_database_path(app):
    """The exception text can name the file; the response should not."""
    error = sqlite3.OperationalError("unable to open database file /srv/secret/vin_cache.db")

    with client_with_cache_error(app, error) as client:
        response = client.get(f"/lookup?vin={SAMPLE_VIN}")

    assert "/srv/secret" not in response.text


def test_programming_errors_are_not_dressed_up_as_retryable(app):
    """A bad statement is our bug. A 503 would invite a pointless retry."""
    error = sqlite3.ProgrammingError("no such column: nonsense")

    with (
        client_with_cache_error(app, error) as client,
        pytest.raises(sqlite3.ProgrammingError),
    ):
        client.get(f"/lookup?vin={SAMPLE_VIN}")


def test_health_fails_loudly_when_the_database_is_gone(app):
    """A readiness probe that answers ok on a dead database is worse than none."""
    error = sqlite3.OperationalError("unable to open database file")

    with client_with_cache_error(app, error) as client:
        response = client.get("/health")

    assert response.status_code == 503
