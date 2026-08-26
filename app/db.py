"""SQLite cache layer.

Plain `sqlite3` rather than an ORM. The whole persistence surface is four
statements against one table; an ORM would add a dependency and a mapping
layer without removing any of the code that actually matters here.

Concurrency model: one connection shared by the process, guarded by a lock.
SQLite serializes writes anyway, and every query here is a primary-key hit on
a local file (microseconds), so contention is not the bottleneck. It also
means `:memory:` works for tests, which a connection-per-call design does not.
See NOTES.md for what changes when this needs to scale out.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS vin_cache (
    vin         TEXT PRIMARY KEY,
    make        TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    model_year  TEXT NOT NULL DEFAULT '',
    body_class  TEXT NOT NULL DEFAULT '',
    raw_json    TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

-- Recalls and safety ratings are keyed by year/make/model, not by VIN, so
-- they get their own table. One row serves every VIN that decodes to the same
-- vehicle, which is the whole reason this is worth caching: a fleet upload of
-- 500 Ford Escapes is one NHTSA fetch, not 500.
CREATE TABLE IF NOT EXISTS vehicle_profile_cache (
    profile_key  TEXT PRIMARY KEY,
    model_year   TEXT NOT NULL,
    make         TEXT NOT NULL,
    model        TEXT NOT NULL,
    recalls_json TEXT NOT NULL,
    ratings_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);
"""

# Column order used by /export. Kept explicit so the parquet schema is stable
# even if the table later grows columns.
EXPORT_COLUMNS = (
    "vin",
    "make",
    "model",
    "model_year",
    "body_class",
    "fetched_at",
)


@dataclass(frozen=True)
class CachedVin:
    vin: str
    make: str
    model: str
    model_year: str
    body_class: str
    fetched_at: datetime
    raw: dict[str, Any]

    def is_expired(self, ttl_seconds: int, now: datetime | None = None) -> bool:
        """TTL of 0 (the default) means entries never expire."""
        if ttl_seconds <= 0:
            return False
        now = now or datetime.now(UTC)
        return now - self.fetched_at > timedelta(seconds=ttl_seconds)


@dataclass(frozen=True)
class CachedProfile:
    """Cached recalls + safety ratings for one year/make/model."""

    profile_key: str
    model_year: str
    make: str
    model: str
    recalls: list[Any] | None
    ratings: dict[str, Any] | None
    fetched_at: datetime

    def is_expired(self, ttl_seconds: int, now: datetime | None = None) -> bool:
        """TTL of 0 means entries never expire."""
        if ttl_seconds <= 0:
            return False
        now = now or datetime.now(UTC)
        return now - self.fetched_at > timedelta(seconds=ttl_seconds)


def profile_key(model_year: str, make: str, model: str) -> str:
    """Stable cache key. Upper-cased so casing differences collapse to one row."""
    return f"{model_year.strip()}|{make.strip().upper()}|{model.strip().upper()}"


class VinCache:
    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL lets readers proceed during a write; NORMAL sync is the
            # right durability tradeoff for a rebuildable cache.
            if db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- reads -----------------------------------------------------------

    def get(self, vin: str) -> CachedVin | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM vin_cache WHERE vin = ?", (vin,)).fetchone()
        return _row_to_cached(row) if row else None

    def all_rows(self) -> list[dict[str, Any]]:
        """Every cached entry, as plain dicts in EXPORT_COLUMNS order."""
        cols = ", ".join(EXPORT_COLUMNS)
        with self._lock:
            rows = self._conn.execute(f"SELECT {cols} FROM vin_cache ORDER BY vin").fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM vin_cache").fetchone()[0]

    # ---- writes ----------------------------------------------------------

    def upsert(
        self,
        vin: str,
        make: str,
        model: str,
        model_year: str,
        body_class: str,
        raw: dict[str, Any],
        fetched_at: datetime | None = None,
    ) -> CachedVin:
        """Insert or refresh a row.

        UPSERT rather than DELETE+INSERT so a concurrent request that lost the
        race just overwrites with an identical decode instead of erroring.
        """
        ts = fetched_at or datetime.now(UTC)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO vin_cache
                    (vin, make, model, model_year, body_class, raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vin) DO UPDATE SET
                    make       = excluded.make,
                    model      = excluded.model,
                    model_year = excluded.model_year,
                    body_class = excluded.body_class,
                    raw_json   = excluded.raw_json,
                    fetched_at = excluded.fetched_at
                """,
                (
                    vin,
                    make,
                    model,
                    model_year,
                    body_class,
                    json.dumps(raw, separators=(",", ":")),
                    ts.isoformat(),
                ),
            )
            self._conn.commit()
        return CachedVin(vin, make, model, model_year, body_class, ts, raw)

    def delete(self, vin: str) -> bool:
        """Returns True only if a row actually existed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM vin_cache WHERE vin = ?", (vin,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---- vehicle profiles (recalls + safety ratings) ---------------------

    def get_profile(self, key: str) -> CachedProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM vehicle_profile_cache WHERE profile_key = ?", (key,)
            ).fetchone()
        return _row_to_profile(row) if row else None

    def upsert_profile(
        self,
        key: str,
        model_year: str,
        make: str,
        model: str,
        recalls: list[Any] | None,
        ratings: dict[str, Any] | None,
        fetched_at: datetime | None = None,
    ) -> CachedProfile:
        """Store a fetched profile.

        `None` for recalls means the fetch failed and is stored as SQL NULL, so
        a later read can tell "we asked and there are none" (empty list) apart
        from "we never got an answer". Clearing a vehicle on the second reading
        would be exactly the wrong outcome.
        """
        ts = fetched_at or datetime.now(UTC)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO vehicle_profile_cache
                    (profile_key, model_year, make, model, recalls_json,
                     ratings_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_key) DO UPDATE SET
                    model_year   = excluded.model_year,
                    make         = excluded.make,
                    model        = excluded.model,
                    recalls_json = excluded.recalls_json,
                    ratings_json = excluded.ratings_json,
                    fetched_at   = excluded.fetched_at
                """,
                (
                    key,
                    model_year,
                    make,
                    model,
                    json.dumps(recalls, separators=(",", ":")) if recalls is not None else "null",
                    json.dumps(ratings, separators=(",", ":")) if ratings is not None else "null",
                    ts.isoformat(),
                ),
            )
            self._conn.commit()
        return CachedProfile(key, model_year, make, model, recalls, ratings, ts)

    def count_profiles(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM vehicle_profile_cache").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_profile(row: sqlite3.Row) -> CachedProfile:
    return CachedProfile(
        profile_key=row["profile_key"],
        model_year=row["model_year"],
        make=row["make"],
        model=row["model"],
        recalls=json.loads(row["recalls_json"]),
        ratings=json.loads(row["ratings_json"]),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
    )


def _row_to_cached(row: sqlite3.Row) -> CachedVin:
    return CachedVin(
        vin=row["vin"],
        make=row["make"],
        model=row["model"],
        model_year=row["model_year"],
        body_class=row["body_class"],
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        raw=json.loads(row["raw_json"]),
    )
