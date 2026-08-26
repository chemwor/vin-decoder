"""Clients for the api.nhtsa.gov recalls and safety-ratings endpoints.

Kept apart from `vpic.py` because this is a different host with different
conventions: recalls answers under a lowercase `results` key while
SafetyRatings uses `Results`, and safety ratings needs two round trips (name
lookup, then a fetch by the opaque VehicleId it hands back).

The important difference from the decode, though, is what a failure *means*.
A vPIC outage makes a lookup impossible, so it becomes a 502. An outage here
only makes the underwriting picture incomplete, and an incomplete picture is
something an underwriter must be told about rather than shielded from -- so
these raise, and `service.py` degrades the assessment to NEEDS_REVIEW instead
of failing the request. Silence would be the dangerous behaviour here.

Both endpoints are keyed by model year / make / model, never by VIN. That is a
property of NHTSA's public API and it is the single most important caveat in
this feature; see `underwriting.py` for what it means for the flag.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

from . import upstream

logger = logging.getLogger(__name__)


class NhtsaError(Exception):
    """Base class for the recalls / safety-ratings integration."""


class NhtsaUnavailable(NhtsaError):
    """Unreachable, timed out, or 5xx'd after all retries."""


class NhtsaBadResponse(NhtsaError):
    """Answered, but not with the shape we expect."""


class NhtsaVehicleNotListed(NhtsaError):
    """NHTSA does not recognise this make/model/year combination.

    Distinct from an outage, and emphatically not the same as "no recalls".
    NHTSA indexes recalls under factory model codes while vPIC decodes to
    marketing names -- a 2015 Harley decodes as "Street Glide" but is filed
    under FLHX -- so this vehicle may well have campaigns we simply cannot
    address by name. Clearing it would be the wrong call.
    """


@dataclass(frozen=True)
class Recall:
    """One NHTSA recall campaign covering this year/make/model.

    `park_it` and `park_outside` are NHTSA's own severity markers -- a
    "Do Not Drive" and a "Park Outside" (fire risk) advisory respectively.
    They are the only fields here that carry an explicit hazard judgement from
    the regulator rather than from us, which is why the underwriting rules lean
    on them so heavily.
    """

    campaign_number: str
    component: str
    summary: str
    consequence: str
    remedy: str
    manufacturer: str
    report_received_date: date | None
    park_it: bool
    park_outside: bool
    over_the_air_update: bool


@dataclass(frozen=True)
class SafetyRating:
    """NCAP crash-test results for the closest matching body style."""

    vehicle_id: int
    vehicle_description: str
    overall: str
    overall_front: str
    overall_side: str
    rollover: str
    rollover_possibility: float | None
    electronic_stability_control: str
    forward_collision_warning: str
    lane_departure_warning: str
    complaints_count: int
    recalls_count: int
    investigation_count: int


class NhtsaClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        recalls_base_url: str,
        ratings_base_url: str,
        max_retries: int = 2,
    ) -> None:
        self._client = client
        self._recalls_base_url = recalls_base_url.rstrip("/")
        self._ratings_base_url = ratings_base_url.rstrip("/")
        self._max_retries = max(0, max_retries)

    async def recalls(self, model_year: str, make: str, model: str) -> list[Recall]:
        """Every recall campaign NHTSA lists for this year/make/model."""
        try:
            # This endpoint answers 4xx for a make/model it does not index,
            # which is a statement about its catalogue rather than about our
            # request -- so that status, and only that status, becomes
            # NhtsaVehicleNotListed. A malformed *body* is still a bad response.
            payload = await self._get(
                f"{self._recalls_base_url}/recallsByVehicle",
                params={"make": make, "model": model, "modelYear": model_year},
                status_error=NhtsaVehicleNotListed,
            )
        except NhtsaVehicleNotListed as exc:
            raise NhtsaVehicleNotListed(
                f"NHTSA does not list model '{model}' under {make} {model_year}"
            ) from exc
        # Lowercase "results" here; SafetyRatings uses "Results". Not a typo.
        rows = payload.get("results")
        if rows is None:
            rows = payload.get("Results", [])
        if not isinstance(rows, list):
            raise NhtsaBadResponse("recalls response was not a list")
        return [_parse_recall(r) for r in rows if isinstance(r, dict)]

    async def safety_rating(
        self,
        model_year: str,
        make: str,
        model: str,
        body_hint: str = "",
    ) -> SafetyRating | None:
        """NCAP ratings, or None when NHTSA has not crash-tested this vehicle.

        Two calls: the name lookup returns one row per body style (a 2013 328i
        yields three), so we pick the closest to the decoded body class rather
        than blindly taking the first and reporting a coupe's rollover number
        for a sedan.
        """
        listing = await self._get(
            f"{self._ratings_base_url}/modelyear/{model_year}/make/{make}/model/{model}"
        )
        variants = listing.get("Results") or []
        if not isinstance(variants, list) or not variants:
            logger.info("no NCAP entry for %s %s %s", model_year, make, model)
            return None

        chosen = _best_variant(variants, body_hint)
        vehicle_id = chosen.get("VehicleId")
        if not isinstance(vehicle_id, int):
            raise NhtsaBadResponse("SafetyRatings variant had no VehicleId")

        detail = await self._get(f"{self._ratings_base_url}/VehicleId/{vehicle_id}")
        rows = detail.get("Results") or []
        if not isinstance(rows, list) or not rows:
            return None
        return _parse_rating(rows[0], chosen.get("VehicleDescription", ""))

    async def _get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        status_error: type[Exception] | None = None,
    ) -> dict[str, Any]:
        payload = await upstream.get_json(
            self._client,
            url,
            params=params,
            max_retries=self._max_retries,
            unavailable=NhtsaUnavailable,
            bad_response=NhtsaBadResponse,
            label="NHTSA",
            status_error=status_error,
        )
        if not isinstance(payload, dict):
            raise NhtsaBadResponse("NHTSA returned a non-object body")
        return payload


