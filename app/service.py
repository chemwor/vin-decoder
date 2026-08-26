"""Business logic for the lookup flow.

Kept out of the route handlers so the HTTP layer stays about HTTP (status
codes, serialization) and this stays about the cache-aside policy. It also
means the flow can be unit-tested without spinning up an app.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from .config import settings
from .db import CachedVin, VinCache
from .schemas import LookupResponse
from .vpic import VpicClient

logger = logging.getLogger(__name__)


class SingleFlight:
    """Collapses concurrent work on the same key into one execution.

    Without this, N simultaneous requests for the same uncached VIN produce N
    calls to NHTSA. With it, the first caller fetches and the rest wake up to a
    cache hit.

    The reference count is what keeps this from being a slow memory leak: a
    plain `defaultdict(asyncio.Lock)` would accumulate one lock per VIN ever
    seen and never free them. Counts are incremented and decremented without
    awaiting in between, so under asyncio's single-threaded loop there is no
    window for a lock to be dropped while someone is still waiting on it.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[str, int] = {}

    @asynccontextmanager
    async def acquire(self, key: str):
        self._waiters[key] = self._waiters.get(key, 0) + 1
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                yield
        finally:
            self._waiters[key] -= 1
            if self._waiters[key] == 0:
                del self._waiters[key]
                del self._locks[key]

    def in_flight(self) -> int:
        return len(self._locks)


_single_flight = SingleFlight()


async def lookup(vin: str, cache: VinCache, vpic: VpicClient) -> LookupResponse:
    """Cache-aside read: check SQLite, fall back to vPIC, write through."""
    hit = await _get_fresh(cache, vin)
    if hit is not None:
        logger.info("cache hit vin=%s", vin)
        return _to_response(hit, cached=True)

    async with _single_flight.acquire(vin):
        # Re-check under the lock: while we were queued, whoever held it may
        # have already populated the cache for this VIN.
        hit = await _get_fresh(cache, vin)
        if hit is not None:
            logger.info("cache hit (deduped) vin=%s", vin)
            return _to_response(hit, cached=True)

        logger.info("cache miss vin=%s, calling vPIC", vin)
        decoded = await vpic.decode(vin)
        stored = await asyncio.to_thread(
            cache.upsert,
            decoded.vin,
            decoded.make,
            decoded.model,
            decoded.model_year,
            decoded.body_class,
            decoded.raw,
        )

    # Reported as cached_result=False even though we just wrote it: the flag
    # describes how *this* response was produced, not the state of the cache.
    return _to_response(stored, cached=False)


async def remove(vin: str, cache: VinCache) -> bool:
    deleted = await asyncio.to_thread(cache.delete, vin)
    logger.info("cache delete vin=%s deleted=%s", vin, deleted)
    return deleted


async def _get_fresh(cache: VinCache, vin: str) -> CachedVin | None:
    """Cache read, honouring TTL. sqlite3 is blocking, so it runs off-loop."""
    entry = await asyncio.to_thread(cache.get, vin)
    if entry is None or entry.is_expired(settings.cache_ttl_seconds):
        return None
    return entry


def _to_response(entry: CachedVin, *, cached: bool) -> LookupResponse:
    return LookupResponse(
        vin=entry.vin,
        make=entry.make,
        model=entry.model,
        model_year=entry.model_year,
        body_class=entry.body_class,
        cached_result=cached,
    )
