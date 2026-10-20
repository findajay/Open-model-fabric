"""Shared boto3 plumbing for AWS compute adapters (EC2, ECS).

boto3 is synchronous; calls run in a worker thread so the event loop never blocks. AWS
errors become :class:`ComputeError` with the AWS error code in ``details``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from omf.core.domain.errors import ComputeError, ConfigurationError
from omf.core.ports.secrets import SecretValue

RETRYABLE_CODES = frozenset(
    {
        "RequestLimitExceeded",
        "ThrottlingException",
        "InsufficientInstanceCapacity",
        "ServiceUnavailable",
        "ServerException",
    }
)


def boto_client(
    service: str,
    *,
    region: str,
    endpoint_url: str | None,
    access_key: SecretValue | None,
    secret_key: SecretValue | None,
    adapter: str,
) -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - optional extra
        raise ConfigurationError(
            f"the {adapter} compute adapter needs boto3 (pip install 'open-model-fabric[s3]')"
        ) from exc
    kwargs: dict[str, Any] = {"region_name": region, "endpoint_url": endpoint_url}
    if access_key and secret_key:
        kwargs.update(
            aws_access_key_id=access_key.reveal(), aws_secret_access_key=secret_key.reveal()
        )
    return boto3.client(service, **kwargs)


async def call(client: Any, label: str, name: str, **kwargs: Any) -> dict[str, Any]:
    """Run ``client.<name>(**kwargs)`` in a thread, mapping failures to ComputeError."""

    def run() -> dict[str, Any]:
        try:
            return dict(getattr(client, name)(**kwargs))
        except Exception as exc:
            error = getattr(exc, "response", {}).get("Error", {})
            code = error.get("Code", type(exc).__name__)
            message = error.get("Message", str(exc))
            raise ComputeError(
                f"{label} {name} failed: {code}: {str(message)[:300]}",
                details={"aws_error": code},
                retryable=code in RETRYABLE_CODES,
            ) from exc

    return await asyncio.to_thread(run)
