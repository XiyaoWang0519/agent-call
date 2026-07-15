from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.settings import Settings

logger = logging.getLogger(__name__)

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_SEARCH_RESULT_LIMIT = 10
EXA_TOOL_OUTPUT_MAX_BYTES = 16 * 1024


class ExaSearchError(RuntimeError):
    """A stable, non-secret failure that is safe to return to the voice model."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ExaSearchResult:
    output: dict[str, Any]
    request_id: str | None
    search_type: str | None
    result_count: int
    output_bytes: int
    cost_dollars: float | None


def _serialized_size(value: dict[str, Any]) -> int:
    # RealtimeBridge currently uses json.dumps with these defaults for function output.
    return len(json.dumps(value).encode("utf-8"))


def _bounded_string(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped if len(stripped) <= limit else stripped[:limit].rstrip()


def _normalize_result(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    url = _bounded_string(raw.get("url"), 2048)
    if url is None:
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    result: dict[str, Any] = {
        "title": _bounded_string(raw.get("title"), 500) or parsed.netloc,
        "url": url,
        "highlights": [],
    }
    published_date = _bounded_string(raw.get("publishedDate"), 64)
    if published_date is not None:
        result["published_date"] = published_date
    author = _bounded_string(raw.get("author"), 300)
    if author is not None:
        result["author"] = author

    highlights = raw.get("highlights")
    if isinstance(highlights, list):
        result["highlights"] = [
            highlight
            for value in highlights
            if (highlight := _bounded_string(value, 8000)) is not None
        ]
    return result


def _trim_at_word(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    candidate = value[:limit].rstrip()
    if " " in candidate:
        candidate = candidate.rsplit(" ", 1)[0].rstrip()
    return f"{candidate}…" if candidate else ""


def _fit_tool_output(results: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {"ok": True, "results": results}
    if _serialized_size(output) <= EXA_TOOL_OUTPUT_MAX_BYTES:
        return output

    # Keep all ranked result metadata when possible, then spend the remaining budget on
    # evidence from the highest-ranked results. This ceiling protects Realtime context size
    # without changing Exa's highest-quality highlights=true retrieval behavior.
    original_highlights = [list(result["highlights"]) for result in results]
    for result in results:
        result["highlights"] = []
    while len(results) > 1 and _serialized_size(output) > EXA_TOOL_OUTPUT_MAX_BYTES:
        results.pop()
        original_highlights.pop()

    for result, highlights in zip(results, original_highlights, strict=True):
        for highlight in highlights:
            result["highlights"].append(highlight)
            if _serialized_size(output) <= EXA_TOOL_OUTPUT_MAX_BYTES:
                continue
            result["highlights"].pop()

            low = 0
            high = len(highlight)
            best = ""
            while low <= high:
                midpoint = (low + high) // 2
                candidate = _trim_at_word(highlight, midpoint)
                result["highlights"].append(candidate)
                fits = _serialized_size(output) <= EXA_TOOL_OUTPUT_MAX_BYTES
                result["highlights"].pop()
                if fits:
                    best = candidate
                    low = midpoint + 1
                else:
                    high = midpoint - 1
            if best:
                result["highlights"].append(best)
            return output
    return output


class ExaSearchClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._api_key = Settings.reveal(settings.exa_api_key)
        self._timeout_seconds = settings.exa_search_timeout_seconds
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.exa_search_timeout_seconds,
                connect=min(1.0, settings.exa_search_timeout_seconds),
            ),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=60,
            ),
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def search(self, query: str) -> ExaSearchResult:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._http_client.post(
                    EXA_SEARCH_URL,
                    headers={
                        "x-api-key": self._api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": query,
                        "type": "auto",
                        "numResults": EXA_SEARCH_RESULT_LIMIT,
                        "moderation": True,
                        "contents": {"highlights": True},
                    },
                )
                response.raise_for_status()
        except TimeoutError as exc:
            raise ExaSearchError("search_timeout") from exc
        except httpx.TimeoutException as exc:
            raise ExaSearchError("search_timeout") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Exa search HTTP failure status=%s", status)
            if status in {401, 402, 403}:
                code = "search_configuration_error"
            elif status == 429:
                code = "search_rate_limited"
            else:
                code = "search_unavailable"
            raise ExaSearchError(code) from exc
        except httpx.HTTPError as exc:
            raise ExaSearchError("search_unavailable") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ExaSearchError("search_invalid_response") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ExaSearchError("search_invalid_response")

        raw_results = payload["results"]
        normalized = [
            result
            for raw in raw_results[:EXA_SEARCH_RESULT_LIMIT]
            if (result := _normalize_result(raw)) is not None
        ]
        output = _fit_tool_output(normalized)
        cost_data = payload.get("costDollars")
        cost = cost_data.get("total") if isinstance(cost_data, dict) else None
        return ExaSearchResult(
            output=output,
            request_id=_bounded_string(payload.get("requestId"), 200),
            search_type=_bounded_string(
                payload.get("resolvedSearchType") or payload.get("searchType"), 100
            ),
            result_count=len(output["results"]),
            output_bytes=_serialized_size(output),
            cost_dollars=(
                float(cost)
                if isinstance(cost, int | float) and not isinstance(cost, bool)
                else None
            ),
        )
