"""SSO (OIDC), API-key rotation and expiry, and rate limiting.

The identity provider is a real HTTP server on loopback that serves OpenID discovery and
a JWKS; tokens are signed with freshly generated RSA keys. Nothing is mocked inside the
validator.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from omf.bootstrap.config import AppConfig
from tests.conftest import ServerThread

jwt = pytest.importorskip("jwt")
rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")

AUDIENCE = "omf-api"
ADMIN_KEY = "admin-key-0123456789abcdef"


class IdentityProvider:
    """Minimal OIDC issuer: discovery + JWKS, with key rotation."""

    def __init__(self) -> None:
        self.keys: dict[str, Any] = {}
        self.jwks_requests = 0
        self.server = ServerThread(
            Starlette(
                routes=[
                    Route("/.well-known/openid-configuration", self.discovery),
                    Route("/jwks", self.jwks),
                ]
            )
        )
        self.rotate("k1")

    @property
    def issuer(self) -> str:
        return self.server.url

    def rotate(self, kid: str) -> None:
        self.keys = {kid: rsa.generate_private_key(public_exponent=65537, key_size=2048)}

    async def discovery(self, _: Request) -> JSONResponse:
        return JSONResponse({"issuer": self.issuer, "jwks_uri": f"{self.issuer}/jwks"})

    async def jwks(self, _: Request) -> JSONResponse:
        self.jwks_requests += 1
        keys = []
        for kid, key in self.keys.items():
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
            keys.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return JSONResponse({"keys": keys})

    def token(self, **overrides: Any) -> str:
        kid = overrides.pop("kid", next(iter(self.keys)))
        key = overrides.pop("key", self.keys.get(kid))
        claims = {
            "iss": self.issuer,
            "aud": AUDIENCE,
            "sub": "user-123",
            "preferred_username": "alice",
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "realm_access": {"roles": ["omf-user"]},
            **overrides,
        }
        return str(jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid}))


@pytest.fixture
def idp() -> Iterator[IdentityProvider]:
    provider = IdentityProvider()
    with provider.server:
        yield provider


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def oidc_client(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    idp: IdentityProvider,
    **oidc: Any,
) -> TestClient:
    config = make_config(
        auth={
            "mode": "api_key",
            "tokens": [{"name": "root", "role": "admin", "token_ref": "env:OMF_T_ADMIN"}],
            "oidc": {
                "issuer": idp.issuer,
                "audience": AUDIENCE,
                "role_claim": "realm_access.roles",
                **oidc,
            },
        }
    )
    return make_client(config, env={"OMF_T_ADMIN": ADMIN_KEY})


def test_oidc_tokens_authenticate_and_map_roles(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    idp: IdentityProvider,
) -> None:
    client = oidc_client(make_config, make_client, idp, user_values=["omf-user"])
    user = bearer(idp.token())
    assert client.get("/api/v1/system", headers=user).json()["data"]["principal"] == "sso:alice"
    assert client.get("/api/v1/audit", headers=user).status_code == 403  # admin only
    admin = bearer(idp.token(realm_access={"roles": ["omf-admin"]}))
    assert client.get("/api/v1/audit", headers=admin).status_code == 200
    # Conversations belong to the SSO principal.
    created = client.post("/api/v1/conversations", headers=user, json={"title": "t"}).json()
    assert created["data"]["owner"] == "sso:alice"
    # A valid token without an OMF role is refused; API keys keep working alongside.
    norole = client.get("/api/v1/system", headers=bearer(idp.token(realm_access={})))
    assert norole.status_code == 403
    assert client.get("/api/v1/audit", headers=bearer(ADMIN_KEY)).status_code == 200


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"exp": int(time.time()) - 600}, "token expired"),
        ({"aud": "someone-else"}, "InvalidAudienceError"),
        ({"iss": "https://evil.example.com"}, "InvalidIssuerError"),
        ({"kid": "nope"}, "unknown key"),
    ],
)
def test_oidc_rejects_invalid_tokens(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    idp: IdentityProvider,
    overrides: dict[str, Any],
    message: str,
) -> None:
    client = oidc_client(make_config, make_client, idp)
    if overrides.get("kid") == "nope":
        overrides["key"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = client.get("/api/v1/system", headers=bearer(idp.token(**overrides)))
    assert response.status_code == 401, response.text
    assert message in response.json()["error"]["message"]


def test_oidc_refuses_algorithm_confusion(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    idp: IdentityProvider,
) -> None:
    client = oidc_client(make_config, make_client, idp)
    claims = {"iss": idp.issuer, "aud": AUDIENCE, "sub": "x", "exp": int(time.time()) + 60}
    public_pem = (
        idp.keys["k1"]
        .public_key()
        .public_bytes(
            encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
            format=__import__(
                "cryptography"
            ).hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    # Classic confusion attack: HMAC-sign with the RSA public key (PyJWT refuses to build
    # this, so it is assembled by hand), and the unsigned "none" algorithm.
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signing_input = (
        b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
        + "."
        + b64(json.dumps(claims).encode())
    )
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    forged = [
        f"{signing_input}.{b64(signature)}",
        jwt.encode(claims, None, algorithm="none", headers={"kid": "k1"}),
    ]
    for token in forged:
        response = client.get("/api/v1/system", headers=bearer(token))
        assert response.status_code == 401
        assert "algorithm not allowed" in response.json()["error"]["message"]


def test_oidc_follows_provider_key_rotation(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    idp: IdentityProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omf_api.oidc._FORCED_REFRESH_S", 0.0)
    client = oidc_client(make_config, make_client, idp)
    assert client.get("/api/v1/system", headers=bearer(idp.token())).status_code == 200
    fetched = idp.jwks_requests
    assert client.get("/api/v1/system", headers=bearer(idp.token())).status_code == 200
    assert idp.jwks_requests == fetched  # cached
    idp.rotate("k2")
    assert client.get("/api/v1/system", headers=bearer(idp.token())).status_code == 200
    assert idp.jwks_requests == fetched + 1  # unknown kid triggered one refresh


def test_oidc_config_requires_https_issuer(make_config: Callable[..., AppConfig]) -> None:
    from omf.core.domain.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        make_config(
            auth={"mode": "api_key", "oidc": {"issuer": "http://idp.example.com", "audience": "a"}}
        )


# ---------------------------------------------------------------------- API keys


def test_api_key_rotation_without_restart_and_expiry(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    tmp_path: Path,
) -> None:
    secret = tmp_path / "admin_token"
    secret.write_text("first-key-0123456789abcdef")
    config = make_config(
        security={"egress": {"allow_loopback": True}, "secret_dirs": [str(tmp_path)]},
        auth={
            "mode": "api_key",
            "token_reload_s": 0.05,
            "tokens": [
                {"name": "ops", "role": "admin", "token_ref": f"file:{secret}"},
                {
                    "name": "old",
                    "role": "user",
                    "token_ref": "env:OMF_T_OLD",
                    "expires_at": "2020-01-01T00:00:00Z",
                },
            ],
        },
    )
    client = make_client(config, env={"OMF_T_OLD": "old-key-0123456789abcdef"})
    assert (
        client.get("/api/v1/audit", headers=bearer("first-key-0123456789abcdef")).status_code == 200
    )
    secret.write_text("second-key-0123456789abcdef")  # rotated on disk (e.g. a k8s Secret)
    time.sleep(0.1)
    assert (
        client.get("/api/v1/audit", headers=bearer("first-key-0123456789abcdef")).status_code == 401
    )
    assert (
        client.get("/api/v1/audit", headers=bearer("second-key-0123456789abcdef")).status_code
        == 200
    )
    expired = client.get("/api/v1/system", headers=bearer("old-key-0123456789abcdef"))
    assert expired.status_code == 401 and "expired" in expired.json()["error"]["message"]


def test_live_entry_wins_over_an_expired_entry_with_the_same_secret(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    # Found while testing configs/laptop-security.example.yaml: the expired entry used
    # to shadow the live one, rejecting a valid key mid-rotation.
    entry = {"role": "user", "token_ref": "env:OMF_T_SHARED"}
    config = make_config(
        auth={
            "mode": "api_key",
            "tokens": [
                {"name": "live", **entry},
                {"name": "old", **entry, "expires_at": "2020-01-01T00:00:00Z"},
            ],
        }
    )
    client = make_client(config, env={"OMF_T_SHARED": "shared-key-0123456789abcdef"})
    response = client.get("/api/v1/system", headers=bearer("shared-key-0123456789abcdef"))
    assert response.status_code == 200 and response.json()["data"]["principal"] == "live"


# ---------------------------------------------------------------------- rate limits


def test_rate_limits_per_principal_and_for_inference(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    config = make_config(
        security={
            "egress": {"allow_loopback": True},
            "rate_limits": {"requests_per_minute": 4, "inference_per_minute": 2},
        },
        auth={
            "mode": "api_key",
            "tokens": [
                {"name": "a", "role": "user", "token_ref": "env:OMF_T_A"},
                {"name": "b", "role": "user", "token_ref": "env:OMF_T_B"},
            ],
        },
    )
    keys = {"OMF_T_A": "a-key-0123456789abcdef", "OMF_T_B": "b-key-0123456789abcdef"}
    client = make_client(config, env=keys)
    a, b = bearer(keys["OMF_T_A"]), bearer(keys["OMF_T_B"])
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/api/v1/chat/completions", headers=a, json=body).status_code == 200
    assert client.post("/api/v1/chat/completions", headers=a, json=body).status_code == 200
    limited = client.post("/api/v1/chat/completions", headers=a, json=body)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert limited.json()["error"]["details"]["limit"] == "inference"
    assert int(limited.headers["Retry-After"]) >= 1
    assert client.get("/api/v1/system", headers=a).status_code == 200  # 4th general request
    general = client.get("/api/v1/system", headers=a)  # 5th: general bucket used up
    assert general.status_code == 429 and general.json()["error"]["details"]["limit"] == "requests"
    assert client.get("/api/v1/system", headers=b).status_code == 200  # other principal
    assert client.get("/health/live").status_code == 200  # probes are never limited
