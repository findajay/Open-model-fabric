"""Create a blob container on a local Azurite (the azure_blob adapter does not create
containers). Uses the adapter's own Shared Key signing and Azurite's publicly documented
development account.

    uv run python scripts/azurite_container.py models
"""

from __future__ import annotations

import asyncio
import sys

from omf.adapters.storage.azure_blob import AzureBlobConfig, AzureBlobStorageAdapter
from omf.core.ports.secrets import SecretValue
from omf.core.security.egress import EgressPolicy

ENDPOINT = "http://127.0.0.1:10000/devstoreaccount1"
# Azurite's well-known development key (documented by Microsoft; not a secret).
DEV_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="


async def main(container: str) -> None:
    config = AzureBlobConfig(
        account_name="devstoreaccount1",
        container=container,
        endpoint_url=ENDPOINT,
        account_key_ref="env:AZURE_STORAGE_KEY",
    )
    adapter = AzureBlobStorageAdapter(
        "azurite", config, EgressPolicy(allow_loopback=True), account_key=SecretValue(DEV_KEY)
    )
    response = await adapter._request("PUT", adapter._url(), params={"restype": "container"})
    print(f"container {container!r}: HTTP {response.status_code}")
    await adapter.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "models"))
