from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from legion.domain.errors import (
    ContextExhausted,
    MalformedModelResponse,
    ModelAuthError,
    ModelRequestRejected,
    ModelTimeout,
    ModelUnavailable,
    RateLimited,
)

_CONTEXT_MARKERS = (
    "context_length_exceeded",
    "prompt is too long",
    "maximum context length",
    "context window",
)


async def post_json(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise ModelTimeout(f"request to {url} timed out") from exc
    except httpx.TransportError as exc:
        raise ModelUnavailable(f"cannot reach {url}: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        raise _error_for(response, headers)
    try:
        data = loads_strict(response.text)
    except json.JSONDecodeError as exc:
        raise MalformedModelResponse("response body is not JSON") from exc
    if not isinstance(data, dict):
        raise MalformedModelResponse("response body is not a JSON object")
    return data


def loads_strict(text: str) -> Any:
    # python's json accepts NaN/Infinity, we can't hash those
    return json.loads(text, parse_constant=_reject_constant)


def _reject_constant(name: str) -> Any:
    raise MalformedModelResponse(f"response contains {name}, which is not valid JSON")


def token_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MalformedModelResponse(f"bad token count in usage: {value!r}"[:120])
    return value


def _error_for(response: httpx.Response, sent: Mapping[str, str]) -> Exception:
    status = response.status_code
    detail = _detail(response)
    # Some servers echo the request, auth header included, in their error text.
    for value in sent.values():
        for part in {value, value.rpartition(" ")[2]}:
            if len(part) >= 8:
                detail = detail.replace(part, "[redacted]")
    if status in (401, 403):
        return ModelAuthError(f"provider refused credentials ({status}): {detail}")
    if status == 429:
        return RateLimited(f"rate limited: {detail}", _retry_after(response))
    if status == 408:
        return ModelTimeout(f"provider timed out (408): {detail}")
    if status >= 500:
        return ModelUnavailable(f"provider error ({status}): {detail}")
    if any(marker in detail.lower() for marker in _CONTEXT_MARKERS):
        return ContextExhausted(f"context window exceeded: {detail}")
    return ModelRequestRejected(f"provider rejected the request ({status}): {detail}")


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except json.JSONDecodeError:
        return response.text[:300]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        code = error.get("code") or error.get("type")
        text = str(error["message"])
        return f"{code}: {text}"[:300] if code else text[:300]
    return str(data)[:300]


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
