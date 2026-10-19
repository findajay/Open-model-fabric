import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { BarChart } from "@/components/BarChart";
import { ErrorAlert } from "@/components/ErrorAlert";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api } from "@/lib/api";
import { ms, num, pct, time } from "@/lib/format";

function Stat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
        {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
      </CardContent>
    </Card>
  );
}

export function DashboardPage() {
  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: api.providers,
    refetchInterval: 15_000,
  });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: () => api.deployments() });
  const metrics = useQuery({
    queryKey: ["metrics"],
    queryFn: () => api.metrics(),
    refetchInterval: 10_000,
  });
  const recent = useQuery({
    queryKey: ["recent"],
    queryFn: () => api.recentRequests(20),
    refetchInterval: 10_000,
  });

  const up = (providers.data ?? []).filter((p) => p.health.status === "up").length;
  const enabled = (providers.data ?? []).filter((p) => p.enabled).length;
  const active = (deployments.data ?? []).filter((d) => d.state === "available").length;
  const stats = metrics.data?.providers ?? [];

  return (
    <div className="p-6">
      <PageHeader
        title="Dashboard"
        description="Provider status, active models and recent inference traffic. Request records never contain prompt or completion text."
      />
      <ErrorAlert error={providers.error ?? metrics.error} className="mb-4" />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="Providers up"
          value={`${up} / ${enabled}`}
          hint="enabled providers reporting healthy"
        />
        <Stat label="Active models" value={active} hint="deployments currently served" />
        <Stat
          label="Requests (window)"
          value={metrics.data?.window_requests ?? "—"}
          hint="last 500 records"
        />
        <Stat
          label="Error rate"
          value={
            stats.length
              ? pct(
                  stats.reduce((a, s) => a + s.errors, 0) /
                    Math.max(
                      1,
                      stats.reduce((a, s) => a + s.requests, 0),
                    ),
                )
              : "—"
          }
          hint="failed / all requests"
        />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Configured providers</CardTitle>
            <CardDescription>
              <Link className="underline" to="/providers">
                Manage providers
              </Link>
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <THead>
                <TR>
                  <TH>Provider</TH>
                  <TH>Adapter</TH>
                  <TH>Status</TH>
                  <TH>Locality</TH>
                  <TH className="text-right">Check latency</TH>
                </TR>
              </THead>
              <TBody>
                {(providers.data ?? []).map((p) => (
                  <TR key={p.id}>
                    <TD className="font-medium">
                      {p.display_name ?? p.id}{" "}
                      {p.is_default && <Badge variant="outline">default</Badge>}
                    </TD>
                    <TD className="font-mono text-xs">{p.adapter_type}</TD>
                    <TD>
                      <StatusBadge status={p.health.status} />
                    </TD>
                    <TD className="text-xs">{p.locality}</TD>
                    <TD className="text-right font-mono text-xs">{ms(p.health.latency_ms)}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Median request latency by provider</CardTitle>
            <CardDescription>
              p50 of completed requests in the window; hover for p95 and time to first token.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <BarChart
              ariaLabel="Median latency per provider"
              format={(v) => ms(v)}
              data={stats.map((s) => ({
                label: s.provider_id,
                value: s.p50_latency_ms,
                detail: [
                  `p95 ${ms(s.p95_latency_ms)}`,
                  `mean TTFT ${ms(s.mean_ttft_ms)}`,
                  `${s.requests} requests, ${s.errors} failed`,
                ],
              }))}
            />
            <Table className="mt-3">
              <THead>
                <TR>
                  <TH>Provider</TH>
                  <TH className="text-right">Requests</TH>
                  <TH className="text-right">Errors</TH>
                  <TH className="text-right">p50</TH>
                  <TH className="text-right">p95</TH>
                  <TH className="text-right">TTFT</TH>
                  <TH className="text-right">tok/s</TH>
                </TR>
              </THead>
              <TBody>
                {stats.map((s) => (
                  <TR key={s.provider_id}>
                    <TD>{s.provider_id}</TD>
                    <TD className="text-right tabular-nums">{s.requests}</TD>
                    <TD className="text-right tabular-nums">{pct(s.error_rate)}</TD>
                    <TD className="text-right tabular-nums">{ms(s.p50_latency_ms)}</TD>
                    <TD className="text-right tabular-nums">{ms(s.p95_latency_ms)}</TD>
                    <TD className="text-right tabular-nums">{ms(s.mean_ttft_ms)}</TD>
                    <TD className="text-right tabular-nums">{num(s.mean_tokens_per_second)}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </CardContent>
        </Card>
      </div>

      <Card className="mt-4">
        <CardHeader>
          <CardTitle>Recent inference requests</CardTitle>
          <CardDescription>
            Selected provider and model are recorded for every request.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <THead>
              <TR>
                <TH>Time</TH>
                <TH>Request</TH>
                <TH>Provider / model</TH>
                <TH>Policy</TH>
                <TH>Status</TH>
                <TH className="text-right">Latency</TH>
                <TH className="text-right">TTFT</TH>
                <TH className="text-right">Tokens</TH>
              </TR>
            </THead>
            <TBody>
              {(recent.data ?? []).map((r) => (
                <TR key={r.request_id}>
                  <TD className="whitespace-nowrap text-xs">{time(r.created_at)}</TD>
                  <TD className="font-mono text-xs">{r.request_id}</TD>
                  <TD className="font-mono text-xs">
                    {r.provider_id ?? "—"} / {r.model ?? "—"}
                  </TD>
                  <TD className="text-xs">
                    {r.routing_policy ?? "—"}
                    {r.streaming ? " · stream" : ""}
                    {r.operation === "embeddings" ? " · embeddings" : ""}
                  </TD>
                  <TD>
                    <StatusBadge status={r.status} />
                    {r.error_code && (
                      <span className="ml-1 font-mono text-[11px] text-muted-foreground">
                        {r.error_code}
                      </span>
                    )}
                  </TD>
                  <TD className="text-right tabular-nums">{ms(r.latency_ms)}</TD>
                  <TD className="text-right tabular-nums">{ms(r.ttft_ms)}</TD>
                  <TD className="text-right tabular-nums">{r.completion_tokens ?? "—"}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
          {recent.data?.length === 0 && (
            <p className="text-sm text-muted-foreground">No requests yet — try the Playground.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