def _best_variant(variants: list[Any], body_hint: str) -> dict[str, Any]:
    """Pick the body style closest to the decoded BodyClass.

    Crude on purpose: NHTSA's descriptions ("2013 BMW 328 I 4 DR RWD") share
    only loose vocabulary with vPIC's ("Sedan/Saloon"), so this scores a few
    reliable tokens and falls back to the first entry. Getting this wrong
    picks a sibling trim's stars, not wrong data.
    """
    hint = body_hint.upper()
    wanted = set()
    if "COUPE" in hint or "2-DOOR" in hint or "2 DOOR" in hint:
        wanted.add("2 DR")
    if "SEDAN" in hint or "SALOON" in hint or "4-DOOR" in hint:
        wanted.add("4 DR")
    if "CONVERTIBLE" in hint or "CABRIOLET" in hint:
        wanted.add("CONV")
    if "WAGON" in hint:
        wanted.add("WAGON")
    if "SUV" in hint or "UTILITY" in hint:
        wanted.add("SUV")

    best, best_score = None, -1
    for v in variants:
        if not isinstance(v, dict):
            continue
        description = str(v.get("VehicleDescription", "")).upper()
        score = sum(1 for token in wanted if token in description)
        if score > best_score:
            best, best_score = v, score
    return best if best is not None else {}


def _parse_recall(row: dict[str, Any]) -> Recall:
    return Recall(
        campaign_number=_text(row.get("NHTSACampaignNumber")),
        component=_text(row.get("Component")),
        summary=_text(row.get("Summary")),
        consequence=_text(row.get("Consequence")),
        remedy=_text(row.get("Remedy")),
        manufacturer=_text(row.get("Manufacturer")),
        report_received_date=_parse_date(_text(row.get("ReportReceivedDate"))),
        park_it=bool(row.get("parkIt")),
        park_outside=bool(row.get("parkOutSide")),
        over_the_air_update=bool(row.get("overTheAirUpdate")),
    )


def _parse_rating(row: dict[str, Any], description: str) -> SafetyRating:
    return SafetyRating(
        vehicle_id=int(row.get("VehicleId") or 0),
        vehicle_description=_text(row.get("VehicleDescription")) or description,
        overall=_text(row.get("OverallRating")),
        overall_front=_text(row.get("OverallFrontCrashRating")),
        overall_side=_text(row.get("OverallSideCrashRating")),
        rollover=_text(row.get("RolloverRating")),
        rollover_possibility=_maybe_float(row.get("RolloverPossibility")),
        electronic_stability_control=_text(row.get("NHTSAElectronicStabilityControl")),
        forward_collision_warning=_text(row.get("NHTSAForwardCollisionWarning")),
        lane_departure_warning=_text(row.get("NHTSALaneDepartureWarning")),
        complaints_count=_maybe_int(row.get("ComplaintsCount")),
        recalls_count=_maybe_int(row.get("RecallsCount")),
        investigation_count=_maybe_int(row.get("InvestigationCount")),
    )


