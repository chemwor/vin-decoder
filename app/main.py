"""Application factory, lifespan, and error translation."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings, settings
from .db import VinCache
from .nhtsa import NhtsaClient
from .routes import router
from .vpic import VinNotDecodable, VpicBadResponse, VpicClient, VpicUnavailable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("app")

DESCRIPTION = """
Decode VINs via the NHTSA vPIC API, with a local SQLite cache in front of it.

* `/lookup` - decode a VIN (cache-aside: SQLite first, vPIC on miss)
* `/remove` - evict a VIN from the cache
* `/export` - download the whole cache as a parquet file
* `/underwrite` - decode plus recalls, safety ratings and an open-recall flag
"""


def create_app(config: Settings | None = None) -> FastAPI:
    """Build the app.

    A factory rather than a module-level singleton so tests can construct an
    isolated instance (its own temp DB, a fake vPIC client) per test module.
    """
    config = config or settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One httpx client and one DB connection for the process lifetime.
        # Creating an AsyncClient per request would throw away connection
        # pooling and TLS session reuse, which is most of the cost of a call
        # to an external API.
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.vpic_timeout_seconds),
            headers={"User-Agent": "vin-decoder/1.0 (coding-challenge)"},
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
        app.state.cache = VinCache(config.db_path)
        app.state.vpic = VpicClient(
            http_client,
            base_url=config.vpic_base_url,
            max_retries=config.vpic_max_retries,
        )
        # Shares the one httpx client: same pooling, same timeouts, one place
        # to close. Different host, so it gets its own base URLs.
        app.state.nhtsa = NhtsaClient(
            http_client,
            recalls_base_url=config.nhtsa_recalls_base_url,
            ratings_base_url=config.nhtsa_ratings_base_url,
            max_retries=config.vpic_max_retries,
        )
        logger.info("startup: db=%s vpic=%s", config.db_path, config.vpic_base_url)
        try:
            yield
        finally:
            await http_client.aclose()
            app.state.cache.close()
            logger.info("shutdown complete")

    app = FastAPI(
        title="VIN Decoder",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    """Map integration failures to HTTP status codes in one place.

    The distinction that matters to a caller is "your VIN is bad" (422, do not
    retry) versus "our upstream is bad" (502, retry later) versus "our own
    storage is bad" (503). Handling it here keeps every route free of
    try/except boilerplate.
    """

    @app.exception_handler(VinNotDecodable)
    async def _not_decodable(request: Request, exc: VinNotDecodable) -> JSONResponse:
        logger.info("vin not decodable vin=%s: %s", exc.vin, exc.error_text)
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"vPIC could not decode this VIN: {exc.error_text}",
                "vin": exc.vin,
            },
        )

    @app.exception_handler(VpicUnavailable)
    async def _unavailable(request: Request, exc: VpicUnavailable) -> JSONResponse:
        logger.error("vpic unavailable: %s", exc)
        return JSONResponse(
            status_code=502,
            content={
                "detail": "Vehicle decode service is unavailable. Please retry.",
                "vin": None,
            },
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_unavailable(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        """Storage problems that are about the environment, not the query.

        Scoped to OperationalError on purpose. That is the family SQLite raises
        for conditions outside the code -- a read-only file, a lock it could not
        take, a disk it could not reach. Its siblings (IntegrityError,
        ProgrammingError) mean we wrote a bad statement, and a 503 inviting a
        retry would be the wrong answer for a bug that will fail identically
        every time; those keep the default 500.

        This existed as a bare "Internal Server Error" until a read-only cache
        file produced one with nothing in the response to say what was wrong.
        Cached VINs answered fine and only cache *misses* failed, which is a
        confusing shape to debug from the outside -- hence the specific text.
        """
        message = str(exc).lower()
        if "readonly" in message or "read-only" in message:
            detail = (
                "The VIN cache is not writable, so new VINs cannot be stored. "
                "Cached VINs will still resolve. Check file ownership and "
                "permissions on the SQLite database and its -wal/-shm files."
            )
            retry_after = None
        elif "locked" in message or "busy" in message:
            detail = "The VIN cache is busy. This is usually transient -- please retry."
            retry_after = "1"
        else:
            detail = "The VIN cache is unavailable. The service cannot read or write its database."
            retry_after = "5"

        # Full text to the log, classification to the caller: the exception can
        # name the database path, which is not something to hand out.
        logger.error("sqlite operational error: %s", exc)
        headers = {"Retry-After": retry_after} if retry_after else None
        return JSONResponse(
            status_code=503,
            content={"detail": detail, "vin": None},
            headers=headers,
        )

    @app.exception_handler(VpicBadResponse)
    async def _bad_response(request: Request, exc: VpicBadResponse) -> JSONResponse:
        logger.error("vpic bad response: %s", exc)
        return JSONResponse(
            status_code=502,
            content={
                "detail": "Vehicle decode service returned an unexpected response.",
                "vin": None,
            },
        )


app = create_app()
