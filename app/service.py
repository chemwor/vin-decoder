"""Business logic for the lookup flow.

Kept out of the route handlers so the HTTP layer stays about HTTP (status
codes, serialization) and this stays about the cache-aside policy. It also
means the flow can be unit-tested without spinning up an app.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from . import mechanical, nhtsa, risk_profile, underwriting
from .config import settings
from .db import CachedProfile, CachedVin, VinCache, profile_key
from .nhtsa import NhtsaClient
from .schemas import (
    AssessmentModel,
    ClaimsRoutingModel,
    EnergySourceModel,
    FlagModel,
    LookupResponse,
    MechanicalModel,
    RecallModel,
    RiskFlagModel,
    RiskProfileModel,
    SafetyRatingModel,
    SpecItemModel,
    UnderwriteResponse,
)
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
    entry, cached = await resolve_vin(vin, cache, vpic)
    return _to_response(entry, cached=cached)


async def resolve_vin(vin: str, cache: VinCache, vpic: VpicClient) -> tuple[CachedVin, bool]:
    """The cache-aside policy itself, returning the full cached row.

    Split out of `lookup` so /underwrite reuses one definition of "decode this
    VIN" -- including the single-flight dedupe -- rather than owning a second
    copy that could drift. Returns (entry, came_from_cache).
    """
    hit = await _get_fresh(cache, vin)
    if hit is not None:
        logger.info("cache hit vin=%s", vin)
        return hit, True

    async with _single_flight.acquire(vin):
        # Re-check under the lock: while we were queued, whoever held it may
        # have already populated the cache for this VIN.
        hit = await _get_fresh(cache, vin)
        if hit is not None:
            logger.info("cache hit (deduped) vin=%s", vin)
            return hit, True

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

    # Reported as cached=False even though we just wrote it: the flag describes
    # how *this* response was produced, not the state of the cache.
    return stored, False


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


# --- underwriting flow -----------------------------------------------------


async def underwrite(
    vin: str,
    cache: VinCache,
    vpic: VpicClient,
    client: NhtsaClient,
) -> UnderwriteResponse:
    """Decode, enrich with recalls and ratings, and assess the risk.

    Two cache layers with different keys and different TTLs: the decode is
    per-VIN and immutable, the recall profile is per-year/make/model and
    expires, because new campaigns get announced against old cars.

    Recall and rating failures do not fail the request. They come back as
    `data_gaps`, and the assessment degrades to INSUFFICIENT_DATA rather than
    reporting a clean vehicle it never actually checked.
    """
    entry, vin_cached = await resolve_vin(vin, cache, vpic)

    recalls, rating, gaps, profile_cached = await _resolve_profile(
        cache,
        client,
        entry.model_year,
        entry.make,
        entry.model,
        entry.body_class,
    )

    assessment = underwriting.assess(recalls, rating, gaps)
    specs = mechanical.build(entry.raw)

    return UnderwriteResponse(
        vin=entry.vin,
        make=entry.make,
        model=entry.model,
        model_year=entry.model_year,
        body_class=entry.body_class,
        underwriting=_to_assessment_model(assessment),
        recalls=[_to_recall_model(r) for r in (recalls or [])],
        safety_rating=_to_rating_model(rating) if rating else None,
        mechanical=_to_mechanical_model(specs),
        risk_profile=_to_risk_profile_model(risk_profile.build(entry.raw)),
        data_gaps=gaps,
        cached_result=vin_cached and profile_cached,
    )


async def _resolve_profile(
    cache: VinCache,
    client: NhtsaClient,
    model_year: str,
    make: str,
    model: str,
    body_class: str,
) -> tuple[list[nhtsa.Recall] | None, nhtsa.SafetyRating | None, list[str], bool]:
    """Cache-aside for the year/make/model profile.

    A vehicle whose decode has no make or model (rare, but vPIC will return it)
    cannot be looked up upstream at all, so that is reported as a gap rather
    than sent as a request guaranteed to match nothing.
    """
    if not make or not model or not model_year:
        return None, None, ["Decode lacks the make/model/year needed for a recall search"], False

    key = profile_key(model_year, make, model)
    hit = await _get_fresh_profile(cache, key)
    if hit is not None:
        return _from_cached_profile(hit) + (True,)

    async with _single_flight.acquire(f"profile:{key}"):
        hit = await _get_fresh_profile(cache, key)
        if hit is not None:
            return _from_cached_profile(hit) + (True,)

        logger.info("profile miss key=%s, calling NHTSA", key)
        recalls, rating, gaps = await nhtsa.gather_profile(
            client, model_year, make, model, body_class
        )
        if recalls is None:
            # Deliberately not cached. A failed recall fetch pinned for the
            # whole TTL would keep answering INSUFFICIENT_DATA for a day after
            # a blip that lasted seconds, and would flatten the specific reason
            # into a generic one. Retrying next request costs one fast call.
            logger.info("profile not cached (recall fetch failed) key=%s", key)
        else:
            await asyncio.to_thread(
                cache.upsert_profile,
                key,
                model_year,
                make,
                model,
                [nhtsa.recall_to_dict(r) for r in recalls],
                nhtsa.rating_to_dict(rating) if rating is not None else None,
            )

    return recalls, rating, gaps, False


async def _get_fresh_profile(cache: VinCache, key: str) -> CachedProfile | None:
    entry = await asyncio.to_thread(cache.get_profile, key)
    if entry is None or entry.is_expired(settings.profile_ttl_seconds):
        return None
    return entry


def _from_cached_profile(
    entry: CachedProfile,
) -> tuple[list[nhtsa.Recall] | None, nhtsa.SafetyRating | None, list[str]]:
    """Rehydrate a stored profile.

    A stored NULL for recalls means the original fetch failed. That is replayed
    as the same gap rather than as an empty list, so a cached failure keeps
    reading as "unknown" for its whole TTL instead of quietly becoming "clean".
    """
    recalls = (
        [nhtsa.recall_from_dict(r) for r in entry.recalls] if entry.recalls is not None else None
    )
    rating = nhtsa.rating_from_dict(entry.ratings) if entry.ratings else None
    gaps = [] if recalls is not None else ["Recall data unavailable from NHTSA (cached failure)"]
    return recalls, rating, gaps


# --- model conversion ------------------------------------------------------


def _to_assessment_model(assessment: underwriting.Assessment) -> AssessmentModel:
    return AssessmentModel(
        decision=assessment.decision,
        headline=assessment.headline,
        flags=[
            FlagModel(
                code=f.code,
                severity=f.severity,
                title=f.title,
                detail=f.detail,
                campaigns=f.campaigns,
            )
            for f in assessment.flags
        ],
        open_recall_count=assessment.open_recall_count,
        vin_level_verified=assessment.vin_level_verified,
        caveat=assessment.caveat,
        assessed_at=assessment.assessed_at.isoformat(),
    )


def _to_recall_model(recall: nhtsa.Recall) -> RecallModel:
    return RecallModel(
        campaign_number=recall.campaign_number,
        component=recall.component,
        summary=recall.summary,
        consequence=recall.consequence,
        remedy=recall.remedy,
        manufacturer=recall.manufacturer,
        report_received_date=(
            recall.report_received_date.isoformat() if recall.report_received_date else None
        ),
        park_it=recall.park_it,
        park_outside=recall.park_outside,
        over_the_air_update=recall.over_the_air_update,
    )


def _to_rating_model(rating: nhtsa.SafetyRating) -> SafetyRatingModel:
    return SafetyRatingModel(
        vehicle_description=rating.vehicle_description,
        overall=rating.overall,
        overall_front=rating.overall_front,
        overall_side=rating.overall_side,
        rollover=rating.rollover,
        rollover_possibility=rating.rollover_possibility,
        electronic_stability_control=rating.electronic_stability_control,
        forward_collision_warning=rating.forward_collision_warning,
        lane_departure_warning=rating.lane_departure_warning,
        complaints_count=rating.complaints_count,
        recalls_count=rating.recalls_count,
        investigation_count=rating.investigation_count,
    )


def _to_risk_profile_model(profile: risk_profile.RiskProfile) -> RiskProfileModel:
    routing = profile.claims_routing
    energy = profile.energy_source
    return RiskProfileModel(
        claims_routing=ClaimsRoutingModel(
            queue=routing.queue,
            label=routing.label,
            basis=routing.basis,
            commercial=routing.commercial,
        ),
        energy_source=EnergySourceModel(
            kind=energy.kind,
            label=energy.label,
            battery_type=energy.battery_type,
            basis=energy.basis,
            flags=[
                RiskFlagModel(code=f.code, severity=f.severity, title=f.title, detail=f.detail)
                for f in energy.flags
            ],
        ),
    )


def _to_mechanical_model(profile: mechanical.MechanicalProfile) -> MechanicalModel:
    def items(rows: list[mechanical.SpecItem]) -> list[SpecItemModel]:
        return [SpecItemModel(label=i.label, value=i.value) for i in rows]

    return MechanicalModel(
        powertrain=items(profile.powertrain),
        structure=items(profile.structure),
        safety_equipment=items(profile.safety_equipment),
    )
