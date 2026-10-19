"""Minimal JSON-over-HTTP client shared by REST-based compute adapters.

Translates transport and HTTP failures into :class:`ComputeError`; 404 can be requested
as ``None`` (the usual "does it exist?" check). Never follows redirects.
"""

from __future__ import annotations

import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from omf.core.domain.errors import ComputeError, OMFError

TokenSource = Callable[[], Awaitable[str]]


class RestClient:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        token: TokenSource | None = None,
        timeout_s: float = 30.0,
        verify: bool | str | ssl.SSLContext = True,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            verify=verify,
            follow_redirects=False,
            transport=transport,
            headers=headers or {},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        allow_404: bool = False,
        ok: tuple[int, ...] = (),
        **kwargs: Any,
    ) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        if self._token is not None:
            headers["Authorization"] = f"Bearer {await self._token()}"
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ComputeError(
                f"{self.name} unreachable ({type(exc).__name__})", retryable=True
            ) from exc
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 400 and response.status_code not in ok:
            raise self.error(response)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"_raw": response.text[:2000]}

    def error(self, response: httpx.Response) -> OMFError:
        message = ""
        try:
            body = response.json()
            err = body.get("error", body) if isinstance(body, dict) else body
            message = str(err.get("message", err) if isinstance(err, dict) else err)
        except ValueError:
            message = response.text
        return ComputeError(
            f"{self.name} returned HTTP {response.status_code}: {message[:300]}",
            details={"status": response.status_code},
            retryable=response.status_code in (429, 500, 502, 503, 504),
        )

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client
