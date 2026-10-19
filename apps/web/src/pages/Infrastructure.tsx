import type { ComputeEnvironment, ComputeLifecycleOperation } from "@omf/sdk";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HardDrive, Play, Plus, RefreshCw, Square, Trash2 } from "lucide-react";
import { useState } from "react";

import { ErrorAlert } from "@/components/ErrorAlert";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api } from "@/lib/api";
import { bytes, time } from "@/lib/format";

function Artifacts({ storageId }: { storageId: string }) {
  const [prefix, setPrefix] = useState("");
  const artifacts = useQuery({
    queryKey: ["artifacts", storageId, prefix],
    queryFn: () => api.artifacts(storageId, prefix),
  });
  return (
    <div className="mt-2">
      <Input
        aria-label={`Filter artifacts in ${storageId}`}
        placeholder="key prefix, e.g. benchmarks/"
        value={prefix}
        onChange={(e) => setPrefix(e.target.value)}
        className="mb-2 h-8 max-w-xs text-xs"
      />
      <ErrorAlert error={artifacts.error} />
      <Table>
        <THead>
          <TR>
            <TH>Key</TH>
            <TH className="text-right">Size</TH>
            <TH>Version</TH>
            <TH>SHA-256</TH>
            <TH>Updated</TH>
          </TR>
        </THead>
        <TBody>
          {(artifacts.data ?? []).map((a) => (
            <TR key={a.key}>
              <TD className="font-mono text-xs">{a.key}</TD>
              <TD className="text-right text-xs">{bytes(a.size_bytes)}</TD>
              <TD className="text-xs">v{a.version}</TD>
              <TD className="font-mono text-[11px]" title={a.sha256}>
                {a.sha256.slice(0, 12)}…
              </TD>
              <TD className="text-xs">{time(a.created_at)}</TD>
            </TR>
          ))}
        </TBody>
      </Table>
      {artifacts.data?.length === 0 && (
        <p className="text-xs text-muted-foreground">No artifacts.</p>
      )}
    </div>
  );
}

const LIFECYCLE: {
  op: ComputeLifecycleOperation;
  label: string;
  icon: typeof Play;
  confirm?: string;
}[] = [
  { op: "provision", label: "Provision", icon: Plus },
  { op: "start", label: "Start", icon: Play },
  { op: "stop", label: "Stop", icon: Square, confirm: "Stop {id}? Running requests will fail." },
  {
    op: "deprovision",
    label: "Deprovision",
    icon: Trash2,
    confirm: "Delete the infrastructure of {id}? This cannot be undone.",
  },
];

/** Lifecycle buttons; shown only for operations the environment allows (admin only). */
export function ComputeActions({ env }: { env: ComputeEnvironment }) {
  const queryClient = useQueryClient();
  const run = useMutation({
    mutationFn: (op: ComputeLifecycleOperation) => api.computeOperation(env.id, op),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["compute"] }),
  });
  const available = LIFECYCLE.filter((a) => (env.operations ?? []).includes(a.op));
  if (!available.length) return null;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2">
        {available.map(({ op, label, icon: Icon, confirm }) => (
          <Button
            key={op}
            size="sm"
            variant={op === "deprovision" ? "destructive" : "outline"}
            disabled={run.isPending}
            onClick={() => {
              if (confirm && !window.confirm(confirm.replace("{id}", env.id))) return;
              run.mutate(op);
            }}
          >
            <Icon /> {run.isPending && run.variables === op ? `${label}…` : label}
          </Button>
        ))}
      </div>
      <ErrorAlert error={run.error} />
    </div>
  );
}

