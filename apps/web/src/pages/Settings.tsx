import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Save } from "lucide-react";
import { useState } from "react";

import { ErrorAlert } from "@/components/ErrorAlert";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, NativeSelect } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { api, getToken, setToken } from "@/lib/api";

interface Routing {
  mode: string;
  preferred_provider_ids: string[];
  fallback_provider_ids: string[];
  allow_cross_provider_fallback: boolean;
  max_attempts: number;
  respect_health: boolean;
}

interface Runtime {
  default_provider_id: string | null;
  default_model: string | null;
  request_timeout_s: number;
  default_storage_id: string | null;
  routing: Routing;
}

const csv = (v: string) =>
  v
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

export function SettingsPage() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers });
  const storage = useQuery({ queryKey: ["storage"], queryFn: api.storage });
  const [edited, setForm] = useState<Runtime | null>(null);
  const form = edited ?? (settings.data?.runtime as unknown as Runtime | undefined) ?? null;
  const [token, setTokenInput] = useState(getToken() ?? "");
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: (value: Runtime) => api.updateSettings(value as unknown as Record<string, unknown>),
    onSuccess: (data) => {
      setForm(data.runtime as unknown as Runtime);
      setSaved(true);
      void queryClient.invalidateQueries();
    },
  });

  const update = (patch: Partial<Runtime>) => {
    setSaved(false);
    setForm(form ? { ...form, ...patch } : null);
  };
  const updateRouting = (patch: Partial<Routing>) => {
    setSaved(false);
    setForm(form ? { ...form, routing: { ...form.routing, ...patch } } : null);
  };
  const readOnly = settings.data?.read_only ?? {};

  return (
    <div className="p-6">
      <PageHeader
        title="Settings"
        description="Runtime settings are validated, persisted and audited. Security settings come from the configuration profile and cannot be changed from the browser."
      />
      <ErrorAlert error={settings.error} className="mb-4" />

      <Card className="mb-4">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="size-4" aria-hidden /> API access token
          </CardTitle>
          <CardDescription>
            Your token for this platform&apos;s API when authentication is enabled (kept in session
            storage for this tab). Inference provider and cloud credentials are never entered or
            stored in the browser.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex gap-2">
          <Input
            type="password"
            aria-label="API token"
            value={token}
            onChange={(e) => setTokenInput(e.target.value)}
            className="max-w-md"
            autoComplete="off"
          />
          <Button
            variant="outline"
            onClick={() => {
              setToken(token || null);
              void queryClient.invalidateQueries();
            }}
          >
            Use token
          </Button>
        </CardContent>
      </Card>

      {form && (
        <Card>
          <CardHeader>
            <CardTitle>Defaults, timeouts and routing</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4 md:grid-cols-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor="s-provider">Default inference provider</Label>
              <NativeSelect
                id="s-provider"
                value={form.default_provider_id ?? ""}
                onChange={(e) => update({ default_provider_id: e.target.value || null })}
              >
                <option value="">— none —</option>
                {(providers.data ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.id}
                  </option>
                ))}
              </NativeSelect>
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="s-model">Default model (catalog id or provider-native name)</Label>
              <Input
                id="s-model"
                value={form.default_model ?? ""}
                onChange={(e) => update({ default_model: e.target.value || null })}
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="s-timeout">Request timeout (seconds, across attempts)</Label>
              <Input
                id="s-timeout"
                type="number"
                min={1}
                value={form.request_timeout_s}
                onChange={(e) => update({ request_timeout_s: Number(e.target.value) })}
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="s-storage">Default artifact storage</Label>
              <NativeSelect
                id="s-storage"
                value={form.default_storage_id ?? ""}
                onChange={(e) => update({ default_storage_id: e.target.value || null })}
              >
                {(storage.data ?? []).map((s) => (
                  <option key={s.storage_provider_id} value={s.storage_provider_id}>
                    {s.storage_provider_id} ({s.adapter_type})
                  </option>
                ))}
              </NativeSelect>
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="s-mode">Routing policy</Label>
              <NativeSelect
                id="s-mode"
                value={form.routing.mode}
                onChange={(e) => updateRouting({ mode: e.target.value })}
              >
                <option value="default">Default provider</option>
                <option value="preferred_with_fallback">
                  Preferred provider (with optional fallback)
                </option>
                <option value="capability_based">Capability-based selection</option>
              </NativeSelect>
              <p className="text-[11px] text-muted-foreground">
                Selecting a provider explicitly in a request always bypasses the policy and never
                falls back.
              </p>
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="s-preferred">Preferred providers (comma separated, in order)</Label>
              <Input
                id="s-preferred"
                value={form.routing.preferred_provider_ids.join(", ")}
                onChange={(e) => updateRouting({ preferred_provider_ids: csv(e.target.value) })}
              />
            </div>
            <label className="flex items-center gap-2 text-sm">
              <Switch
                checked={form.routing.allow_cross_provider_fallback}
                onCheckedChange={(v) => updateRouting({ allow_cross_provider_fallback: v })}
                aria-label="Allow cross-provider fallback"
              />
              Allow cross-provider fallback (only for retryable errors, before any output)
            </label>
            <div className="flex flex-col gap-1">
              <Label htmlFor="s-fallback">Fallback providers (comma separated)</Label>
              <Input
                id="s-fallback"
                value={form.routing.fallback_provider_ids.join(", ")}
                disabled={!form.routing.allow_cross_provider_fallback}
                onChange={(e) => updateRouting({ fallback_provider_ids: csv(e.target.value) })}
              />
            </div>
            <label className="flex items-center gap-2 text-sm">
              <Switch
                checked={form.routing.respect_health}
                onCheckedChange={(v) => updateRouting({ respect_health: v })}
                aria-label="Skip providers that are down"
              />
              Skip providers whose health check reports them down
            </label>
            <div className="flex flex-col gap-1">
              <Label htmlFor="s-attempts">Max attempts</Label>
              <Input
                id="s-attempts"
                type="number"
                min={1}
                max={5}
                value={form.routing.max_attempts}
                onChange={(e) => updateRouting({ max_attempts: Number(e.target.value) })}
              />
            </div>
            <div className="flex items-center gap-3 md:col-span-2">
              <Button onClick={() => save.mutate(form)} disabled={save.isPending}>
                <Save /> Save settings
              </Button>
              {saved && <span className="text-xs text-status-good">Saved.</span>}
            </div>
            <ErrorAlert error={save.error} className="md:col-span-2" />
          </CardContent>
        </Card>
      )}

      <Card className="mt-4">
        <CardHeader>
          <CardTitle>Security (read-only, from the configuration profile)</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-xs md:grid-cols-2">
            {Object.entries(readOnly).map(([k, v]) => (
              <div key={k} className="flex flex-col">
                <dt className="font-medium text-muted-foreground">{k}</dt>
                <dd className="break-all font-mono">
                  {typeof v === "object" ? JSON.stringify(v) : String(v)}
                </dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
    </div>
  );
}
