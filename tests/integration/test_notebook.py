"""Tests for notebooks/colab_model_lab.ipynb.

* ``test_notebook_*`` static tests always run: they need only the standard library (and use
  ``nbformat`` for schema validation when it is installed).
* ``test_notebook_executes_in_smoke_mode`` (marker ``notebook``) executes the whole notebook
  with nbclient in smoke mode (``OMF_NOTEBOOK_SMOKE=1``: tiny random local model, no
  downloads). It is skipped unless torch, transformers, tokenizers, nbclient and ipykernel
  are importable.

The notebook must stay independent of the platform, so this module deliberately does not
import ``omf``/``omf_api``; it cross-checks shared formats by parsing platform source.
"""

from __future__ import annotations

import ast
import csv
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = ROOT / "notebooks" / "colab_model_lab.ipynb"
PLATFORM_BENCHMARKS = ROOT / "python" / "omf" / "core" / "services" / "benchmarks.py"

BENCHMARK_COLUMNS = [
    "run_id",
    "run_name",
    "dataset_id",
    "dataset_version",
    "provider_id",
    "model",
    "prompt_id",
    "iteration",
    "phase",
    "ttft_ms",
    "total_ms",
    "output_tokens",
    "token_count_source",
    "tokens_per_second",
    "error_code",
    "error_message",
    "temperature",
    "max_tokens",
    "seed",
    "quantization",
    "model_revision",
    "hardware_fingerprint",
]
SMOKE_PROMPTS = {
    "greet": "Say hello in one short sentence.",
    "count": "Count from one to ten, separated by commas.",
    "define": "Define 'latency' in two sentences.",
}
# Things the notebook must never do on Colab: tunnels/public endpoints, keep-alive or
# auto-reconnect tricks.
FORBIDDEN_CODE_PATTERNS = [
    r"ngrok",
    r"cloudflared",
    r"localtunnel",
    r"\blt\s+--port",
    r"serveo",
    r"localhost\.run",
    r"trycloudflare",
    r"keep[\s_-]?alive",
    r"auto[\s_-]?reconnect",
    r"setInterval",
    r"serve_kernel_port_as",
    r"share\s*=\s*True",
    r"\.launch\(",
    r"uvicorn\.run",
    r"while\s+True",
]


# --------------------------------------------------------------------------- helpers


def _load_notebook() -> dict[str, Any]:
    with NOTEBOOK.open(encoding="utf-8") as fh:
        nb: dict[str, Any] = json.load(fh)
    return nb


def _source(cell: dict[str, Any]) -> str:
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else str(src)


def _code_cells(nb: dict[str, Any]) -> list[str]:
    return [_source(c) for c in nb["cells"] if c["cell_type"] == "code"]


def _markdown(nb: dict[str, Any]) -> str:
    return "\n".join(_source(c) for c in nb["cells"] if c["cell_type"] == "markdown")


def _strip_magics(source: str) -> str:
    return "\n".join(
        "" if line.lstrip().startswith(("!", "%")) else line for line in source.splitlines()
    )


def _parsed_code_cells(nb: dict[str, Any]) -> list[ast.Module]:
    return [
        ast.parse(_strip_magics(src), filename=f"<cell {i}>")
        for i, src in enumerate(_code_cells(nb))
    ]


def _platform_smoke_dataset() -> dict[str, str] | None:
    """Prompts of the platform's built-in ``smoke`` dataset, read from source via ``ast``."""
    if not PLATFORM_BENCHMARKS.exists():
        return None
    tree = ast.parse(PLATFORM_BENCHMARKS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call) and getattr(node.func, "id", None) == "BenchmarkDataset"
        ):
            continue
        kwargs = {k.arg: k.value for k in node.keywords}
        if isinstance(kwargs.get("id"), ast.Constant) and kwargs["id"].value == "smoke":
            prompts = {}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == (
                    "BenchmarkPrompt"
                ):
                    kw = {k.arg: ast.literal_eval(k.value) for k in sub.keywords}
                    prompts[kw["id"]] = kw["text"]
            version = kwargs.get("version")
            assert isinstance(version, ast.Constant)
            prompts["__version__"] = version.value
            return prompts
    return None


def _platform_sample_columns() -> list[str] | None:
    if not PLATFORM_BENCHMARKS.exists():
        return None
    tree = ast.parse(PLATFORM_BENCHMARKS.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SAMPLE_COLUMNS" for t in node.targets
        ):
            columns: list[str] = ast.literal_eval(node.value)
            return columns
    return None


