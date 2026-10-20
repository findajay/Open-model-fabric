"""API authentication, authorisation and rate limiting.

Credentials (all as ``Authorization: Bearer <token>``):

* **API keys** — ``auth.tokens[].token_ref`` secret references. They are resolved at
  startup and every ``auth.token_reload_s`` seconds, so a rotated secret file takes
  effect without a restart. Only SHA-256 digests are kept in memory and comparison is
  constant-time. ``expires_at`` ends a key's validity (rotation deadline); several
  entries may share a name, which is how a key is rotated without downtime.
* **OIDC JWTs** — when ``auth.oidc`` is configured (see :mod:`omf_api.oidc`).

Two roles:

* ``user``  — read endpoints, inference, conversations, benchmarks.
* ``admin`` — everything, including provider/settings/model changes, artifact uploads,
              audit log and secret-reference listing.

``auth.mode: none`` (development only, enforced by the config validator to localhost
binds) treats every caller as an ``admin`` named ``local-dev``.

Rate limits (``security.rate_limits``) are per-principal token buckets in this process:
with N replicas behind a load balancer the effective limit is up to N times higher.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, Request

from omf.bootstrap.config import AuthConfig, RateLimitConfig
from omf.core.domain.errors import (
    AuthenticationRequired,
    OMFError,
    PermissionDenied,
    RateLimited,
)
from omf.core.security.egress import EgressPolicy
from omf.core.services.secrets import SecretResolver
from omf_api.oidc import OidcValidator, looks_like_jwt

log = logging.getLogger(__name__)
Role = Literal["admin", "user"]
INFERENCE_PATHS = (
    "/api/v1/chat/completions",
    "/api/v1/chat/stream",
    "/api/v1/embeddings",
    "/api/v1/audio/",
)


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role


@dataclass(frozen=True)
class _Key:
    digest: bytes
    principal: Principal
    expires_at: datetime | None


class Authenticator:
    def __init__(
        self,
        config: AuthConfig,
        secrets: SecretResolver,
        egress: EgressPolicy | None = None,
        *,
        oidc: OidcValidator | None = None,
    ) -> None:
        self.mode = config.mode
        self._config = config
        self._secrets = secrets
        self._keys: list[_Key] = []
        self._loaded_at = 0.0
        self.oidc = oidc
        if oidc is None and config.oidc is not None and config.mode != "none":
            self.oidc = OidcValidator(config.oidc, egress or EgressPolicy())
        self.reload()

    def reload(self) -> None:
        """Re-resolve token secrets (picks up rotated secret files)."""
        keys: list[_Key] = []
        for token in self._config.tokens:
            try:
                value = self._secrets.resolve(token.token_ref).reveal()
            except OMFError as exc:
                log.warning(
                    "API token %s unavailable (%s); it will not authenticate", token.name, exc.code
                )
                continue
            if len(value) < 16:
                log.warning("API token %s is shorter than 16 characters; ignoring it", token.name)
                continue
            keys.append(
                _Key(
                    hashlib.sha256(value.encode()).digest(),
                    Principal(name=token.name, role=token.role),
                    token.expires_at,
                )
            )
        self._keys = keys
        self._loaded_at = time.monotonic()

    async def aclose(self) -> None:
        if self.oidc is not None:
            await self.oidc.aclose()

    async def authenticate(self, request: Request) -> Principal:
        if self.mode == "none":
            return Principal(name="local-dev", role="admin")
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationRequired("missing bearer token")
        if self.oidc is not None and looks_like_jwt(token):
            name, role = await self.oidc.principal(token)
            return Principal(name=name, role=role)  # type: ignore[arg-type]
        reload_s = self._config.token_reload_s
        if reload_s and time.monotonic() - self._loaded_at > reload_s:
            self.reload()
        digest = hashlib.sha256(token.encode()).digest()
        now = datetime.now(UTC)
        valid: _Key | None = None
        expired = False
        for key in self._keys:  # check all to keep timing uniform
            if hmac.compare_digest(key.digest, digest):
                if key.expires_at is not None and now >= _aware(key.expires_at):
                    expired = True
                elif valid is None:
                    valid = key
        # During a rotation the same secret may appear in an expired and a live entry:
        # any live entry wins; "expired" only when every matching entry has expired.
        if valid is not None:
            return valid.principal
        raise AuthenticationRequired("bearer token expired" if expired else "invalid bearer token")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class TokenBucket:
    """Classic token bucket per key: ``rate`` tokens/s, at most ``capacity`` stored."""

    def __init__(self, per_minute: int, burst: int | None = None) -> None:
        self.capacity = float(burst or per_minute)
        self.rate = per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> float:
        """Consume one token; return 0 if allowed, else seconds until one is available."""
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            self._prune(now)
            return 0.0
        self._buckets[key] = (tokens, now)
        return (1.0 - tokens) / self.rate

    def _prune(self, now: float) -> None:
        if len(self._buckets) > 50_000:  # forget keys whose bucket has refilled completely
            full = self.capacity / self.rate
            self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < full}


class RateLimiter:
    def __init__(self, config: RateLimitConfig) -> None:
        self.general = (
            TokenBucket(config.requests_per_minute, config.burst)
            if config.requests_per_minute
            else None
        )
        self.inference = (
            TokenBucket(config.inference_per_minute, config.burst)
            if config.inference_per_minute
            else None
        )

    def check(self, principal: Principal, path: str) -> None:
        buckets = [("requests", self.general)]
        if path.startswith(INFERENCE_PATHS):
            buckets.append(("inference", self.inference))
        for kind, bucket in buckets:
            if bucket is None:
                continue
            wait = bucket.take(principal.name)
            if wait:
                raise RateLimited(
                    f"rate limit exceeded ({kind}); retry later",
                    details={"limit": kind, "retry_after_s": max(1, math.ceil(wait))},
                )


def _authenticator(request: Request) -> Authenticator:
    auth: Authenticator = request.app.state.authenticator
    return auth


async def current_principal(request: Request) -> Principal:
    principal = await _authenticator(request).authenticate(request)
    request.state.principal = principal
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        limiter.check(principal, request.url.path)
    return principal


def require_admin(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if principal.role != "admin":
        raise PermissionDenied("this operation requires the admin role")
    return principal


UserDep = Annotated[Principal, Depends(current_principal)]
AdminDep = Annotated[Principal, Depends(require_admin)]
