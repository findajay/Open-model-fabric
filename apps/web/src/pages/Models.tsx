import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, ShieldCheck } from "lucide-react";

import { CapabilityBadges, EvidenceLegend } from "@/components/CapabilityBadges";
import { ErrorAlert } from "@/components/ErrorAlert";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api } from "@/lib/api";
import { CAPABILITY_LABELS } from "@/lib/capabilities";

const LICENSE_VARIANT = { allowed: "good", restricted: "warn", unknown: "secondary" } as const;

export function ModelsPage() {
  const queryClient = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: api.models });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: () => api.deployments() });
  const refresh = useMutation({
    mutationFn: () => api.deployments({ refresh: true }),
    onSuccess: (data) => queryClient.setQueryData(["deployments"], data),
  });
  const verify = useMutation({
    mutationFn: ({ providerId, model }: { providerId: string; model: string }) =>
      api.verifyDeployment(providerId, model),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["deployments"] }),
  });

  return (
    <div className="p-6">
      <PageHeader
        title="Model Registry"
        description="Model definitions describe what a model is (family, license, context window, artifacts) independently of where it runs. Deployments bind a model to a provider."
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => refresh.mutate()}
            disabled={refresh.isPending}
          >
            <RefreshCw className={refresh.isPending ? "animate-spin" : ""} /> Rediscover models
          </Button>
        }
      />
      <ErrorAlert error={models.error ?? deployments.error ?? verify.error} className="mb-4" />

      <Card>
        <CardHeader>
          <CardTitle>Catalog</CardTitle>
          <CardDescription>
            Open-weight licenses are not open-source software licenses — check each model card
            before use.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <THead>
              <TR>
                <TH>Model</TH>
                <TH>Family</TH>
                <TH>Modalities</TH>
                <TH className="text-right">Context</TH>
                <TH>Quantizations</TH>
                <TH>Runtimes</TH>
                <TH>Declared capabilities</TH>
                <TH>License</TH>
                <TH>Artifacts</TH>
              </TR>
            </THead>
            <TBody>
              {(models.data ?? []).map((m) => (
                <TR key={m.id}>
                  <TD>
                    <div className="font-medium">{m.display_name}</div>
                    <div className="font-mono text-[11px] text-muted-foreground">{m.id}</div>
                    {m.parameters && (
                      <div className="text-[11px] text-muted-foreground">{m.parameters} params</div>
                    )}
                  </TD>
                  <TD className="text-xs">{m.family ?? "—"}</TD>
                  <TD className="text-xs">
                    {(m.input_modalities ?? []).join(", ")} →{" "}
                    {(m.output_modalities ?? []).join(", ")}
                  </TD>
                  <TD className="text-right font-mono text-xs">
                    {m.context_window?.toLocaleString() ?? "—"}
                  </TD>
                  <TD className="text-xs">{(m.quantizations ?? []).join(", ") || "—"}</TD>
                  <TD className="text-xs">{(m.runtime_compatibility ?? []).join(", ") || "—"}</TD>
                  <TD>
                    <div className="flex flex-wrap gap-1">
                      {(m.capabilities?.claims ?? []).map((c) => (
                        <Badge key={c.capability} variant="outline">
                          {CAPABILITY_LABELS[c.capability]}
                        </Badge>
                      ))}
                    </div>
                  </TD>
                  <TD>
                    {m.license ? (
                      <div className="flex flex-col gap-0.5">
                        {m.license.url ? (
                          <a
                            className="text-xs underline"
                            href={m.license.url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {m.license.name}
                          </a>
                        ) : (
                          <span className="text-xs">{m.license.name}</span>
                        )}
                        <Badge variant={LICENSE_VARIANT[m.license.commercial_use ?? "unknown"]}>
                          commercial: {m.license.commercial_use ?? "unknown"}
                        </Badge>
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground">unknown</span>
                    )}
                  </TD>
                  <TD className="font-mono text-[11px]">
                    {(m.artifacts ?? []).map((a) => (
                      <div key={`${a.storage_provider_id}:${a.key}`}>
                        {a.storage_provider_id}:{a.key}
                      </div>
                    ))}
                    {(m.artifacts ?? []).length === 0 && "—"}
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </CardContent>
      </Card>

      <Card className="mt-4">
        <CardHeader>
          <CardTitle>Deployments</CardTitle>
          <CardDescription>
            Capabilities are authoritative: anything not listed is unsupported. Verify runs a small
            probe against the deployment and records the capabilities that work.
          </CardDescription>
          <EvidenceLegend />
        </CardHeader>
        <CardContent>
          <Table>
            <THead>
              <TR>
                <TH>Deployment</TH>
                <TH>Catalog model</TH>
                <TH>State</TH>
                <TH className="text-right">Context</TH>
                <TH>Quantization</TH>
                <TH>Capabilities</TH>
                <TH />
              </TR>
            </THead>
            <TBody>
              {(deployments.data ?? []).map((d) => (
                <TR key={d.id}>
                  <TD className="font-mono text-xs">{d.id}</TD>
                  <TD className="font-mono text-xs">
                    {d.model_id ?? <span className="text-muted-foreground">unmapped</span>}
                  </TD>
                  <TD>
                    <StatusBadge status={d.state ?? "unknown"} />
                  </TD>
                  <TD className="text-right font-mono text-xs">
                    {d.context_window?.toLocaleString() ?? "—"}
                  </TD>
                  <TD className="text-xs">{d.quantization ?? "—"}</TD>
                  <TD>
                    <CapabilityBadges deployment={d} />
                  </TD>
                  <TD>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={verify.isPending || d.state !== "available"}
                      onClick={() =>
                        verify.mutate({ providerId: d.provider_id, model: d.provider_model })
                      }
                    >
                      <ShieldCheck /> Verify
                    </Button>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
          {verify.data && (
            <div className="mt-3 rounded-md border p-3 text-xs" role="status">
              <div className="mb-1 font-medium">Verification of {verify.data.deployment.id}</div>
              {verify.data.checks.map((c) => (
                <div key={c.capability} className="font-mono">
                  {c.capability}: {c.result} — {c.detail}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
