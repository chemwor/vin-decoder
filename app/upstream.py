"""Retrying JSON fetch shared by the NHTSA integrations.

Both upstreams (vPIC for the decode, api.nhtsa.gov for recalls and safety
ratings) need the same policy: retry the failures that are worth retrying,
give up on the ones that are our own fault, and never let an httpx type leak
past this module. The policy lives here once rather than being copied per
client, but the *exception types* stay with each client so a caller can still
tell a vPIC outage apart from a recalls outage.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# HTTP statuses worth trying again. Everything else is a bug in our request.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    max_retries: int,
    unavailable: type[Exception],
    bad_response: type[Exception],
    label: str,
    status_error: type[Exception] | None = None,
) -> Any:
    """GET `url` and parse JSON, retrying transient failures with backoff.

    `unavailable` and `bad_response` are the exception classes to raise, passed
    in rather than inherited so each client keeps its own error vocabulary and
    the app's exception handlers can map them to different status codes.

    `status_error` overrides `bad_response` for a non-retryable HTTP status
    only. Some endpoints answer 4xx to mean something specific about the query
    rather than something wrong with it, and a caller that can say which needs
    to tell that apart from a malformed body.
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = await client.get(url, params=params)
            if response.status_code in RETRYABLE_STATUS:
                last_error = unavailable(f"{label} returned HTTP {response.status_code}")
            elif response.status_code >= 400:
                # Non-retryable: a 404 here means we built a bad URL.
                raise (status_error or bad_response)(
                    f"{label} returned HTTP {response.status_code}"
                )
            else:
                try:
                    return response.json()
                except ValueError as exc:
                    raise bad_response(f"{label} returned non-JSON body") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = unavailable(f"{label} request failed: {exc}")

        if attempt < max_retries:
            # Exponential backoff with jitter, so a burst of requests that all
            # fail together does not retry in lockstep.
            delay = (2**attempt) * 0.25 + random.uniform(0, 0.1)
            logger.warning(
                "%s attempt %s/%s failed (%s); retrying in %.2fs",
                label,
                attempt + 1,
                max_retries + 1,
                last_error,
                delay,
            )
            await asyncio.sleep(delay)

    raise last_error or unavailable(f"{label} request failed")
