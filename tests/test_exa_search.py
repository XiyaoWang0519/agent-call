from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.exa_search import (
    EXA_SEARCH_URL,
    EXA_TOOL_OUTPUT_MAX_BYTES,
    ExaSearchClient,
    ExaSearchError,
)


@pytest.mark.asyncio
async def test_exa_search_uses_locked_voice_parameters_and_compact_result(settings):
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["request"] = request
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "requestId": "request_123",
                "resolvedSearchType": "auto",
                "results": [
                    {
                        "title": f"Result {index}",
                        "url": f"https://example.com/{index}",
                        "publishedDate": "2026-07-15T00:00:00.000Z",
                        "author": "Example Author",
                        "highlights": [f"Evidence for result {index}."],
                        "text": "Full page text must not reach Realtime.",
                        "favicon": "https://example.com/favicon.ico",
                    }
                    for index in range(12)
                ],
                "costDollars": {"total": 0.007},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ExaSearchClient(settings, http_client=http_client)
        result = await client.search("latest Example Clinic hours")

    request = observed["request"]
    assert isinstance(request, httpx.Request)
    assert request.url == EXA_SEARCH_URL
    assert request.headers["x-api-key"] == "exa-test"
    assert observed["body"] == {
        "query": "latest Example Clinic hours",
        "type": "auto",
        "numResults": 10,
        "moderation": True,
        "contents": {"highlights": True},
    }
    assert result.request_id == "request_123"
    assert result.search_type == "auto"
    assert result.result_count == 10
    assert result.cost_dollars == 0.007
    assert result.output["results"][0] == {
        "title": "Result 0",
        "url": "https://example.com/0",
        "published_date": "2026-07-15T00:00:00.000Z",
        "author": "Example Author",
        "highlights": ["Evidence for result 0."],
    }
    assert "text" not in result.output["results"][0]


@pytest.mark.asyncio
async def test_exa_search_caps_realtime_tool_output_without_lowering_result_count(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": f"Result {index}",
                        "url": f"https://example.com/{index}",
                        "highlights": ["relevant evidence " * 1000],
                    }
                    for index in range(10)
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await ExaSearchClient(settings, http_client=http_client).search("large response")

    assert result.result_count == 10
    assert result.output_bytes <= EXA_TOOL_OUTPUT_MAX_BYTES
    assert len(json.dumps(result.output).encode("utf-8")) <= EXA_TOOL_OUTPUT_MAX_BYTES
    assert result.output["results"][0]["highlights"]


@pytest.mark.asyncio
async def test_exa_search_has_total_wall_clock_deadline(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ExaSearchClient(settings, http_client=http_client)
        client._timeout_seconds = 0.01
        with pytest.raises(ExaSearchError, match="search_timeout") as raised:
            await client.search("a query")

    assert raised.value.code == "search_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "search_configuration_error"),
        (402, "search_configuration_error"),
        (403, "search_configuration_error"),
        (429, "search_rate_limited"),
        (500, "search_unavailable"),
    ],
)
async def test_exa_search_maps_http_failures_to_safe_codes(settings, status, expected_code):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request, json={"error": "secret provider detail"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(ExaSearchError) as raised:
            await ExaSearchClient(settings, http_client=http_client).search("a query")

    assert raised.value.code == expected_code
    assert "secret provider detail" not in str(raised.value)


@pytest.mark.asyncio
async def test_exa_search_rejects_malformed_provider_response(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"unexpected": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(ExaSearchError, match="search_invalid_response"):
            await ExaSearchClient(settings, http_client=http_client).search("a query")


@pytest.mark.asyncio
async def test_exa_search_ignores_malformed_optional_cost_metadata(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"results": [], "costDollars": "unexpected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await ExaSearchClient(settings, http_client=http_client).search("a query")

    assert result.cost_dollars is None
