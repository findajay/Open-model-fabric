"""``omf-api`` / ``python -m omf_api``: run the API server with the selected profile."""

from __future__ import annotations

import argparse
import os

import uvicorn

from omf.bootstrap.config import load_config, resolve_config_path
from omf_api.app import configure_logging, create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Open Model Fabric API server")
    parser.add_argument(
        "--config", help="profile YAML (default: $OMF_CONFIG or configs/$OMF_PROFILE.yaml)"
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--log-level", default=os.environ.get("OMF_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    configure_logging(args.log_level.upper())
    config = load_config(args.config or resolve_config_path())
    if args.host:
        config.server.host = args.host
        # Re-validate host-dependent rules (auth.mode=none is localhost-only).
        config = type(config).model_validate(config.model_dump())
    if args.port:
        config.server.port = args.port
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        log_level=args.log_level.lower(),
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
