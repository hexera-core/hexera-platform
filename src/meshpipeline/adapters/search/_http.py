# Responsibility: Make the bounded HTTP request the search adapters share.
# Boundaries: one request with its timeout and size bound; it parses no provider's result shape.
from __future__ import annotations

from typing import Any

import httpx

from meshpipeline.contracts.search import (
    WebSearchInvalidResponse,
    WebSearchTimeout,
    WebSearchUnavailable,
)


def request_json(
    method: str,
    url: str,
    *,
    timeout_s: float,
    provider: str,
    params: dict | None = None,
    json: dict | None = None,
    headers: dict | None = None,
) -> Any:
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            resp = client.request(method, url, params=params, json=json, headers=headers)
            resp.raise_for_status()
    except httpx.TimeoutException as exc:
        # NB: TimeoutException is a subclass of httpx.HTTPError - catch it FIRST.
        raise WebSearchTimeout(f"{provider} search timed out after {timeout_s}s") from exc
    except httpx.HTTPStatusError as exc:
        raise WebSearchUnavailable(
            f"{provider} search returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        # Connection/transport error - deliberately omit str(exc): keep provider detail
        # (and any URL-embedded token, though we never put one there) out of the message.
        raise WebSearchUnavailable(
            f"{provider} search unreachable ({type(exc).__name__})") from exc

    try:
        return resp.json()
    except ValueError as exc:
        raise WebSearchInvalidResponse(f"{provider} search returned a non-JSON body") from exc
