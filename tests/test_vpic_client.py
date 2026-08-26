"""Unit tests for the vPIC client.

These use httpx's MockTransport, so the retry/parse logic is tested against
real httpx request and response objects without any network.
"""

from __future__ import annotations

import httpx
import pytest

from app.vpic import (
    VinNotDecodable,
    VpicBadResponse,
    VpicClient,
    VpicUnavailable,
)
from tests.conftest import SAMPLE_RAW, SAMPLE_VIN

BASE_URL = "http://vpic.test/api/vehicles"


def make_client(handler, max_retries: int = 2) -> VpicClient:
    transport = httpx.MockTransport(handler)
    return VpicClient(
        httpx.AsyncClient(transport=transport),
        base_url=BASE_URL,
        max_retries=max_retries,
    )


def ok_payload(**overrides) -> dict:
    return {"Count": 1, "Message": "ok", "Results": [dict(SAMPLE_RAW, **overrides)]}


@pytest.mark.asyncio
async def test_decode_happy_path():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ok_payload())

    result = await make_client(handler).decode(SAMPLE_VIN)

    assert (result.make, result.model, result.model_year, result.body_class) == (
        "HONDA",
        "Accord",
        "2003",
        "Coupe",
    )
    # Full upstream record is retained for future fields / debugging.
    assert result.raw["ErrorCode"] == "0"
    assert seen[0].url.path.endswith(f"/DecodeVinValues/{SAMPLE_VIN}")
    assert seen[0].url.params["format"] == "json"


@pytest.mark.asyncio
async def test_null_fields_become_empty_strings():
    """vPIC uses both null and "" for unknown values; callers get one shape."""

    def handler(request):
        return httpx.Response(200, json=ok_payload(BodyClass=None, Model=""))

    result = await make_client(handler).decode(SAMPLE_VIN)
    assert result.body_class == ""
    assert result.model == ""


@pytest.mark.asyncio
async def test_partial_decode_is_kept_not_rejected():
    """A check-digit warning still yields usable data, so we return it."""

    def handler(request):
        return httpx.Response(
            200,
            json=ok_payload(Model="", BodyClass="", ErrorCode="1", ErrorText="1 - Check Digit"),
        )

    result = await make_client(handler).decode(SAMPLE_VIN)
    assert result.make == "HONDA"
    assert result.model == ""


@pytest.mark.asyncio
async def test_empty_decode_raises_not_decodable():
    def handler(request):
        return httpx.Response(
            200,
            json=ok_payload(
                Make="",
                Model="",
                ModelYear="",
                BodyClass="",
                ErrorCode="11",
                ErrorText="11 - Incorrect Model Year",
            ),
        )

    with pytest.raises(VinNotDecodable) as exc:
        await make_client(handler).decode(SAMPLE_VIN)
    assert "Incorrect Model Year" in exc.value.error_text


@pytest.mark.asyncio
async def test_retries_transient_failures_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        if attempts["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=ok_payload())

    result = await make_client(handler, max_retries=2).decode(SAMPLE_VIN)
    assert result.make == "HONDA"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_gives_up_after_max_retries():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(503)

    with pytest.raises(VpicUnavailable):
        await make_client(handler, max_retries=2).decode(SAMPLE_VIN)
    assert attempts["n"] == 3  # first attempt + 2 retries


@pytest.mark.asyncio
async def test_client_error_is_not_retried():
    """A 404 means we built a bad URL; retrying it just wastes time."""
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(404)

    with pytest.raises(VpicBadResponse):
        await make_client(handler, max_retries=2).decode(SAMPLE_VIN)
    assert attempts["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [{"Count": 0, "Results": []}, {"Count": 1}, {"Results": ["not-an-object"]}],
)
async def test_malformed_payloads_raise_bad_response(body):
    def handler(request):
        return httpx.Response(200, json=body)

    with pytest.raises(VpicBadResponse):
        await make_client(handler, max_retries=0).decode(SAMPLE_VIN)


@pytest.mark.asyncio
async def test_non_json_body_raises_bad_response():
    def handler(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(VpicBadResponse):
        await make_client(handler, max_retries=0).decode(SAMPLE_VIN)
