import type { BenchmarkRun } from "@omf/sdk";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Download, Play } from "lucide-react";
import { useState } from "react";

import { BarChart } from "@/components/BarChart";
import { ErrorAlert } from "@/components/ErrorAlert";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, NativeSelect } from "@/components/ui/input";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api } from "@/lib/api";
import { supports } from "@/lib/capabilities";
import { ms, num, pct, time } from "@/lib/format";

async function download(run: BenchmarkRun, format: "csv" | "json") {
  const blob = await api.exportBenchmark(run.id, format);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${run.id}.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}

function RunResults({ run }: { run: BenchmarkRun }) {
  const summaries = run.summaries ?? [];
  return (
    <Card className="mt-4" data-testid="benchmark-results">
      <CardHeader className="flex-row items-start justify-between gap-2">
        <div>
          <CardTitle className="flex items-center gap-2">
            {run.name} <StatusBadge status={run.status ?? "pending"} />
          </CardTitle>
          <CardDescription>
            dataset {run.dataset_id}@{run.dataset_version} · {run.iterations} iteration(s) · warm-up{" "}
            {run.warmup_runs} · temperature {run.params.temperature ?? "default"} · max tokens{" "}
            {run.params.max_tokens ?? "default"} · seed {run.params.seed ?? "—"}
          </CardDescription>
        </div>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => void download(run, "csv")}
            disabled={run.status !== "completed"}
          >
            <Download /> CSV
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => void download(run, "json")}
            disabled={run.status !== "completed"}
          >
            <Download /> JSON
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {run.error && <ErrorAlert error={new Error(run.error)} className="mb-3" />}
        {run.status === "completed" && (
          <div
            className={`mb-3 flex gap-2 rounded-md border p-3 text-xs ${
              run.comparable ? "" : "border-status-warn/50 bg-status-warn/10"
            }`}
            role="note"
          >
            <AlertTriangle className="size-4 shrink-0 text-status-warn" aria-hidden />
            <div>
              <div className="font-medium">
                {run.comparable
                  ? "Targets are comparable on the recorded dimensions."
                  : "Results are NOT directly comparable."}
              </div>
              <ul className="mt-1 list-disc pl-4">
                {(run.comparability_notes ?? []).map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            </div>
          </div>
        )}
        {summaries.length > 0 && (
          <div className="mb-4 grid gap-6 md:grid-cols-2">
            <div>
              <div className="mb-2 text-xs font-medium">Warm p50 total latency</div>
              <BarChart
                ariaLabel="Warm median latency per target"
                format={(v) => ms(v)}
                data={summaries.map((s) => ({
                  label: `${s.provider_id}/${s.model}`,
                  value: s.warm_p50_total_ms,
                  detail: [`p95 ${ms(s.warm_p95_total_ms)}`, `cold ${ms(s.cold_total_ms)}`],
                }))}
              />
            </div>
            <div>
              <div className="mb-2 text-xs font-medium">Warm mean throughput (tokens/s)</div>
              <BarChart
                ariaLabel="Warm throughput per target"
                format={(v) => num(v)}
                data={summaries.map((s) => ({
                  label: `${s.provider_id}/${s.model}`,
                  value: s.warm_mean_tokens_per_second,
                  detail: [
                    `hardware ${s.environment.hardware_fingerprint}`,
                    `quantization ${s.environment.quantization ?? "unknown"}`,
                  ],
                }))}
              />
            </div>
          </div>
        )}
        <Table>
          <THead>
            <TR>
              <TH>Target</TH>
              <TH>Quantization</TH>
              <TH>Hardware</TH>
              <TH className="text-right">Cold total</TH>
              <TH className="text-right">Cold TTFT</TH>
              <TH className="text-right">Warm p50</TH>
              <TH className="text-right">Warm p95</TH>
              <TH className="text-right">Warm TTFT</TH>
              <TH className="text-right">tok/s</TH>
              <TH className="text-right">Errors</TH>
            </TR>
          </THead>
          <TBody>
            {summaries.map((s) => (
              <TR key={`${s.provider_id}/${s.model}`}>
                <TD className="font-mono text-xs">
                  {s.provider_id}/{s.model}
                </TD>
                <TD className="text-xs">{s.environment.quantization ?? "unknown"}</TD>
                <TD className="font-mono text-[11px]" title={s.comparability_key}>
                  {s.environment.hardware_fingerprint}
                </TD>
                <TD className="text-right tabular-nums">{ms(s.cold_total_ms)}</TD>
                <TD className="text-right tabular-nums">{ms(s.cold_ttft_ms)}</TD>
                <TD className="text-right tabular-nums">{ms(s.warm_p50_total_ms)}</TD>
                <TD className="text-right tabular-nums">{ms(s.warm_p95_total_ms)}</TD>
                <TD className="text-right tabular-nums">{ms(s.warm_mean_ttft_ms)}</TD>
                <TD className="text-right tabular-nums">{num(s.warm_mean_tokens_per_second)}</TD>
                <TD className="text-right tabular-nums">{pct(s.error_rate)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
        {run.status !== "completed" && (
          <p className="mt-2 text-xs text-muted-foreground">
            {(run.samples ?? []).length} samples so far…
          </p>
        )}
      </CardContent>
    </Card>
  );
}

export function BenchmarksPage() {
  const queryClient = useQueryClient();
  const datasets = useQuery({ queryKey: ["datasets"], queryFn: api.datasets });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: () => api.deployments() });
  const runs = useQuery({ queryKey: ["benchmarks"], queryFn: api.benchmarks });
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const current = useQuery({
    queryKey: ["benchmark", selectedRun],
    queryFn: () => api.benchmark(selectedRun!),
    enabled: !!selectedRun,
    refetchInterval: (q) =>
      q.state.data && ["pending", "running"].includes(q.state.data.status ?? "") ? 1000 : false,
  });

  const [name, setName] = useState("Benchmark");
  const [datasetId, setDatasetId] = useState("smoke");
  const [targets, setTargets] = useState<string[]>([]);
  const [iterations, setIterations] = useState(2);
  const [warmup, setWarmup] = useState(1);
  const [maxTokens, setMaxTokens] = useState(128);

  const candidates = (deployments.data ?? []).filter(
    (d) => d.state === "available" && supports(d, "streaming"),
  );
  const dataset = datasets.data?.find((d) => d.id === datasetId);

  const start = useMutation({
    mutationFn: () =>
      api.startBenchmark({
        name,
        dataset_id: datasetId,
        iterations,
        warmup_runs: warmup,
        params: { temperature: 0, max_tokens: maxTokens, seed: 42 },
        targets: targets.map((t) => {
          const [provider_id, ...rest] = t.split("/");
          return { provider_id: provider_id!, model: rest.join("/") };
        }),
      }),
    onSuccess: (run) => {
      setSelectedRun(run.id);
      void queryClient.invalidateQueries({ queryKey: ["benchmarks"] });
    },
  });

  return (
    <div className="p-6">
      <PageHeader
        title="Benchmark Lab"
        description="Reproducible runs over versioned prompt datasets. Cold (first request) and warm measurements are reported separately, and results from different hardware, quantizations or token-count sources are flagged rather than presented as equivalent."
      />
      <ErrorAlert error={datasets.error ?? deployments.error ?? start.error} className="mb-4" />
      <Card>
        <CardHeader>
          <CardTitle>New run</CardTitle>
          <CardDescription>
            Each target is measured with an explicit provider (no routing, no fallback).
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <Label htmlFor="b-name">Name</Label>
              <Input id="b-name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="b-dataset">Prompt dataset</Label>
              <NativeSelect
                id="b-dataset"
                value={datasetId}
                onChange={(e) => setDatasetId(e.target.value)}
              >
                {(datasets.data ?? []).map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.id}@{d.version} — {d.prompts.length} prompts
                  </option>
                ))}
              </NativeSelect>
              {dataset && (
                <ul className="mt-1 list-disc pl-4 text-xs text-muted-foreground">
                  {dataset.prompts.map((p) => (
                    <li key={p.id}>{p.text}</li>
                  ))}
                </ul>
              )}
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div className="flex flex-col gap-1">
                <Label htmlFor="b-iter">Iterations</Label>
                <Input
                  id="b-iter"
                  type="number"
                  min={1}
                  max={50}
                  value={iterations}
                  onChange={(e) => setIterations(Number(e.target.value))}
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="b-warm">Discarded warm-up</Label>
                <Input
                  id="b-warm"
                  type="number"
                  min={0}
                  max={10}
                  value={warmup}
                  onChange={(e) => setWarmup(Number(e.target.value))}
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="b-max">Max tokens</Label>
                <Input
                  id="b-max"
                  type="number"
                  min={1}
                  value={maxTokens}
                  onChange={(e) => setMaxTokens(Number(e.target.value))}
                />
              </div>
            </div>
          </div>
          <fieldset className="flex flex-col gap-1">
            <legend className="mb-1 text-xs font-medium text-muted-foreground">
              Targets (streaming deployments)
            </legend>
            <div className="max-h-60 overflow-y-auto rounded border p-2">
              {candidates.map((d) => (
                <label key={d.id} className="flex items-center gap-2 py-0.5 font-mono text-xs">
                  <input
                    type="checkbox"
                    checked={targets.includes(d.id)}
                    onChange={(e) =>
                      setTargets((prev) =>
                        e.target.checked ? [...prev, d.id] : prev.filter((t) => t !== d.id),
                      )
                    }
                  />
                  {d.id}
                </label>
              ))}
              {candidates.length === 0 && (
                <p className="text-xs text-muted-foreground">No available deployments.</p>
              )}
            </div>
            <Button
              className="mt-2 self-start"
              onClick={() => start.mutate()}
              disabled={targets.length === 0 || start.isPending}
            >
              <Play /> Run benchmark
            </Button>
          </fieldset>
        </CardContent>
      </Card>

      {current.data && <RunResults run={current.data} />}

      <Card className="mt-4">
        <CardHeader>
          <CardTitle>History</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <THead>
              <TR>
                <TH>Run</TH>
                <TH>Dataset</TH>
                <TH>Targets</TH>
                <TH>Status</TH>
                <TH>Comparable</TH>
                <TH>Created</TH>
              </TR>
            </THead>
            <TBody>
              {(runs.data ?? []).map((r) => (
                <TR key={r.id} className="cursor-pointer" onClick={() => setSelectedRun(r.id)}>
                  <TD className="font-medium">{r.name}</TD>
                  <TD className="text-xs">
                    {r.dataset_id}@{r.dataset_version}
                  </TD>
                  <TD className="font-mono text-[11px]">
                    {r.targets.map((t) => `${t.provider_id}/${t.model}`).join(", ")}
                  </TD>
                  <TD>
                    <StatusBadge status={r.status ?? "pending"} />
                  </TD>
                  <TD className="text-xs">
                    {r.status === "completed" ? (r.comparable ? "yes" : "no — see notes") : "—"}
                  </TD>
                  <TD className="text-xs">{time(r.created_at)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