# --------------------------------------------------------------------------- static


def test_notebook_is_valid_nbformat_v4() -> None:
    nb = _load_notebook()
    assert nb["nbformat"] == 4
    assert nb["metadata"]["kernelspec"]["name"] == "python3"
    assert {c["cell_type"] for c in nb["cells"]} <= {"code", "markdown", "raw"}
    if importlib.util.find_spec("nbformat") is not None:
        import nbformat

        nbformat.validate(nbformat.reads(NOTEBOOK.read_text(encoding="utf-8"), as_version=4))
    # Committed without outputs, so no stale results or local paths leak into the repo.
    for cell in nb["cells"]:
        if cell["cell_type"] == "code":
            assert cell.get("outputs") == []
            assert cell.get("execution_count") is None


def test_notebook_code_cells_compile() -> None:
    nb = _load_notebook()
    sources = _code_cells(nb)
    assert len(sources) >= 10
    for i, src in enumerate(sources):
        compile(_strip_magics(src), f"<notebook cell {i}>", "exec")


def test_notebook_is_independent_of_the_platform() -> None:
    for tree in _parsed_code_cells(_load_notebook()):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not roots & {"omf", "omf_api", "tests"}, ast.unparse(node)


def test_notebook_has_no_unsafe_loading_or_runtime_tricks() -> None:
    nb = _load_notebook()
    raw = NOTEBOOK.read_text(encoding="utf-8")
    assert not re.search(r"trust_remote_code\s*=\s*True", raw)
    code = "\n".join(_code_cells(nb))
    for pattern in FORBIDDEN_CODE_PATTERNS:
        assert not re.search(pattern, code, flags=re.IGNORECASE), pattern
    # Trusted, pinnable, safetensors-only download.
    assert "snapshot_download(" in code
    assert "revision=" in code and 'MODEL_REVISION = "main"' in code
    assert "allow_patterns=ALLOW_PATTERNS" in code
    assert "use_safetensors" in code and '"use_safetensors": True' in code
    allow = re.search(r"ALLOW_PATTERNS = \[(.*?)\]", code, flags=re.S)
    assert allow is not None
    assert ".bin" not in allow.group(1) and ".pt" not in allow.group(1)
    assert "sha256" in code


def test_notebook_imports_colab_only_lazily() -> None:
    nb = _load_notebook()
    found = False
    for tree in _parsed_code_cells(nb):
        for node in tree.body:  # top-level statements must not import google.colab
            if isinstance(node, ast.ImportFrom | ast.Import):
                text = ast.unparse(node)
                assert "google.colab" not in text, text
        found = found or "from google.colab import drive" in ast.unparse(tree)
    assert found
    code = "\n".join(_code_cells(nb))
    assert "USE_DRIVE and IN_COLAB" in code
    assert "/content/drive" in code and '"MyDrive" / "open-model-fabric"' in code


def test_notebook_states_policy() -> None:
    nb = _load_notebook()
    text = " ".join(_markdown(nb).split())
    for phrase in [
        "Colab is an interactive notebook environment.",
        "keep the runtime alive",
        "avoid idle timeouts",
        "auto-reconnect",
        "public web endpoint",
        "`endpoint.authorized`",
        "`colab_notebook`",
        "configs/colab-experiment.yaml",
        "Ollama",
        "dedicated inference infrastructure",
        "Google Drive storage adapter",
        "Open-weight model licenses are *not* the same as open-source software licenses",
    ]:
        assert phrase in text, phrase
    # The policy appears near the top: within the first three cells.
    top = " ".join(" ".join(_source(c) for c in nb["cells"][:3]).split())
    assert "Colab is an interactive notebook environment." in top


def test_notebook_matches_platform_formats() -> None:
    code = "\n".join(_code_cells(_load_notebook()))
    assert 'MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"' in code
    assert 'PROVIDER_ID = "colab-notebook"' in code
    assert 'DATASET_ID = "smoke"' in code and 'DATASET_VERSION = "1.0.0"' in code
    for pid, text in SMOKE_PROMPTS.items():
        assert f'{{"id": "{pid}", "text": "{text}"}}' in code, pid
    columns = re.search(r"BENCHMARK_COLUMNS = \[(.*?)\]", code, flags=re.S)
    assert columns is not None
    assert ast.literal_eval(f"[{columns.group(1)}]") == BENCHMARK_COLUMNS

    platform_dataset = _platform_smoke_dataset()
    if platform_dataset is not None:
        assert platform_dataset.pop("__version__") == "1.0.0"
        assert platform_dataset == SMOKE_PROMPTS
    platform_columns = _platform_sample_columns()
    if platform_columns is not None:
        # Same names, same order; the platform may add columns the notebook cannot know
        # (e.g. comparability_key).
        assert [c for c in platform_columns if c in BENCHMARK_COLUMNS] == BENCHMARK_COLUMNS