export function InfrastructurePage() {
  const [refreshKey, setRefreshKey] = useState(0);
  const compute = useQuery({
    queryKey: ["compute", refreshKey],
    queryFn: () => api.compute(refreshKey > 0),
  });
  const storage = useQuery({ queryKey: ["storage", refreshKey], queryFn: api.storage });
  const [open, setOpen] = useState<string | null>(null);

  return (
    <div className="p-6">
      <PageHeader
        title="Infrastructure"
        description="Where inference servers run (compute) and where artifacts live (storage). These are independent of the inference protocol: the same OpenAI-compatible server can move between Colab, Docker and Kubernetes."
        actions={
          <Button variant="outline" size="sm" onClick={() => setRefreshKey((k) => k + 1)}>
            <RefreshCw /> Re-check
          </Button>
        }
      />
      <ErrorAlert error={compute.error ?? storage.error} className="mb-4" />
      <h2 className="mb-2 text-sm font-semibold">Compute environments</h2>
      <div className="grid gap-4 lg:grid-cols-2">
        {(compute.data ?? []).map((env) => {
          const r = env.resources;
          return (
            <Card key={env.id} data-testid={`compute-${env.id}`}>
              <CardHeader>
                <CardTitle className="flex flex-wrap items-center gap-2">
                  {env.display_name} <StatusBadge status={env.state ?? "unknown"} />
                  {env.ephemeral && <Badge variant="warn">ephemeral</Badge>}
                  <Badge variant="outline">
                    {env.managed === "manual" ? "manually managed" : "automatic"}
                  </Badge>
                </CardTitle>
                <CardDescription className="font-mono">
                  {env.id} · {env.kind}
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-2 text-xs">
                {r && (
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                    <span className="text-muted-foreground">CPU</span>
                    <span>
                      {r.cpu_count ?? "?"} cores {r.cpu_model ? `· ${r.cpu_model}` : ""}
                    </span>
                    <span className="text-muted-foreground">Memory</span>
                    <span>
                      {bytes(r.memory_total_bytes)}
                      {r.memory_available_bytes ? ` (${bytes(r.memory_available_bytes)} free)` : ""}
                    </span>
                    <span className="text-muted-foreground">Accelerator</span>
                    <span>{r.accelerator}</span>
                    <span className="text-muted-foreground">GPUs</span>
                    <span>
                      {(r.gpus ?? []).length
                        ? (r.gpus ?? [])
                            .map(
                              (g) =>
                                `${g.name}${g.memory_total_bytes ? ` (${bytes(g.memory_total_bytes)})` : ""}`,
                            )
                            .join(", ")
                        : "none"}
                    </span>
                    <span className="text-muted-foreground">Source</span>
                    <span>
                      {r.source === "declared" ? "declared by operator (not detected)" : r.source}
                    </span>
                  </div>
                )}
                {(env.endpoints ?? []).map((ep) => (
                  <div key={ep.url} className="rounded border p-2">
                    <div className="font-mono">{ep.url}</div>
                    <div className="mt-1 flex flex-wrap gap-2">
                      <Badge variant={ep.authorized ? "good" : "warn"}>
                        {ep.authorized ? "authorized" : "not authorized"}
                      </Badge>
                      <Badge
                        variant={
                          ep.reachable ? "good" : ep.reachable === false ? "bad" : "secondary"
                        }
                      >
                        {ep.reachable
                          ? "reachable"
                          : ep.reachable === false
                            ? "unreachable"
                            : "not probed"}
                      </Badge>
                    </div>
                    {ep.message && <p className="mt-1 text-muted-foreground">{ep.message}</p>}
                  </div>
                ))}
                <div className="text-muted-foreground">
                  Supported operations: {(env.operations ?? []).join(", ")}
                </div>
                <ComputeActions env={env} />
                {(env.notes ?? []).map((n) => (
                  <p key={n} className="text-muted-foreground">
                    {n}
                  </p>
                ))}
                <div className="text-[11px] text-muted-foreground">
                  Checked {time(env.checked_at)}
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      <h2 className="mb-2 mt-6 text-sm font-semibold">Artifact storage</h2>
      <div className="grid gap-4">
        {(storage.data ?? []).map((s) => (
          <Card key={s.storage_provider_id}>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <HardDrive className="size-4" aria-hidden /> {s.storage_provider_id}
                <StatusBadge status={s.status} />
                <Badge variant="outline">{s.adapter_type}</Badge>
              </CardTitle>
              <CardDescription>
                Intended use: {(s.intended_use ?? []).join(", ") || "—"}
                {s.message ? ` · ${s.message}` : ""}
              </CardDescription>
            </CardHeader>
            <CardContent className="text-xs">
              <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-[11px]">
                {JSON.stringify(s.details ?? {}, null, 2)}
              </pre>
              {s.status === "up" && (
                <Button
                  size="sm"
                  variant="ghost"
                  className="mt-2"
                  onClick={() =>
                    setOpen(open === s.storage_provider_id ? null : s.storage_provider_id)
                  }
                >
                  {open === s.storage_provider_id ? "Hide artifacts" : "Browse artifacts"}
                </Button>
              )}
              {open === s.storage_provider_id && <Artifacts storageId={s.storage_provider_id} />}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