def _parse_date(raw: str) -> date | None:
    """NHTSA returns DD/MM/YYYY here (e.g. 16/07/2018), not the US order.

    Tried in that order deliberately: reading 07/06/2019 as June 7th when it
    means July 6th only shifts a date, but reading 16/07 as a US date fails
    outright, so the unambiguous format goes first and US order is the fallback.
    """
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _maybe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _maybe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def gather_profile(
    client: NhtsaClient,
    model_year: str,
    make: str,
    model: str,
    body_hint: str = "",
) -> tuple[list[Recall] | None, SafetyRating | None, list[str]]:
    """Fetch recalls and ratings concurrently, reporting what failed.

    Returns `(recalls, rating, gaps)`. A `None` for recalls means "we do not
    know", which is a different and much more consequential state than the
    empty list's "we asked and there are none" -- the underwriting rules treat
    them differently and `gaps` carries the reason through to the response.
    """
    recalls_task = asyncio.create_task(client.recalls(model_year, make, model))
    rating_task = asyncio.create_task(client.safety_rating(model_year, make, model, body_hint))
    results = await asyncio.gather(recalls_task, rating_task, return_exceptions=True)

    recalls: list[Recall] | None = None
    rating: SafetyRating | None = None
    gaps: list[str] = []

    if isinstance(results[0], NhtsaVehicleNotListed):
        logger.info("recalls: vehicle not listed %s %s %s", model_year, make, model)
        gaps.append(
            f"NHTSA does not index recalls under the model name '{model}' for "
            f"{make} {model_year}. It files some vehicles (motorcycles especially) "
            "under factory model codes, so campaigns may exist under another name."
        )
    elif isinstance(results[0], BaseException):
        logger.warning(
            "recalls lookup failed for %s %s %s: %s", model_year, make, model, results[0]
        )
        gaps.append("Recall data unavailable from NHTSA")
    else:
        recalls = results[0]

    if isinstance(results[1], BaseException):
        logger.warning(
            "ratings lookup failed for %s %s %s: %s", model_year, make, model, results[1]
        )
        gaps.append("Safety rating data unavailable from NHTSA")
    else:
        rating = results[1]

    return recalls, rating, gaps


# --- cache serialization ---------------------------------------------------
#
# Hand-written rather than dataclasses.asdict() because `date` is not JSON
# serializable and round-tripping it silently as a string would make the cache
# and the live path return different types for the same field.


def recall_to_dict(recall: Recall) -> dict[str, Any]:
    return {
        "campaign_number": recall.campaign_number,
        "component": recall.component,
        "summary": recall.summary,
        "consequence": recall.consequence,
        "remedy": recall.remedy,
        "manufacturer": recall.manufacturer,
        "report_received_date": (
            recall.report_received_date.isoformat() if recall.report_received_date else None
        ),
        "park_it": recall.park_it,
        "park_outside": recall.park_outside,
        "over_the_air_update": recall.over_the_air_update,
    }


def recall_from_dict(row: dict[str, Any]) -> Recall:
    raw_date = row.get("report_received_date")
    return Recall(
        campaign_number=_text(row.get("campaign_number")),
        component=_text(row.get("component")),
        summary=_text(row.get("summary")),
        consequence=_text(row.get("consequence")),
        remedy=_text(row.get("remedy")),
        manufacturer=_text(row.get("manufacturer")),
        report_received_date=date.fromisoformat(raw_date) if raw_date else None,
        park_it=bool(row.get("park_it")),
        park_outside=bool(row.get("park_outside")),
        over_the_air_update=bool(row.get("over_the_air_update")),
    )


def rating_to_dict(rating: SafetyRating) -> dict[str, Any]:
    return {
        "vehicle_id": rating.vehicle_id,
        "vehicle_description": rating.vehicle_description,
        "overall": rating.overall,
        "overall_front": rating.overall_front,
        "overall_side": rating.overall_side,
        "rollover": rating.rollover,
        "rollover_possibility": rating.rollover_possibility,
        "electronic_stability_control": rating.electronic_stability_control,
        "forward_collision_warning": rating.forward_collision_warning,
        "lane_departure_warning": rating.lane_departure_warning,
        "complaints_count": rating.complaints_count,
        "recalls_count": rating.recalls_count,
        "investigation_count": rating.investigation_count,
    }


def rating_from_dict(row: dict[str, Any]) -> SafetyRating:
    return SafetyRating(
        vehicle_id=_maybe_int(row.get("vehicle_id")),
        vehicle_description=_text(row.get("vehicle_description")),
        overall=_text(row.get("overall")),
        overall_front=_text(row.get("overall_front")),
        overall_side=_text(row.get("overall_side")),
        rollover=_text(row.get("rollover")),
        rollover_possibility=_maybe_float(row.get("rollover_possibility")),
        electronic_stability_control=_text(row.get("electronic_stability_control")),
        forward_collision_warning=_text(row.get("forward_collision_warning")),
        lane_departure_warning=_text(row.get("lane_departure_warning")),
        complaints_count=_maybe_int(row.get("complaints_count")),
        recalls_count=_maybe_int(row.get("recalls_count")),
        investigation_count=_maybe_int(row.get("investigation_count")),
    )
