"""Azure Blob adapter specifics: Shared Key signing, auth modes, egress, errors.

The storage contract suite (tests/contract) covers the port behaviour against an
in-memory fake and, when ``OMF_TEST_AZURITE_URL`` is set, against a real Azurite.
"""

from __future__ import annotations

import base64
import io
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import cmp_to_key
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from omf.adapters.storage.azure_blob import (
    API_VERSION,
    AzureBlobConfig,
    AzureBlobStorageAdapter,
    _collate,
    _parse_listing,
    shared_key_signature,
    string_to_sign,
)
from omf.bootstrap.config import AppConfig
from omf.core.domain.errors import EgressDenied, StorageError
from omf.core.domain.models import HealthStatus
from omf.core.ports.secrets import SecretValue
from omf.core.security.egress import EgressPolicy
from tests.contract.fake_azure_blob import ACCOUNT, CONTAINER, FakeAzureBlob

AZURITE_URL = os.environ.get("OMF_TEST_AZURITE_URL")
AZURITE_KEY = os.environ.get(
    "OMF_TEST_AZURITE_KEY",
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==",
)
LOCAL = EgressPolicy(allow_loopback=True)


def config(**overrides: object) -> AzureBlobConfig:
    raw: dict[str, object] = {
        "account_name": ACCOUNT,
        "container": CONTAINER,
        "endpoint_url": f"http://127.0.0.1:10000/{ACCOUNT}",
    }
    raw.update(overrides)
    return AzureBlobConfig.model_validate(raw)


def test_string_to_sign_matches_the_documented_layout() -> None:
    # Expected value derived by hand from the Shared Key format: verb, 11 standard
    # headers, sorted x-ms-* headers, /account + encoded path, sorted lowercase query.
    url = "https://acct.blob.core.windows.net/c/omf/a%2Bb.bin?comp=block&blockid=MDA%3D"
    headers = {
        "x-ms-version": "2023-11-03",
        "x-ms-date": "Mon, 20 Oct 2026 00:00:00 GMT",
        "Content-Length": "5",
        "Content-Type": "text/plain",
        "x-ms-meta-omf_sha256": "abc",
    }
    assert string_to_sign("acct", "PUT", url, headers) == (
        "PUT\n"
        "\n\n5\n\ntext/plain\n\n\n\n\n\n\n"
        "x-ms-date:Mon, 20 Oct 2026 00:00:00 GMT\n"
        "x-ms-meta-omf_sha256:abc\n"
        "x-ms-version:2023-11-03\n"
        "/acct/c/omf/a%2Bb.bin\nblockid:MDA=\ncomp:block"
    )
    # A zero Content-Length is signed as an empty value.
    zero = string_to_sign("acct", "PUT", url, {**headers, "Content-Length": "0"})
    assert zero.split("\n")[3] == ""
    expected = base64.b64encode(__import__("hmac").new(b"key", b"abc", "sha256").digest()).decode()
    assert shared_key_signature(base64.b64encode(b"key").decode(), "abc") == expected


def test_header_collation_ignores_hyphens_like_the_service() -> None:
    names = ["x-ms-meta-a-c", "x-ms-meta-ab", "x-ms-meta-a_b", "x-ms-date"]
    ordered = sorted(names, key=cmp_to_key(_collate))
    # Plain sorting would put "a-c" first ('-' < 'b'); the service ignores '-' at level 0.
    assert ordered == ["x-ms-date", "x-ms-meta-a_b", "x-ms-meta-ab", "x-ms-meta-a-c"]
    assert sorted(names) != ordered


def test_config_requires_exactly_one_auth_method_and_valid_names() -> None:
    assert config(account_key_ref="env:K").endpoint == f"http://127.0.0.1:10000/{ACCOUNT}"
    assert (
        AzureBlobConfig(account_name="acct", container="models", sas_token_ref="env:S").endpoint
        == "https://acct.blob.core.windows.net"
    )
    for bad in (
        {},  # no auth
        {"account_key_ref": "env:K", "sas_token_ref": "env:S"},
        {"tenant_id": "t", "client_id": "c"},  # incomplete Entra ID
        {"account_key_ref": "env:K", "container": "bad--name"},
        {"account_key_ref": "env:K", "container": "UPPER"},
        {"account_key_ref": "env:K", "account_name": "has-dash"},
        {"account_key_ref": "plain-text-key"},  # must be a secret reference
    ):
        with pytest.raises(ValidationError):
            config(**bad)


async def test_sas_token_auth_sends_no_authorization_header() -> None:
    fake = FakeAzureBlob(auth="sas")
    adapter = AzureBlobStorageAdapter(
        "az",
        config(sas_token_ref="env:S"),
        LOCAL,
        sas_token=SecretValue("?sv=2023-11-03&ss=b&sig=fake-signature"),
        transport=fake.transport(),
    )
    await adapter.put("x.txt", io.BytesIO(b"hi"), content_type="text/plain")
    buf = io.BytesIO()
    await adapter.get("x.txt", buf)
    assert buf.getvalue() == b"hi"
    assert (await adapter.status()).details["auth"] == "sas"
    await adapter.aclose()


