"""HTTP routes.

Each route is exposed in two shapes, because "the request should contain a
single string called vin" does not say which verb:

    GET  /lookup?vin=...       POST   /lookup   {"vin": "..."}
    POST /remove {"vin": ...}  DELETE /remove?vin=...

Both shapes call the same service function, so there is one code path and one
definition of correct behaviour. Upstream failures are translated to status
codes by the exception handlers in main.py, not with try/except here.

Dependencies use `Annotated[...]` rather than `= Depends(...)` defaults: it is
the current FastAPI idiom, it keeps the signatures readable, and it avoids the
mutable-default-argument smell that the `= Depends()` form technically is.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse

from . import service
from .db import VinCache
from .dependencies import get_cache, get_nhtsa_client, get_vpic_client
from .export import build_parquet
from .nhtsa import NhtsaClient
from .schemas import (
    ErrorResponse,
    LookupResponse,
    RemoveResponse,
    UnderwriteResponse,
    VinRequest,
    normalize_vin,
)
from .vpic import VpicClient

router = APIRouter()

# Resolved from this file, never from the working directory: the page has to
# be found the same way whether the app is started from the project root, an
# IDE run configuration, or /srv in the container.
STATIC_DIR = Path(__file__).parent / "static"

LOOKUP_ERRORS = {
    422: {"model": ErrorResponse, "description": "VIN is malformed or not decodable"},
    502: {"model": ErrorResponse, "description": "vPIC unavailable"},
    503: {"model": ErrorResponse, "description": "The local cache is unreadable or unwritable"},
}


def vin_query(
    vin: Annotated[str, Query(description="17-character alphanumeric VIN")],
) -> str:
    """Validate a VIN supplied as a query parameter.

    The body forms get this from the pydantic model; query params need it done
    explicitly so both entry points reject the same inputs with the same 422.
    """
    try:
        return normalize_vin(vin)
    except ValueError as exc:
        # 422 to match what pydantic returns for the body forms.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


Vin = Annotated[str, Depends(vin_query)]
Cache = Annotated[VinCache, Depends(get_cache)]
Vpic = Annotated[VpicClient, Depends(get_vpic_client)]
Nhtsa = Annotated[NhtsaClient, Depends(get_nhtsa_client)]


# --- /lookup ---------------------------------------------------------------


@router.get("/lookup", response_model=LookupResponse, responses=LOOKUP_ERRORS, tags=["lookup"])
async def lookup_get(vin: Vin, cache: Cache, vpic: Vpic) -> LookupResponse:
    """Decode a VIN, from cache when possible."""
    return await service.lookup(vin, cache, vpic)


@router.post("/lookup", response_model=LookupResponse, responses=LOOKUP_ERRORS, tags=["lookup"])
async def lookup_post(payload: VinRequest, cache: Cache, vpic: Vpic) -> LookupResponse:
    """Decode a VIN, from cache when possible."""
    return await service.lookup(payload.vin, cache, vpic)


# --- /remove ---------------------------------------------------------------


@router.post("/remove", response_model=RemoveResponse, tags=["cache"])
async def remove_post(payload: VinRequest, cache: Cache) -> RemoveResponse:
    """Drop a VIN from the cache.

    Deleting something that is not cached is a 200 with
    cache_delete_success=false, not a 404: the caller's intent ("this VIN
    should not be cached") is satisfied either way, and the boolean tells them
    whether anything was actually there.
    """
    deleted = await service.remove(payload.vin, cache)
    return RemoveResponse(vin=payload.vin, cache_delete_success=deleted)


@router.delete("/remove", response_model=RemoveResponse, tags=["cache"])
async def remove_delete(vin: Vin, cache: Cache) -> RemoveResponse:
    """Drop a VIN from the cache."""
    deleted = await service.remove(vin, cache)
    return RemoveResponse(vin=vin, cache_delete_success=deleted)


# --- /export ---------------------------------------------------------------


@router.get(
    "/export",
    tags=["cache"],
    response_class=Response,
    responses={
        200: {
            "content": {"application/vnd.apache.parquet": {}},
            "description": "Parquet file containing every cached VIN",
        }
    },
)
async def export_cache(cache: Cache) -> Response:
    """Download the entire cache as a parquet file.

    Built in a worker thread: pyarrow serialization is CPU-bound and would
    otherwise stall the event loop for every other in-flight request.
    """
    data = await asyncio.to_thread(build_parquet, cache)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Response(
        content=data,
        media_type="application/vnd.apache.parquet",
        headers={
            "Content-Disposition": f'attachment; filename="vin_cache_{stamp}.parquet"',
            "Content-Length": str(len(data)),
        },
    )


# --- /underwrite -----------------------------------------------------------


@router.get(
    "/underwrite",
    response_model=UnderwriteResponse,
    responses=LOOKUP_ERRORS,
    tags=["underwriting"],
)
async def underwrite_get(vin: Vin, cache: Cache, vpic: Vpic, nhtsa: Nhtsa) -> UnderwriteResponse:
    """Decode a VIN and assess it for open recalls.

    Separate from /lookup rather than folded into it: this can make two further
    upstream calls, and callers who only need the decode should not pay for
    them. The same 422/502 rules apply, because both start with the same decode.
    """
    return await service.underwrite(vin, cache, vpic, nhtsa)


@router.post(
    "/underwrite",
    response_model=UnderwriteResponse,
    responses=LOOKUP_ERRORS,
    tags=["underwriting"],
)
async def underwrite_post(
    payload: VinRequest, cache: Cache, vpic: Vpic, nhtsa: Nhtsa
) -> UnderwriteResponse:
    """Decode a VIN and assess it for open recalls."""
    return await service.underwrite(payload.vin, cache, vpic, nhtsa)


# --- operational -----------------------------------------------------------


@router.get("/health", tags=["ops"])
async def health(cache: Cache) -> dict[str, object]:
    """Liveness plus a cheap readiness signal (the DB answers a query)."""
    cached = await asyncio.to_thread(cache.count)
    profiles = await asyncio.to_thread(cache.count_profiles)
    return {"status": "ok", "cached_vins": cached, "cached_profiles": profiles}


# --- ui --------------------------------------------------------------------


@router.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve the demo client at the API's own origin.

    Same-origin is what lets the page call /lookup and friends with no CORS
    middleware and no preflight. Hidden from the schema because /docs is the
    documentation for machines; this is the one for humans.
    """
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")
