"""FastAPI dependency providers.

Both the cache and the vPIC client are created once in the app lifespan and
stashed on `app.state`. Routes ask for them through `Depends`, which is what
makes the test suite able to inject a fake vPIC client without touching the
network or reaching into module globals.
"""

from __future__ import annotations

from fastapi import Request

from .db import VinCache
from .nhtsa import NhtsaClient
from .vpic import VpicClient


def get_cache(request: Request) -> VinCache:
    return request.app.state.cache


def get_vpic_client(request: Request) -> VpicClient:
    return request.app.state.vpic


def get_nhtsa_client(request: Request) -> NhtsaClient:
    return request.app.state.nhtsa
