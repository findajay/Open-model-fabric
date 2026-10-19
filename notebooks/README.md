# Notebooks

## `colab_model_lab.ipynb` — Colab model lab

A standalone notebook for experimenting with open-weight models on Google Colab (CPU or
GPU) and producing benchmark files the Open Model Fabric platform understands. It does
not import the platform code; it only shares file formats with it.

What it does, in order:

1. Detects CPU / CUDA GPU (and reports the absence of a TPU), shows RAM and VRAM.
2. Optionally mounts Google Drive (`USE_DRIVE` form field, Colab only).
3. Selects a model configuration from a form (default `Qwen/Qwen2.5-0.5B-Instruct`,
   CPU-capable; `Qwen2.5-1.5B-Instruct`; `Qwen2.5-7B-Instruct`, GPU only).
4. Downloads weights from the Hugging Face Hub with `snapshot_download`: the requested
   `MODEL_REVISION` (default `main`) is resolved to a commit hash, and only safetensors,
   JSON, tokenizer and license files are fetched. `trust_remote_code` is never enabled.
5. Caches weights on Drive (`MyDrive/open-model-fabric/models`) or locally
   (`/content/omf-models` in Colab, `~/.cache/open-model-fabric/models` elsewhere,
   override with `OMF_MODEL_CACHE`) and writes `omf-manifest.json` with each file's size
   and SHA-256. Cached files are verified against it on later runs.
6. Loads the model with `transformers` (bf16/fp16 on GPU, fp32 on CPU), runs the
   platform's `smoke` dataset v1.0.0 (`greet`, `count`, `define`) through the chat
   template, and measures one cold request plus `WARM_ITERATIONS` warm passes:
   TTFT (via `TextIteratorStreamer`), total latency, output tokens and tokens/second.
7. Writes to the output directory (Drive `MyDrive/open-model-fabric/experiments`, or
   `OMF_NOTEBOOK_OUTPUT_DIR`, default `./omf_lab_outputs`):
   * `experiment_<run_id>.json` — full experiment record (model + revision, license,
     hardware, dtype/quantization, dataset, generation settings incl. seed, samples);
   * `benchmark_<run_id>.csv` / `benchmark_<run_id>.json` — one row per sample with the
     platform's benchmark export columns, `provider_id` = `colab-notebook`;
   * `model-manifest_<run_id>.json` — the checksum manifest.
8. Prints `curl` commands to upload those files to the platform's artifact storage, then
   releases the model's CPU/GPU memory.

### Policy

Colab is an interactive notebook environment. The notebook does not keep the runtime
alive, work around idle timeouts, auto-reconnect, or expose a public endpoint or tunnel.
The platform registers a Colab-hosted inference endpoint only when you attest that your
Colab plan and the terms permit it (`endpoint.authorized` on the `colab_notebook` compute
adapter in `configs/colab-experiment.yaml`); otherwise use Ollama
(`configs/local-ollama.yaml`) or dedicated inference infrastructure.

Open-weight model licenses are model licenses, not open-source software licenses. The
Qwen2.5 0.5B/1.5B/7B Instruct model cards state Apache-2.0; re-check the card for the
revision you pin.

### Uploading results to the platform

Upload the exported files through the API so the platform records size and SHA-256
(the notebook prints these commands with the right file names):

```bash
curl -fsS -X POST "$OMF_URL/api/v1/storage/gdrive/artifacts" \
  -H "Authorization: Bearer $OMF_TOKEN" \
  -F "key=experiments/colab/<run_id>/benchmark_<run_id>.csv" \
  -F "file=@benchmark_<run_id>.csv;type=text/csv"
```

`gdrive` is the Google Drive storage adapter configured in
`configs/colab-experiment.yaml`; use `local` or any other configured storage id instead.

### Smoke mode and tests

`OMF_NOTEBOOK_SMOKE=1` replaces the download with a tiny randomly initialised Qwen2 model
and a word-level tokenizer built locally, then runs the same load / generate / measure /
export code. It needs no network. Its numbers are **not** meaningful benchmarks.

`tests/integration/test_notebook.py` has static checks that always run (valid nbformat
v4, every code cell compiles, no `omf` imports, no `trust_remote_code=True`, no tunnels or
keep-alive tricks, policy text present, dataset and CSV columns match the platform) and an
execution test (marker `notebook`) that runs the notebook in smoke mode with `nbclient`.
The execution test is skipped unless `torch`, `transformers`, `tokenizers`, `nbclient`,
`nbformat` and `ipykernel` are installed:

```bash
uv pip install torch transformers tokenizers nbclient nbformat ipykernel
python -m pytest tests/integration/test_notebook.py -m notebook
```
