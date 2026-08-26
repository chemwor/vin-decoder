"""Unit tests for the recalls / safety-ratings client.

MockTransport again, so parsing and the two-step ratings flow are exercised
against real httpx objects without touching the network.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.nhtsa import (
    NhtsaBadResponse,
    NhtsaClient,
    NhtsaUnavailable,
    NhtsaVehicleNotListed,
    rating_from_dict,
    rating_to_dict,
    recall_from_dict,
    recall_to_dict,
)
from tests.conftest import make_rating, make_recall

RECALLS_URL = "http://nhtsa.test/recalls"
RATINGS_URL = "http://nhtsa.test/SafetyRatings"


def make_client(handler, max_retries: int = 0) -> NhtsaClient:
    return NhtsaClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        recalls_base_url=RECALLS_URL,
        ratings_base_url=RATINGS_URL,
        max_retries=max_retries,
    )


RECALL_ROW = {
    "Manufacturer": "Ford Motor Company",
    "NHTSACampaignNumber": "18V471000",
    "parkIt": False,
    "parkOutSide": True,
    "overTheAirUpdate": False,
    "ReportReceivedDate": "16/07/2018",
    "Component": "POWER TRAIN:AUTOMATIC TRANSMISSION",
    "Summary": "Summary text",
    "Consequence": "Consequence text",
    "Remedy": "Remedy text",
}


# --- recalls ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_recalls_parses_lowercase_results_key():
    """This endpoint answers under `results`; SafetyRatings uses `Results`."""

    def handler(request):
        return httpx.Response(200, json={"Count": 1, "results": [RECALL_ROW]})

    recalls = await make_client(handler).recalls("2014", "FORD", "Escape")

    assert len(recalls) == 1
    assert recalls[0].campaign_number == "18V471000"
    assert recalls[0].park_outside is True


@pytest.mark.asyncio
async def test_report_date_is_read_as_day_first():
    """NHTSA returns 16/07/2018 meaning 16 July, not an invalid month 16."""

    def handler(request):
        return httpx.Response(200, json={"results": [RECALL_ROW]})

    recalls = await make_client(handler).recalls("2014", "FORD", "Escape")
    assert recalls[0].report_received_date == date(2018, 7, 16)


@pytest.mark.asyncio
async def test_unparseable_date_is_none_not_an_error():
    def handler(request):
        return httpx.Response(200, json={"results": [dict(RECALL_ROW, ReportReceivedDate="soon")]})

    recalls = await make_client(handler).recalls("2014", "FORD", "Escape")
    assert recalls[0].report_received_date is None


@pytest.mark.asyncio
async def test_no_recalls_is_an_empty_list_not_an_error():
    def handler(request):
        return httpx.Response(200, json={"Count": 0, "results": []})

    assert await make_client(handler).recalls("2020", "FORD", "Escape") == []


@pytest.mark.asyncio
async def test_server_error_raises_unavailable():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(NhtsaUnavailable):
        await make_client(handler).recalls("2014", "FORD", "Escape")


@pytest.mark.asyncio
async def test_unindexed_model_is_not_listed_rather_than_empty():
    """NHTSA answers 400 for models it files under another name.

    A 2015 Harley decodes as "Street Glide" but is indexed as FLHX, so the
    campaigns exist -- we just cannot address them. Returning [] here would
    clear a vehicle nobody checked.
    """

    def handler(request):
        return httpx.Response(400, json={"Count": 0, "results": []})

    with pytest.raises(NhtsaVehicleNotListed):
        await make_client(handler).recalls("2015", "HARLEY-DAVIDSON", "Street Glide")


@pytest.mark.asyncio
async def test_non_object_body_raises_bad_response():
    def handler(request):
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(NhtsaBadResponse):
        await make_client(handler).recalls("2014", "FORD", "Escape")


# --- safety ratings --------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_rating_follows_the_two_step_lookup():
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        if "modelyear" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "Count": 1,
                    "Results": [{"VehicleDescription": "2013 BMW 328 I 4 DR", "VehicleId": 7293}],
                },
            )
        return httpx.Response(200, json={"Results": [{"VehicleId": 7293, "OverallRating": "5"}]})

    rating = await make_client(handler).safety_rating("2013", "BMW", "328i")

    assert rating is not None
    assert rating.overall == "5"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_untested_vehicle_returns_none():
    def handler(request):
        return httpx.Response(200, json={"Count": 0, "Results": []})

    assert await make_client(handler).safety_rating("2013", "BMW", "328i") is None


@pytest.mark.asyncio
async def test_body_class_picks_the_matching_variant():
    """A sedan must not be given the coupe's rollover number."""
    requested: list[str] = []

    def handler(request):
        requested.append(str(request.url))
        if "modelyear" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "Results": [
                        {"VehicleDescription": "2013 BMW 328 I 2 DR RWD", "VehicleId": 111},
                        {"VehicleDescription": "2013 BMW 328 I 4 DR RWD", "VehicleId": 222},
                    ]
                },
            )
        return httpx.Response(200, json={"Results": [{"VehicleId": 222, "OverallRating": "5"}]})

    await make_client(handler).safety_rating("2013", "BMW", "328i", body_hint="Sedan/Saloon")

    assert "222" in requested[-1]


# --- cache serialization ---------------------------------------------------


def test_recall_round_trips_through_the_cache():
    original = make_recall("22V123000", park_it=True, report_received_date=date(2022, 3, 4))
    assert recall_from_dict(recall_to_dict(original)) == original


def test_recall_with_no_date_round_trips():
    original = make_recall(report_received_date=None)
    assert recall_from_dict(recall_to_dict(original)) == original


def test_rating_round_trips_through_the_cache():
    original = make_rating()
    assert rating_from_dict(rating_to_dict(original)) == original
