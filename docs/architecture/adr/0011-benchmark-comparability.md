# ADR-0011: Benchmarks record environment and flag incomparable results

**Status:** Accepted

## Context
Latency and throughput depend on hardware, quantization, prompt set, generation
settings, warm-up, and how tokens are counted. A dashboard that ranks numbers from
different conditions side by side misleads.

## Decision
- **Versioned datasets** (`smoke@1.0.0`, `short-generation@1.0.0`). Changing a prompt
  requires a version bump.
- **Protocol per target:** sequential requests with the provider set explicitly (no
  routing or fallback), streamed to measure TTFT. The first request is recorded as
  `cold`, then `warmup_runs` requests are discarded, then
  `iterations × prompts` are recorded as `warm`.
- **Each summary records `TargetEnvironment`:** adapter, model, revision, quantization,
  compute environment, hardware, and a hardware fingerprint. It also gets a
  `comparability_key`, a hash of the dataset version, parameters, quantization and
  hardware.
- **The run is flagged `comparable: false`, with notes, when:**
  - hardware differs or is unknown across targets;
  - quantizations differ;
  - token counts mix provider-reported and estimated sources.

  A caveat is always noted: "cold" may not be a true cold start when the server already
  had the model loaded.
- **Exports:** CSV has one row per sample, including the environment columns. JSON
  (`omf.benchmark.v1`) holds the full run. Either can be stored in any artifact store.

## Consequences
- The UI shows the comparability notes instead of hiding them.
- Benchmark requests are normal inference requests, so they also appear in recent
  requests and the dashboard metrics.

## Alternatives considered
- External benchmarking tools only: they lose the link to OMF's provider and model
  records.
