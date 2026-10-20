"""OpenID Connect bearer-token validation (single sign-on).

Validates JWTs issued by the configured OIDC provider: the signature against the
provider's JWKS, plus ``iss``, ``aud``, ``exp`` (with leeway) and the algorithm allowlist.
Algorithm ``none`` and HMAC algorithms are never accepted, so a token cannot be signed
with a public key. The JWKS is fetched through the egress policy, cached for
``jwks_cache_s``, and refreshed early (at most every 30 s) when a token names an
unknown key id, which handles key rotation at the provider.

Roles come from ``role_claim`` (a dotted path to a string or list, e.g. Keycloak's
``realm_access.roles``): any value in ``admin_values`` grants ``admin``; otherwise any
value in ``user_values`` (or any valid token, if that list is empty) grants ``user``.
Principal names are prefixed with ``sso:`` so they cannot collide with API-key names.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from omf.bootstrap.config import OidcConfig
from omf.core.domain.errors import AuthenticationRequired, ConfigurationError, PermissionDenied
from omf.core.security.egress import EgressPolicy

try:  # optional extra: pip install 'open-model-fabric[oidc]'
    import jwt
except ImportError:  # pragma: no cover - checked at startup
    jwt = None  # type: ignore[assignment]

_FORCED_REFRESH_S = 30.0


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and token.startswith("ey")


def _claim(claims: dict[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class OidcValidator:
    def __init__(
        self,
        config: OidcConfig,
        egress: EgressPolicy,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if jwt is None:
            raise ConfigurationError("auth.oidc needs PyJWT: pip install 'open-model-fabric[oidc]'")
        self.config = config
        self._egress = egress
        self._client = httpx.AsyncClient(timeout=10.0, follow_redirects=False, transport=transport)
        self._keys: dict[str | None, Any] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ JWKS

    async def _get_json(self, url: str) -> dict[str, Any]:
        await self._egress.check_url(url)
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthenticationRequired(
                "identity provider unavailable", details={"cause": type(exc).__name__}
            ) from exc
        if not isinstance(body, dict):
            raise AuthenticationRequired("identity provider returned an invalid document")
        return body

    async def _jwks_url(self) -> str:
        if self.config.jwks_url:
            return self.config.jwks_url
        discovery = await self._get_json(f"{self.config.issuer}/.well-known/openid-configuration")
        if str(discovery.get("issuer", "")).rstrip("/") != self.config.issuer:
            raise AuthenticationRequired("identity provider issuer mismatch")
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise AuthenticationRequired("identity provider publishes no jwks_uri")
        return jwks_uri

    async def _refresh(self) -> None:
        jwks = await self._get_json(await self._jwks_url())
        keys: dict[str | None, Any] = {}
        for raw in jwks.get("keys") or []:
            if raw.get("use", "sig") != "sig":
                continue
            try:
                keys[raw.get("kid")] = jwt.PyJWK(raw)
            except jwt.PyJWTError:
                continue  # unsupported key type: ignore it, others may work
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def _key(self, kid: str | None) -> Any:
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            stale = not self._keys or age > self.config.jwks_cache_s
            unknown = kid not in self._keys and age > _FORCED_REFRESH_S
            if stale or unknown:
                await self._refresh()
            if kid in self._keys:
                return self._keys[kid]
            if kid is None and len(self._keys) == 1:
                return next(iter(self._keys.values()))
        raise AuthenticationRequired("token signed with an unknown key")

    # ------------------------------------------------------------------ tokens

    async def principal(self, token: str) -> tuple[str, str]:
        """Validate ``token``; return ``(name, role)`` or raise."""
        cfg = self.config
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthenticationRequired("malformed token") from exc
        if header.get("alg") not in cfg.algorithms:
            raise AuthenticationRequired("token algorithm not allowed")
        key = await self._key(header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=list(cfg.algorithms),
                audience=cfg.audience,
                issuer=cfg.issuer,
                leeway=cfg.leeway_s,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationRequired("token expired") from exc
        except jwt.PyJWTError as exc:
            raise AuthenticationRequired(f"invalid token: {type(exc).__name__}") from exc
        raw_roles = _claim(claims, cfg.role_claim)
        roles = {raw_roles} if isinstance(raw_roles, str) else set(raw_roles or [])
        if roles & set(cfg.admin_values):
            role = "admin"
        elif not cfg.user_values or roles & set(cfg.user_values):
            role = "user"
        else:
            raise PermissionDenied("token grants no Open Model Fabric role")
        name = str(claims.get(cfg.name_claim) or claims["sub"])[:120]
        return f"sso:{name}", role