# --------------------------------------------------------------------------- execution

_EXEC_DEPS = ("torch", "transformers", "tokenizers", "nbclient", "nbformat", "ipykernel")
_missing = [m for m in _EXEC_DEPS if importlib.util.find_spec(m) is None]


@pytest.mark.notebook
@pytest.mark.skipif(bool(_missing), reason=f"notebook execution needs {_missing}")
def test_notebook_executes_in_smoke_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import nbformat
    from nbclient import NotebookClient

    out = tmp_path / "outputs"
    monkeypatch.setenv("OMF_NOTEBOOK_SMOKE", "1")
    monkeypatch.setenv("OMF_NOTEBOOK_OUTPUT_DIR", str(out))
    # Prove smoke mode needs no network or model hub.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")  # deterministic CPU path

    nb = nbformat.read(NOTEBOOK, as_version=4)
    client = NotebookClient(
        nb,
        kernel_name="python3",
        timeout=600,
        resources={"metadata": {"path": str(tmp_path)}},
    )
    client.execute()

    printed = "".join(
        o.get("text", "") for c in nb.cells if c.cell_type == "code" for o in c.outputs
    )
    assert "SMOKE MODE" in printed and "NOT meaningful benchmarks" in printed

    csv_files = list(out.glob("benchmark_*.csv"))
    bench_json_files = list(out.glob("benchmark_*.json"))
    experiment_files = list(out.glob("experiment_*.json"))
    manifest_files = list(out.glob("model-manifest_*.json"))
    assert len(csv_files) == len(bench_json_files) == len(experiment_files) == 1
    assert len(manifest_files) == 1

    with csv_files[0].open(newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == BENCHMARK_COLUMNS
        rows = list(reader)
    cold = [r for r in rows if r["phase"] == "cold"]
    warm = [r for r in rows if r["phase"] == "warm"]
    assert len(cold) == 1 and cold[0]["prompt_id"] == "greet"
    assert len(warm) >= len(SMOKE_PROMPTS)
    assert {r["prompt_id"] for r in warm} == set(SMOKE_PROMPTS)
    assert len(rows) == len(cold) + len(warm)
    for row in rows:
        assert row["provider_id"] == "colab-notebook"
        assert (row["dataset_id"], row["dataset_version"]) == ("smoke", "1.0.0")
        assert row["seed"] == "42" and row["quantization"] == "none"
        assert re.fullmatch(r"[0-9a-f]{16}", row["hardware_fingerprint"])
        if not row["error_code"]:
            assert float(row["tokens_per_second"]) > 0
            assert int(row["output_tokens"]) > 0
            assert 0 < float(row["ttft_ms"]) <= float(row["total_ms"])
    assert all(not r["error_code"] for r in rows), rows

    bench = json.loads(bench_json_files[0].read_text())
    assert bench["smoke"] is True and bench["columns"] == BENCHMARK_COLUMNS
    assert len(bench["rows"]) == len(rows)

    experiment = json.loads(experiment_files[0].read_text())
    assert experiment["smoke"] is True
    assert experiment["dataset"] == {
        "id": "smoke",
        "version": "1.0.0",
        "prompts": [{"id": k, "text": v} for k, v in SMOKE_PROMPTS.items()],
    }
    assert experiment["generation"]["seed"] == 42
    assert experiment["model"]["revision"] and experiment["model"]["quantization"] == "none"
    assert experiment["hardware"]["fingerprint"] == rows[0]["hardware_fingerprint"]
    assert [s["warm_up_status"].split(":")[0] for s in experiment["samples"]] == [
        r["phase"] for r in rows
    ]
    assert any("NOT a meaningful benchmark" in n for n in experiment["notes"])

    manifest = json.loads(manifest_files[0].read_text())
    names = {f["path"] for f in manifest["files"]}
    assert "model.safetensors" in names and "config.json" in names
    assert all(re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) for f in manifest["files"])
    assert not any(n.endswith((".bin", ".pt", ".pkl")) for n in names)
