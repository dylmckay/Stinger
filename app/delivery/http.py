"""The HTTP attempt: POST a payload to an endpoint and classify the outcome.

Produces the AttemptResult that record_attempt consumes. All the delivery-side
HTTP discipline lives here: a hard timeout ceiling, redirects disabled, the
SSRF guard with IP pinning, and the mapping from wire result to
(succeeded, retryable). Signing is layered on top via `extra_headers` — this
function does not know or care how the signature was produced.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from collections.abc import Mapping

import httpx

from app.delivery.record import AttemptResult, MAX_RESPONSE_BODY
from app.delivery.ssrf import SSRFError, resolve_and_validate

DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=5.0)
OVERALL_DEADLINE_S = 15.0                    # hard wall-clock cap < lease (30s)
NON_RETRYABLE_STATUSES = frozenset({410})    # 410 Gone: consumer says stop
MAX_RESPONSE_WIRE_BYTES = 65536   # wire-read ceiling; comfortably > MAX_RESPONSE_BODY bytes


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (RFC 9110: delta-seconds or HTTP-date) into
    seconds-from-now. None if absent/unparseable; a past date clamps to 0."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():                 # delta-seconds
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)

async def attempt_delivery(
    client: httpx.AsyncClient,
    *,
    url: str,
    payload: str,
    message_id: str,
    extra_headers: Mapping[str, str] | None = None,
    allow_private: bool = False,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> AttemptResult:
    start = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - start) * 1000)

    # 1. SSRF guard: resolve, validate every address, pin one.
    try:
        target = await resolve_and_validate(url, allow_private=allow_private)
    except SSRFError as e:
        return AttemptResult(
            succeeded=False, retryable=False,
            error=f"blocked: {e}", latency_ms=elapsed_ms(),
        )

    headers = {
        "content-type": "application/json",
        "user-agent": "Stinger/0.1",
        "webhook-id": message_id,
        "host": target.host_header,
        "accept-encoding": "identity",       # no compression → raw == decoded, no bomb path
    }
    if extra_headers:
        headers.update(extra_headers)

    # Connect to the pinned IP; preserve hostname for TLS SNI.
    request = client.build_request(
        "POST", target.connect_url,
        content=payload, headers=headers, timeout=timeout,
        extensions={"sni_hostname": target.host},
    )

    # 2. Execute under a hard overall deadline, streaming so a malicious receiver
    #    can't make us buffer an unbounded body. We cap bytes pulled off the wire,
    #    not just stored text: the deadline still kills a slow drip that never
    #    reaches the cap, so the body is now bounded in BOTH size and time.
    try:
        async with asyncio.timeout(OVERALL_DEADLINE_S):
            resp = await client.send(request, follow_redirects=False, stream=True)
            try:
                buf = bytearray()
                async for chunk in resp.aiter_raw():
                    buf += chunk
                    if len(buf) >= MAX_RESPONSE_WIRE_BYTES:
                        break
            finally:
                await resp.aclose()
    except (httpx.TimeoutException, TimeoutError):
        return AttemptResult(False, retryable=True, error="timeout", latency_ms=elapsed_ms())
    except httpx.HTTPError as e:
        return AttemptResult(
            False, retryable=True,
            error=f"{type(e).__name__}: {e}", latency_ms=elapsed_ms(),
        )

    body = bytes(buf).decode("utf-8", "replace")[:MAX_RESPONSE_BODY]
    latency = elapsed_ms()

    # 3. Classify.
    if 200 <= resp.status_code < 300:
        return AttemptResult(
            True, response_status=resp.status_code,
            response_body=body, latency_ms=latency,
        )
    return AttemptResult(
        False,
        retryable=resp.status_code not in NON_RETRYABLE_STATUSES,
        response_status=resp.status_code, response_body=body, latency_ms=latency,
        retry_after_seconds=_parse_retry_after(resp.headers.get("retry-after"))
    )