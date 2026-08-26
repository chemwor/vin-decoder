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

    def close(self) -> None:
        with self._lock:
            self._conn.close()


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
