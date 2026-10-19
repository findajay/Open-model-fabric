"""Google service-account access tokens (shared by the Drive storage and GCE compute adapters)."""

from __future__ import annotations

import asyncio
import json

from omf.core.domain.errors import ConfigurationError
from omf.core.ports.secrets import SecretValue


class ServiceAccountTokenSource:
    """Mints OAuth access tokens from a service-account JSON using ``google-auth``."""

    def __init__(self, credentials_json: SecretValue, scope: str) -> None:
        try:
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ConfigurationError(
                "Google adapters need the 'gdrive' extra: pip install 'open-model-fabric[gdrive]'"
            ) from exc
        try:
            info = json.loads(credentials_json.reveal())
        except ValueError as exc:
            raise ConfigurationError("Google credentials secret is not valid JSON") from exc
        self._credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            info, scopes=[scope]
        )
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if not self._credentials.valid:
                from google.auth.transport.requests import Request

                await asyncio.to_thread(self._credentials.refresh, Request())
            return str(self._credentials.token)
