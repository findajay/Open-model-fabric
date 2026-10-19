"""Hexagonal-architecture guard rails, enforced by static import analysis.

* ``omf.core`` must not import adapters, the composition root, the web layer, HTTP
  clients, database drivers or any provider/cloud SDK.
* Adapters must not import the web layer or the composition root.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "python" / "omf" / "core"
ADAPTERS = ROOT / "python" / "omf" / "adapters"

FORBIDDEN_IN_CORE = (
    "omf.adapters",
    "omf.bootstrap",
    "omf_api",
    "fastapi",
    "starlette",
    "httpx",
    "requests",
    "sqlalchemy",
    "alembic",
    "psycopg",
    "boto3",
    "botocore",
    "google",
    "googleapiclient",
    "opentelemetry",
    "openai",
    "ollama",
    "torch",
    "transformers",
    "uvicorn",
    "yaml",
)
FORBIDDEN_IN_ADAPTERS = ("omf.bootstrap", "omf_api", "fastapi")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _violations(root: Path, forbidden: tuple[str, ...]) -> list[str]:
    found = []
    for path in root.rglob("*.py"):
        for name in _imports(path):
            if any(name == f or name.startswith(f + ".") for f in forbidden):
                found.append(f"{path.relative_to(ROOT)} imports {name}")
    return found


def test_core_has_no_infrastructure_imports() -> None:
    assert _violations(CORE, FORBIDDEN_IN_CORE) == []


def test_adapters_do_not_depend_on_web_or_composition_root() -> None:
    assert _violations(ADAPTERS, FORBIDDEN_IN_ADAPTERS) == []


def test_core_python_files_exist() -> None:
    # Guard against the checks above passing vacuously after a refactor.
    assert len(list(CORE.rglob("*.py"))) > 10
    assert len(list(ADAPTERS.rglob("*.py"))) > 10
