"""Application factory, lifespan, and error translation."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings, settings
from .db import VinCache
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
    retry) versus "our upstream is bad" (502, retry later). Handling it here
    keeps every route free of try/except boilerplate.
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
