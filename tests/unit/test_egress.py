from __future__ import annotations

import pytest

from omf.core.domain.errors import EgressDenied
from omf.core.security.egress import EgressPolicy

STRICT = EgressPolicy()
DEV = EgressPolicy(allow_loopback=True, allow_private_networks=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://[fd00:ec2::254]/",
        "http://[::ffff:169.254.169.254]/",
        "http://169.254.10.10/",  # link-local
        "http://0.0.0.0:8080/",
        "file:///etc/passwd",
        "gopher://localhost/",
        "http://user:pass@example.com/",
    ],
)
def test_always_blocked_even_in_dev(url: str) -> None:
    with pytest.raises(EgressDenied):
        DEV.check_url_syntax(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",
        "http://[::1]:8000",
        "http://10.0.0.5/v1",
        "http://192.168.1.20/v1",
        "http://100.64.0.1/",
    ],
)
def test_strict_policy_blocks_loopback_and_private(url: str) -> None:
    with pytest.raises(EgressDenied):
        STRICT.check_url_syntax(url)


def test_dev_policy_allows_loopback_and_private() -> None:
    DEV.check_url_syntax("http://127.0.0.1:11434")
    DEV.check_url_syntax("http://[::1]:11434")  # IPv6 loopback (also in Python's "reserved" ::/8)
    DEV.check_url_syntax("http://10.0.0.5:8000/v1")


def test_allowlist() -> None:
    policy = EgressPolicy(allowed_hosts=["api.example.com", "*.inference.example.org"])
    policy.check_url_syntax("https://api.example.com/v1")
    policy.check_url_syntax("https://gpu1.inference.example.org/v1")
    with pytest.raises(EgressDenied):
        policy.check_url_syntax("https://evil.example.net/v1")


async def test_dns_resolution_is_checked() -> None:
    # "localhost" resolves to loopback, which the strict policy forbids.
    with pytest.raises(EgressDenied):
        await STRICT.check_url("http://localhost:11434")
    await DEV.check_url("http://localhost:11434")


async def test_unresolvable_host_is_retryable_not_policy() -> None:
    with pytest.raises(EgressDenied) as info:
        await DEV.check_url("http://does-not-exist.invalid/")
    assert info.value.retryable
