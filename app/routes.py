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
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from . import service
from .db import VinCache
from .dependencies import get_cache, get_vpic_client
from .export import build_parquet
from .schemas import (
    ErrorResponse,
    LookupResponse,
    RemoveResponse,
    VinRequest,
    normalize_vin,
)
from .vpic import VpicClient

router = APIRouter()

LOOKUP_ERRORS = {
    422: {"model": ErrorResponse, "description": "VIN is malformed or not decodable"},
    502: {"model": ErrorResponse, "description": "vPIC unavailable"},
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


# --- operational -----------------------------------------------------------


@router.get("/health", tags=["ops"])
async def health(cache: Cache) -> dict[str, object]:
    """Liveness plus a cheap readiness signal (the DB answers a query)."""
    cached = await asyncio.to_thread(cache.count)
    return {"status": "ok", "cached_vins": cached}
