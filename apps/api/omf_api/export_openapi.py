"""Write the OpenAPI document to ``packages/contracts/openapi.json`` (or a given path).

Used by ``make openapi`` and by CI to detect contract drift (the generated TypeScript
SDK is built from this file).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from omf.bootstrap.config import parse_config
from omf_api.app import create_app


def build_openapi() -> dict[str, object]:
    config = parse_config({"profile": "openapi", "auth": {"mode": "none"}})
    return create_app(config).openapi()


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "packages/contracts/openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(build_openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
