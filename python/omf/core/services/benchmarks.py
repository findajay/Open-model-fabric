"""Reproducible benchmark runs.

Measurement protocol (per target, sequential, explicit provider — never routed or
fallen back):

1. **cold** — the first request of the run. It may include model load time. Note: a
   server that already had the model loaded will report a "cold" number that is really
   warm; the run records this caveat instead of hiding it.
2. ``warmup_runs`` further requests whose results are discarded.
3. ``iterations`` x every prompt of the dataset — the **warm** samples.

Comparability: each target gets a ``comparability_key`` derived from the dataset version,
generation settings, quantization and hardware fingerprint. If targets in one run differ
in anything other than the model itself, or token counts come from different sources
(provider-reported vs estimated), the run is flagged ``comparable = false`` with notes, so
the UI never presents incomparable numbers as equivalent.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import io
import json
import logging
import statistics
import time
from typing import Any

from pydantic import BaseModel

from omf.core.domain.errors import BenchmarkNotFound, OMFError, ValidationFailed
from omf.core.domain.events import StreamEventType
from omf.core.domain.models import (
    BenchmarkRun,
    BenchmarkSample,
    BenchmarkStatus,
    BenchmarkSummary,
    BenchmarkTarget,
    ChatMessage,
    ComputeResources,
    GenerationParams,
    Role,
    TargetEnvironment,
    utcnow,
)
from omf.core.ports.metadata import BenchmarkRepository
from omf.core.services.compute import ComputeRegistry
from omf.core.services.inference import ChatCommand, InferenceService
from omf.core.services.model_registry import ModelRegistryService
from omf.core.services.provider_registry import ProviderRegistry

log = logging.getLogger(__name__)


class BenchmarkPrompt(BaseModel):
    id: str
    text: str


class BenchmarkDataset(BaseModel):
    id: str
    version: str
    description: str
    prompts: list[BenchmarkPrompt]


# Versioned, built-in datasets. Changing prompts requires bumping the version so stored
# results stay comparable only with results from the same prompt set.
DATASETS: dict[str, BenchmarkDataset] = {
    d.id: d
    for d in [
        BenchmarkDataset(
            id="smoke",
            version="1.0.0",
            description="Three short prompts for quick latency/throughput checks.",
            prompts=[
                BenchmarkPrompt(id="greet", text="Say hello in one short sentence."),
                BenchmarkPrompt(id="count", text="Count from one to ten, separated by commas."),
                BenchmarkPrompt(id="define", text="Define 'latency' in two sentences."),
            ],
        ),
        BenchmarkDataset(
            id="short-generation",
            version="1.0.0",
            description="Prompts that elicit ~100-200 token answers for throughput measurement.",
            prompts=[
                BenchmarkPrompt(
                    id="explain-http", text="Explain how an HTTP request works, step by step."
                ),
                BenchmarkPrompt(
                    id="haiku-set", text="Write three haiku about distributed systems."
                ),
                BenchmarkPrompt(
                    id="python-fn",
                    text=(
                        "Write a Python function that checks whether a string is a "
                        "palindrome, with a docstring."
                    ),
                ),
                BenchmarkPrompt(
                    id="summarize",
                    text=(
                        "Summarize the benefits and drawbacks of microservices "
                        "in five bullet points."
                    ),
                ),
            ],
        ),
    ]
}


class BenchmarkRequest(BaseModel):
    name: str
    dataset_id: str = "smoke"
    targets: list[BenchmarkTarget]
    params: GenerationParams = GenerationParams(temperature=0.0, max_tokens=128, seed=42)
    warmup_runs: int = 0
    iterations: int = 2


class BenchmarkService:
    def __init__(
        self,
        repository: BenchmarkRepository,
        inference: InferenceService,
        providers: ProviderRegistry,
        models: ModelRegistryService,
        compute: ComputeRegistry,
    ) -> None:
        self._repo = repository
        self._inference = inference
        self._providers = providers
        self._models = models
        self._compute = compute
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @staticmethod
    def datasets() -> list[BenchmarkDataset]:
        return list(DATASETS.values())

    async def submit(self, req: BenchmarkRequest, *, wait: bool = False) -> BenchmarkRun:
        dataset = DATASETS.get(req.dataset_id)
        if dataset is None:
            raise ValidationFailed(
                f"unknown dataset {req.dataset_id!r}", details={"available": sorted(DATASETS)}
            )
        if not req.targets:
            raise ValidationFailed("at least one target is required")
        for t in req.targets:
            self._providers.get(t.provider_id)  # ProviderNotFound early
        run = BenchmarkRun(
            name=req.name,
            dataset_id=dataset.id,
            dataset_version=dataset.version,
            targets=req.targets,
            params=req.params,
            warmup_runs=req.warmup_runs,
            iterations=req.iterations,
        )
        await self._repo.save_benchmark(run)
        task = asyncio.create_task(self._execute(run, dataset), name=f"benchmark-{run.id}")
        self._tasks[run.id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run.id, None))
        if wait:
            await task
            return await self.get(run.id)
        return run

    async def get(self, run_id: str) -> BenchmarkRun:
        run = await self._repo.get_benchmark(run_id)
        if run is None:
            raise BenchmarkNotFound(f"benchmark {run_id!r} not found")
        return run

    async def list(self, limit: int = 50) -> list[BenchmarkRun]:
        return await self._repo.list_benchmarks(limit=limit)

    async def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return True

    async def shutdown(self) -> None:
        for run_id in list(self._tasks):
            await self.cancel(run_id)

    # ------------------------------------------------------------------ execution

    async def _execute(self, run: BenchmarkRun, dataset: BenchmarkDataset) -> None:
        run.status = BenchmarkStatus.RUNNING
        run.started_at = utcnow()
        await self._repo.save_benchmark(run)
        try:
            environments: dict[tuple[str, str], TargetEnvironment] = {}
            for target in run.targets:
                environments[(target.provider_id, target.model)] = await self._environment(target)
                first = True
                for _ in range(1 + run.warmup_runs):
                    sample = await self._measure(run, target, dataset.prompts[0], 0, "cold")
                    if first:
                        run.samples.append(sample)
                        first = False
                for iteration in range(run.iterations):
                    for prompt in dataset.prompts:
                        run.samples.append(
                            await self._measure(run, target, prompt, iteration, "warm")
                        )
                await self._repo.save_benchmark(run)  # progress checkpoint
            run.summaries = [
                _summarize(run, t, environments[(t.provider_id, t.model)]) for t in run.targets
            ]
            run.comparable, run.comparability_notes = _comparability(run)
            run.status = BenchmarkStatus.COMPLETED
        except asyncio.CancelledError:
            run.status = BenchmarkStatus.CANCELLED
            run.error = "cancelled"
            raise
        except Exception as exc:
            log.exception("benchmark %s failed", run.id)
            run.status = BenchmarkStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            run.completed_at = utcnow()
            await asyncio.shield(self._repo.save_benchmark(run))

    async def _measure(
        self,
        run: BenchmarkRun,
        target: BenchmarkTarget,
        prompt: BenchmarkPrompt,
        iteration: int,
        phase: str,
    ) -> BenchmarkSample:
        cmd = ChatCommand(
            messages=[ChatMessage(role=Role.USER, content=prompt.text)],
            provider_id=target.provider_id,
            model=target.model,
            params=run.params,
            record=True,
        )
        started = time.perf_counter()
        sample: dict[str, Any] = {
            "provider_id": target.provider_id,
            "model": target.model,
            "prompt_id": prompt.id,
            "iteration": iteration,
            "phase": phase,
        }
        try:
            async for event in self._inference.stream(cmd):
                if event.event == StreamEventType.COMPLETED:
                    data = event.data
                    timings = data.timings  # type: ignore[union-attr]
                    usage = data.usage  # type: ignore[union-attr]
                    sample.update(
                        ttft_ms=timings.ttft_ms,
                        total_ms=timings.total_ms,
                        output_tokens=usage.completion_tokens if usage else None,
                        token_count_source=usage.source if usage else None,
                        tokens_per_second=timings.tokens_per_second,
                    )
                elif event.event == StreamEventType.FAILED:
                    err = event.data.error  # type: ignore[union-attr]
                    sample.update(
                        error_code=err.code,
                        error_message=err.message,
                        total_ms=round((time.perf_counter() - started) * 1000, 2),
                    )
        except OMFError as exc:
            sample.update(error_code=exc.code, error_message=exc.message)
        return BenchmarkSample.model_validate(sample)

    async def _environment(self, target: BenchmarkTarget) -> TargetEnvironment:
        definition = self._providers.get(target.provider_id)
        quantization = None
        revision = None
        try:
            deployment = await self._models.resolve(target.provider_id, target.model)
            quantization = deployment.quantization
            if deployment.model_id:
                with contextlib.suppress(OMFError):
                    revision = (await self._models.definition(deployment.model_id)).revision
        except OMFError:
            pass
        hardware: ComputeResources | None = None
        if definition.compute_environment_id:
            with contextlib.suppress(OMFError):
                hardware = (
                    await self._compute.describe(definition.compute_environment_id)
                ).resources
        return TargetEnvironment(
            provider_id=target.provider_id,
            adapter_type=definition.adapter_type,
            model=target.model,
            model_revision=revision,
            quantization=quantization,
            compute_environment_id=definition.compute_environment_id,
            hardware=hardware,
            hardware_fingerprint=_fingerprint(hardware),
        )


def _fingerprint(hw: ComputeResources | None) -> str:
    if hw is None:
        return "unknown"
    gpus = ",".join(sorted(g.name for g in hw.gpus)) or "no-gpu"
    raw = f"{hw.cpu_model}|{hw.cpu_count}|{hw.architecture}|{hw.accelerator}|{gpus}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 2)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 2) if values else None


def _summarize(
    run: BenchmarkRun, target: BenchmarkTarget, env: TargetEnvironment
) -> BenchmarkSummary:
    samples = [
        s for s in run.samples if s.provider_id == target.provider_id and s.model == target.model
    ]
    errors = [s for s in samples if s.error_code]
    cold = next((s for s in samples if s.phase == "cold" and not s.error_code), None)
    warm = [s for s in samples if s.phase == "warm" and not s.error_code]
    key_src = json.dumps(
        {
            "dataset": f"{run.dataset_id}@{run.dataset_version}",
            "params": run.params.model_dump(mode="json"),
            "quantization": env.quantization,
            "hardware": env.hardware_fingerprint,
        },
        sort_keys=True,
    )
    return BenchmarkSummary(
        provider_id=target.provider_id,
        model=target.model,
        environment=env,
        comparability_key=hashlib.sha256(key_src.encode()).hexdigest()[:16],
        samples=len(samples),
        errors=len(errors),
        error_rate=round(len(errors) / len(samples), 4) if samples else 0.0,
        cold_total_ms=cold.total_ms if cold else None,
        cold_ttft_ms=cold.ttft_ms if cold else None,
        warm_p50_total_ms=_percentile([s.total_ms for s in warm if s.total_ms is not None], 0.5),
        warm_p95_total_ms=_percentile([s.total_ms for s in warm if s.total_ms is not None], 0.95),
        warm_mean_ttft_ms=_mean([s.ttft_ms for s in warm if s.ttft_ms is not None]),
        warm_mean_tokens_per_second=_mean(
            [s.tokens_per_second for s in warm if s.tokens_per_second is not None]
        ),
    )


def _comparability(run: BenchmarkRun) -> tuple[bool, list[str]]:
    notes: list[str] = [
        "'cold' is the first request of this run; if the server already had the model "
        "loaded it is not a true cold start.",
    ]
    comparable = True
    hw = {s.environment.hardware_fingerprint for s in run.summaries}
    if len(hw) > 1 or ("unknown" in hw and len(run.summaries) > 1):
        comparable = False
        notes.append(
            "Targets ran on different or unknown hardware; latency and throughput "
            "reflect infrastructure as much as the model."
        )
    quants = {s.environment.quantization for s in run.summaries}
    if len(quants) > 1:
        comparable = False
        notes.append(f"Targets use different quantizations: {sorted(str(q) for q in quants)}.")
    sources = {s.token_count_source for s in run.samples if s.token_count_source}
    if len(sources) > 1:
        comparable = False
        notes.append(
            "Some token counts are provider-reported and others estimated; "
            "tokens/second is not comparable across them."
        )
    if "estimated" in sources:
        notes.append("Estimated token counts use ~4 characters/token and are approximate.")
    return comparable, notes


# ---------------------------------------------------------------------- export

SAMPLE_COLUMNS = [
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
    "comparability_key",
]


def export_csv(run: BenchmarkRun) -> str:
    summaries = {(s.provider_id, s.model): s for s in run.summaries}
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SAMPLE_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for s in run.samples:
        summary = summaries.get((s.provider_id, s.model))
        env = summary.environment if summary else None
        writer.writerow(
            {
                **s.model_dump(),
                "run_id": run.id,
                "run_name": run.name,
                "dataset_id": run.dataset_id,
                "dataset_version": run.dataset_version,
                "temperature": run.params.temperature,
                "max_tokens": run.params.max_tokens,
                "seed": run.params.seed,
                "quantization": env.quantization if env else None,
                "model_revision": env.model_revision if env else None,
                "hardware_fingerprint": env.hardware_fingerprint if env else None,
                "comparability_key": summary.comparability_key if summary else None,
            }
        )
    return buf.getvalue()


def export_json(run: BenchmarkRun) -> str:
    return json.dumps({"format": "omf.benchmark.v1", "run": run.model_dump(mode="json")}, indent=2)
