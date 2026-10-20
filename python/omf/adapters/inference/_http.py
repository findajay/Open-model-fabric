"""Shared HTTP plumbing for network inference adapters.

* One ``httpx.AsyncClient`` per adapter instance, closed on ``aclose``.
* Redirects are never followed (a redirect could point at an internal address).
* The egress policy is re-checked before connecting (cached briefly) to narrow the
  DNS-rebinding window between configuration-time validation and use.
* Transport and HTTP errors are translated into domain errors here, at the adapter
  boundary, so services never see ``httpx`` exceptions.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, NoReturn

import httpx

from omf.core.domain.errors import (
    ModelNotFound,
    OMFError,
    ProviderAuthenticationFailed,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from omf.core.domain.models import EmbeddingRequest, Timeouts
from omf.core.security.egress import EgressPolicy

_EGRESS_CACHE_S = 60.0
MAX_ERROR_BODY = 300


class HttpEndpoint:
    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        timeouts: Timeouts,
        egress: EgressPolicy,
        headers: dict[str, str] | None = None,
        verify_tls: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        preflight_error: OMFError | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._preflight_error = preflight_error
        self.base_url = base_url.rstrip("/")
        self._egress = egress
        # None until the first successful check. (A 0.0 sentinel would skip the check on
        # hosts up for less than the cache window: time.monotonic() counts from boot.)
        self._egress_checked_at: float | None = None
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers or {},
            timeout=httpx.Timeout(
                connect=timeouts.connect_s,
                read=timeouts.read_s,
                write=timeouts.read_s,
                pool=timeouts.connect_s,
            ),
            follow_redirects=False,
            verify=verify_tls,
            transport=transport,
        )

    async def ensure_egress(self) -> None:
        if self._preflight_error is not None:
            raise self._preflight_error
        now = time.monotonic()
        if self._egress_checked_at is not None and now - self._egress_checked_at < _EGRESS_CACHE_S:
            return
        try:
            await self._egress.check_url(self.base_url)
        except OMFError as exc:
            if exc.retryable:  # DNS failure: report as unavailable, not as policy denial
                raise ProviderUnavailable(
                    f"provider {self.provider_id!r}: {exc.message}",
                    details={"provider_id": self.provider_id},
                ) from exc
            raise
        self._egress_checked_at = now

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ errors

    def translate(self, exc: Exception) -> OMFError:
        pid = self.provider_id
        details: dict[str, Any] = {"provider_id": pid}
        if isinstance(exc, OMFError):
            return exc
        if isinstance(exc, httpx.TimeoutException):
            phase = "connecting to" if isinstance(exc, httpx.ConnectTimeout) else "waiting for"
            return ProviderTimeout(f"timed out {phase} provider {pid!r}", details=details)
        if isinstance(
            exc,
            httpx.ConnectError
            | httpx.RemoteProtocolError
            | httpx.ReadError
            | httpx.WriteError
            | httpx.NetworkError,
        ):
            return ProviderUnavailable(
                f"provider {pid!r} is unreachable at {self.base_url} ({type(exc).__name__})",
                details=details,
            )
        if isinstance(exc, httpx.HTTPStatusError):
            return self.status_error(exc.response.status_code, _body_text(exc.response))
        return ProviderError(
            f"unexpected error talking to {pid!r}: {type(exc).__name__}", details=details
        )

    def status_error(self, status: int, body: str) -> OMFError:
        pid = self.provider_id
        message = _extract_message(body)
        details: dict[str, Any] = {"provider_id": pid, "upstream_status": status}
        if message:
            details["upstream_message"] = message
        if status in (401, 403):
            return ProviderAuthenticationFailed(
                f"provider {pid!r} rejected the credentials (HTTP {status})", details=details
            )
        if status == 404:
            return ModelNotFound(f"provider {pid!r}: {message or 'not found'}", details=details)
        if status in (408, 429, 502, 503, 504):
            return ProviderUnavailable(
                f"provider {pid!r} is temporarily unavailable (HTTP {status})", details=details
            )
        return ProviderError(
            f"provider {pid!r} returned HTTP {status}: {message or 'error'}", details=details
        )

    def raise_for(self, exc: Exception) -> NoReturn:
        raise self.translate(exc) from exc


def _body_text(response: httpx.Response) -> str:
    try:
        return response.text[:2000]
    except (httpx.ResponseNotRead, UnicodeDecodeError):
        return ""


def _extract_message(body: str) -> str:
    """Pull a human-readable message out of common error shapes, truncated."""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:MAX_ERROR_BODY]
    if isinstance(data, dict):
        err = data.get("error", data.get("detail", data.get("message")))
        if isinstance(err, dict):
            err = err.get("message", err)
        return str(err)[:MAX_ERROR_BODY]
    return str(data)[:MAX_ERROR_BODY]


async def read_error_body(response: httpx.Response) -> str:
    try:
        await response.aread()
    except httpx.HTTPError:
        return ""
    return _body_text(response)


def check_vectors(provider_id: str, vectors: Any, request: EmbeddingRequest) -> list[list[float]]:
    """Validate an engine's embeddings: one non-empty numeric vector per input, same size."""
    if not isinstance(vectors, list) or len(vectors) != len(request.inputs):
        raise ProviderError(
            f"provider {provider_id!r} returned "
            f"{len(vectors) if isinstance(vectors, list) else 'no'} embeddings "
            f"for {len(request.inputs)} inputs"
        )
    try:
        out = [[float(x) for x in v] for v in vectors]
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"provider {provider_id!r} returned non-numeric embeddings") from exc
    if not all(math.isfinite(x) for v in out for x in v):
        raise ProviderError(f"provider {provider_id!r} returned non-finite embeddings")
    if not out[0] or any(len(v) != len(out[0]) for v in out):
        raise ProviderError(f"provider {provider_id!r} returned embeddings of unequal size")
    return out