async def test_entra_id_bearer_token_is_fetched_once_and_cached() -> None:
    fake = FakeAzureBlob(auth="bearer")
    token_requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            token_requests.append(form)
            return httpx.Response(200, json={"access_token": "storage-token", "expires_in": 3600})
        return fake.handle(request)

    adapter = AzureBlobStorageAdapter(
        "az",
        config(tenant_id="tenant", client_id="app", client_secret_ref="env:S"),
        LOCAL,
        client_secret=SecretValue("s3cret"),
        transport=httpx.MockTransport(handler),
    )
    await adapter.put("a.txt", io.BytesIO(b"1"))
    await adapter.put("b.txt", io.BytesIO(b"2"))
    assert [a.key for a in await adapter.list()] == ["a.txt", "b.txt"]
    assert len(token_requests) == 1
    assert token_requests[0]["scope"] == quote("https://storage.azure.com/.default", safe="")
    assert token_requests[0]["client_secret"] == "s3cret"
    await adapter.aclose()


async def test_egress_policy_blocks_metadata_endpoints_before_any_request() -> None:
    fake = FakeAzureBlob()
    adapter = AzureBlobStorageAdapter(
        "az",
        config(account_key_ref="env:K", endpoint_url=f"http://169.254.169.254/{ACCOUNT}"),
        EgressPolicy(),
        account_key=SecretValue(base64.b64encode(b"k").decode()),
        transport=fake.transport(),
    )
    with pytest.raises(EgressDenied):
        await adapter.put("x.txt", io.BytesIO(b"x"))
    status = await adapter.status()
    assert status.status == HealthStatus.DOWN
    assert fake.requests == []
    await adapter.aclose()


async def test_auth_failures_are_storage_errors_and_status_down() -> None:
    fake = FakeAzureBlob(auth="bearer")  # the adapter will sign with Shared Key instead
    adapter = AzureBlobStorageAdapter(
        "az",
        config(account_key_ref="env:K"),
        LOCAL,
        account_key=SecretValue(base64.b64encode(b"k").decode()),
        transport=fake.transport(),
    )
    with pytest.raises(StorageError, match="403 AuthenticationFailed"):
        await adapter.get_metadata("x.txt")
    status = await adapter.status()
    assert status.status == HealthStatus.DOWN and "AuthenticationFailed" in (status.message or "")
    await adapter.aclose()


async def test_user_metadata_uses_valid_azure_names() -> None:
    fake = FakeAzureBlob()  # the fake rejects metadata names containing '-'
    adapter = AzureBlobStorageAdapter(
        "az",
        config(account_key_ref="env:K"),
        LOCAL,
        account_key=SecretValue(base64.b64encode(b"k").decode()),
        transport=fake.transport(),
    )
    art = await adapter.put("m.bin", io.BytesIO(b"x"), metadata={"run-id": "7", "Model": "q"})
    assert art.metadata == {"run-id": "7", "Model": "q"}
    stored = fake.blobs["omf/m.bin"].metadata
    assert set(stored) == {"omf_sha256", "omf_version", "omf_user"}
    assert json.loads(base64.b64decode(stored["omf_user"])) == {"Model": "q", "run-id": "7"}
    await adapter.aclose()


@pytest.mark.skipif(not AZURITE_URL, reason="OMF_TEST_AZURITE_URL not set")
async def test_real_account_sas_against_azurite() -> None:
    assert AZURITE_URL
    owner = AzureBlobStorageAdapter(
        "owner",
        config(account_key_ref="env:K", endpoint_url=AZURITE_URL, container="omf-sas-test"),
        LOCAL,
        account_key=SecretValue(AZURITE_KEY),
    )
    await owner._request("PUT", owner._url(), params={"restype": "container"})
    # Account SAS, signed with the account key (same layout as azure-sdk-for-python).
    expiry = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = {
        "sp": "rwdlac",
        "ss": "b",
        "srt": "sco",
        "st": "",
        "se": expiry,
        "sip": "",
        "spr": "https,http",
        "sv": API_VERSION,
        "ses": "",
    }
    to_sign = "devstoreaccount1\n" + "".join(f"{fields[k]}\n" for k in fields)
    sig = shared_key_signature(AZURITE_KEY, to_sign)
    query = {k: v for k, v in fields.items() if v} | {"sig": sig}
    token = "&".join(f"{k}={quote(v, safe='')}" for k, v in query.items())
    adapter = AzureBlobStorageAdapter(
        "sas",
        config(sas_token_ref="env:S", endpoint_url=AZURITE_URL, container="omf-sas-test"),
        LOCAL,
        sas_token=SecretValue(token),
    )
    await adapter.put("sas/x.txt", io.BytesIO(b"via sas"), content_type="text/plain")
    buf = io.BytesIO()
    await adapter.get("sas/x.txt", buf)
    assert buf.getvalue() == b"via sas"
    assert [a.key for a in await adapter.list("sas/")] == ["sas/x.txt"]
    await adapter.delete("sas/x.txt")
    bad = AzureBlobStorageAdapter(
        "bad",
        config(sas_token_ref="env:S", endpoint_url=AZURITE_URL, container="omf-sas-test"),
        LOCAL,
        sas_token=SecretValue(token.replace(quote(sig, safe=""), "AAAA")),
    )
    assert (await bad.status()).status == HealthStatus.DOWN
    await owner._request("DELETE", owner._url(), params={"restype": "container"})
    for a in (owner, adapter, bad):
        await a.aclose()


