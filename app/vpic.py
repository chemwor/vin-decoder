"""Client for the NHTSA vPIC decode API.

Isolated behind a small class with one public method so that (a) the routes
never see httpx, and (b) tests can swap in a fake via FastAPI's dependency
overrides instead of monkeypatching the network.

Uses the `DecodeVinValues` endpoint, which returns one flat object per VIN.
The alternative, `DecodeVin`, returns ~140 {Variable, Value} rows that you
then have to pivot client-side. Same data, more work, more to go wrong.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# HTTP statuses worth trying again. Everything else is a bug in our request.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class VpicError(Exception):
    """Base class for anything the upstream integration can go wrong with."""


class VpicUnavailable(VpicError):
    """Upstream was unreachable, timed out, or 5xx'd after all retries."""


class VpicBadResponse(VpicError):
    """Upstream answered, but not with the shape we expect."""


class VinNotDecodable(VpicError):
    """Upstream answered fine and told us this VIN decodes to nothing."""

    def __init__(self, vin: str, error_text: str) -> None:
        super().__init__(f"vPIC could not decode VIN {vin}: {error_text}")
        self.vin = vin
        self.error_text = error_text


@dataclass(frozen=True)
class DecodedVin:
    """The subset of vPIC's response this service promises, plus the raw blob.

    `raw` is kept so the cache holds the full decode. If someone later asks for
    fuel type or drive type, it is a schema migration and a backfill from
    `raw_json`, not 100k re-fetches from NHTSA.
    """

    vin: str
    make: str
    model: str
    model_year: str
    body_class: str
    raw: dict[str, Any]


class VpicClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        max_retries: int = 2,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(0, max_retries)

    async def decode(self, vin: str) -> DecodedVin:
        url = f"{self._base_url}/DecodeVinValues/{vin}"
        payload = await self._get_json(url, params={"format": "json"})
        return _parse(vin, payload)

    async def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(url, params=params)
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = VpicUnavailable(f"vPIC returned HTTP {response.status_code}")
                elif response.status_code >= 400:
                    # Non-retryable: a 404 here means we built a bad URL.
                    raise VpicBadResponse(f"vPIC returned HTTP {response.status_code}")
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise VpicBadResponse("vPIC returned non-JSON body") from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = VpicUnavailable(f"vPIC request failed: {exc}")

            if attempt < self._max_retries:
                # Exponential backoff with jitter, so a burst of requests that
                # all fail together does not retry in lockstep.
                delay = (2**attempt) * 0.25 + random.uniform(0, 0.1)
                logger.warning(
                    "vPIC attempt %s/%s failed (%s); retrying in %.2fs",
                    attempt + 1,
                    self._max_retries + 1,
                    last_error,
                    delay,
                )
                await asyncio.sleep(delay)

        raise last_error or VpicUnavailable("vPIC request failed")


def _parse(vin: str, payload: dict[str, Any]) -> DecodedVin:
    results = payload.get("Results")
    if not isinstance(results, list) or not results:
        raise VpicBadResponse("vPIC response contained no Results")

    record = results[0]
    if not isinstance(record, dict):
        raise VpicBadResponse("vPIC Results[0] was not an object")

    make = _clean(record.get("Make"))
    model = _clean(record.get("Model"))
    model_year = _clean(record.get("ModelYear"))
    body_class = _clean(record.get("BodyClass"))

    # vPIC always returns HTTP 200. Failure is signalled in the body: ErrorCode
    # is a comma-separated list where "0" means a clean decode. Codes like
    # "1" (check-digit mismatch) still come back with usable data, so the test
    # is "did we get anything identifying?" rather than "was ErrorCode 0?".
    if not make and not model and not model_year:
        error_text = _clean(record.get("ErrorText")) or "no data returned"
        raise VinNotDecodable(vin, error_text)

    return DecodedVin(
        vin=vin,
        make=make,
        model=model,
        model_year=model_year,
        body_class=body_class,
        raw=record,
    )


def _clean(value: Any) -> str:
    """vPIC uses both null and "" for missing fields. Normalize to ""."""
    if value is None:
        return ""
    return str(value).strip()
