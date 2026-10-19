"""API authentication and authorisation.

Bearer tokens are configured as secret references (``auth.tokens[].token_ref``); at
startup they are resolved and only their SHA-256 digests are kept in memory. Comparison
is constant-time. Two roles:

* ``user``  — read endpoints, inference, conversations, benchmarks.
* ``admin`` — everything, including provider/settings/model changes, artifact uploads,
              audit log and secret-reference listing.

``auth.mode: none`` (development only, enforced by the config validator to localhost
binds) treats every caller as an ``admin`` named ``local-dev``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Request

from omf.bootstrap.config import AuthConfig
from omf.core.domain.errors import AuthenticationRequired, OMFError, PermissionDenied
from omf.core.services.secrets import SecretResolver

log = logging.getLogger(__name__)
Role = Literal["admin", "user"]


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role


class Authenticator:
    def __init__(self, config: AuthConfig, secrets: SecretResolver) -> None:
        self.mode = config.mode
        self._digests: list[tuple[bytes, Principal]] = []
        for token in config.tokens:
            try:
                value = secrets.resolve(token.token_ref).reveal()
            except OMFError as exc:
                log.warning(
                    "API token %s unavailable (%s); it will not authenticate", token.name, exc.code
                )
                continue
            if len(value) < 16:
                log.warning("API token %s is shorter than 16 characters; ignoring it", token.name)
                continue
            self._digests.append(
                (
                    hashlib.sha256(value.encode()).digest(),
                    Principal(name=token.name, role=token.role),
                )
            )

    def authenticate(self, request: Request) -> Principal:
        if self.mode == "none":
            return Principal(name="local-dev", role="admin")
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationRequired("missing bearer token")
        digest = hashlib.sha256(token.strip().encode()).digest()
        match: Principal | None = None
        for known, principal in self._digests:  # check all to keep timing uniform
            if hmac.compare_digest(known, digest):
                match = principal
        if match is None:
            raise AuthenticationRequired("invalid bearer token")
        return match


def _authenticator(request: Request) -> Authenticator:
    auth: Authenticator = request.app.state.authenticator
    return auth


def current_principal(request: Request) -> Principal:
    principal = _authenticator(request).authenticate(request)
    request.state.principal = principal
    return principal


def require_admin(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if principal.role != "admin":
        raise PermissionDenied("this operation requires the admin role")
    return principal


UserDep = Annotated[Principal, Depends(current_principal)]
AdminDep = Annotated[Principal, Depends(require_admin)]