@pytest.mark.skipif(not AZURITE_URL, reason="OMF_TEST_AZURITE_URL not set")
async def test_azurite_rejects_a_wrong_shared_key() -> None:
    """Guards the meaning of the Azurite runs: they only prove the signing is right
    if Azurite really verifies signatures."""
    assert AZURITE_URL
    wrong = AzureBlobStorageAdapter(
        "wrong",
        config(account_key_ref="env:K", endpoint_url=AZURITE_URL),
        LOCAL,
        account_key=SecretValue(base64.b64encode(b"not-the-key").decode()),
    )
    status = await wrong.status()
    assert status.status == HealthStatus.DOWN and "403" in (status.message or "")
    await wrong.aclose()


def test_listing_with_a_dtd_is_refused() -> None:
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><EnumerationResults/>'
    with pytest.raises(StorageError, match="DTD"):
        _parse_listing(bomb)
    with pytest.raises(StorageError, match="invalid listing"):
        _parse_listing(b"<not-closed>")


def test_missing_secret_registers_storage_as_unavailable(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    storage = [
        {"id": "local", "adapter": "local_fs", "default": True, "config": {"root": "x"}},
        {
            "id": "azure",
            "adapter": "azure_blob",
            "config": {
                "account_name": "acct",
                "container": "models",
                "account_key_ref": "env:OMF_TEST_MISSING_AZURE_KEY",
            },
        },
    ]
    client = make_client(make_config(storage=storage))
    status = {s["storage_provider_id"]: s for s in client.get("/api/v1/storage").json()["data"]}
    assert status["azure"]["status"] == "down"
    assert status["azure"]["adapter_type"] == "azure_blob"
    assert status["local"]["status"] == "up"


@pytest.mark.skipif(not AZURITE_URL, reason="OMF_TEST_AZURITE_URL not set")
async def test_api_upload_and_download_through_azurite(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    assert AZURITE_URL
    owner = AzureBlobStorageAdapter(
        "owner",
        config(account_key_ref="env:K", endpoint_url=AZURITE_URL, container="omf-api-test"),
        LOCAL,
        account_key=SecretValue(AZURITE_KEY),
    )
    await owner._request("PUT", owner._url(), params={"restype": "container"})
    storage = [
        {
            "id": "azure",
            "adapter": "azure_blob",
            "default": True,
            "config": {
                "account_name": "devstoreaccount1",
                "container": "omf-api-test",
                "endpoint_url": AZURITE_URL,
                "account_key_ref": "env:OMF_TEST_AZURITE_ACCOUNT_KEY",
            },
        }
    ]
    client = make_client(
        make_config(storage=storage), env={"OMF_TEST_AZURITE_ACCOUNT_KEY": AZURITE_KEY}
    )
    assert client.get("/api/v1/storage").json()["data"][0]["status"] == "up"
    uploaded = client.post(
        "/api/v1/storage/azure/artifacts",
        data={"key": "experiments/run-1.json"},
        files={"file": ("run.json", b'{"loss": 0.1}', "application/json")},
    )
    assert uploaded.status_code == 201, uploaded.text
    content = client.get(
        "/api/v1/storage/azure/artifact/content", params={"key": "experiments/run-1.json"}
    )
    assert content.content == b'{"loss": 0.1}'
    assert content.headers["x-content-sha256"] == uploaded.json()["data"]["sha256"]
    listed = client.get("/api/v1/storage/azure/artifacts").json()["data"]
    assert [a["key"] for a in listed] == ["experiments/run-1.json"]
    deleted = client.delete(
        "/api/v1/storage/azure/artifact", params={"key": "experiments/run-1.json"}
    )
    assert deleted.status_code == 200
    await owner._request("DELETE", owner._url(), params={"restype": "container"})
    await owner.aclose()
