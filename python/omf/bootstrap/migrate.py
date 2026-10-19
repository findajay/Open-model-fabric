"""Apply metadata-store migrations: ``python -m omf.bootstrap.migrate``.

Uses ``OMF_DATABASE_URL`` if set, otherwise the metadata settings of the active profile
(``OMF_CONFIG`` / ``OMF_PROFILE``). The API also migrates on startup; this command is for
deployments that run migrations as a separate step (e.g. a Kubernetes Job).
"""

from __future__ import annotations

import asyncio
import os

from omf.adapters.metadata.sql import SqlMetadataStore
from omf.bootstrap.config import load_config, resolve_config_path


def database_url() -> str:
    if os.environ.get("OMF_DATABASE_URL"):
        return os.environ["OMF_DATABASE_URL"]
    config = load_config(resolve_config_path())
    if config.metadata.url:
        return config.metadata.url
    ref = str(config.metadata.url_ref)
    if ref.startswith("env:") and os.environ.get(ref[4:]):
        return os.environ[ref[4:]]
    raise SystemExit(f"cannot resolve metadata.url_ref {ref}; set OMF_DATABASE_URL")


async def main() -> None:
    store = SqlMetadataStore(database_url())
    await store.initialize()
    await store.aclose()
    print(f"metadata store migrated ({store.dialect})")


if __name__ == "__main__":
    asyncio.run(main())
