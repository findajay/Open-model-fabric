import type { ProviderCreate, ProviderView } from "@omf/sdk";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Plus, Star, Trash2, Zap } from "lucide-react";
import { useState } from "react";

import { ErrorAlert } from "@/components/ErrorAlert";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, NativeSelect, Textarea } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { ms } from "@/lib/format";

interface AdapterInfo {
  adapter_type: string;
  kind: string;
  display_name?: string;
  description?: string;
  default_locality?: string;
  endpoint_fields?: string[];
  secret_fields?: string[];
}

function AddProviderForm({ adapters, onDone }: { adapters: AdapterInfo[]; onDone: () => void }) {
  const inference = adapters.filter((a) => a.kind === "inference");
  const [adapterType, setAdapterType] = useState(inference[0]?.adapter_type ?? "ollama");
  const [id, setId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyRef, setApiKeyRef] = useState("");
  const [locality, setLocality] = useState<"" | "local" | "external">("");
  const [extra, setExtra] = useState("{}");
  const [warnings, setWarnings] = useState<string[]>([]);
  const adapter = inference.find((a) => a.adapter_type === adapterType);
  const needsUrl = adapter?.endpoint_fields?.includes("base_url");
  const takesKey = adapter?.secret_fields?.includes("api_key_ref");

  function definition(): ProviderCreate {
    let config: Record<string, unknown>;
    try {
      config = JSON.parse(extra || "{}") as Record<string, unknown>;
    } catch {
      throw new Error("Advanced configuration must be valid JSON");
    }
    if (needsUrl && baseUrl) config.base_url = baseUrl;
    if (takesKey && apiKeyRef) config.api_key_ref = apiKeyRef;
    return {
      id,
      adapter_type: adapterType,
      display_name: displayName || null,
      locality: locality || null,
      config,
    } as ProviderCreate;
  }

  const test = useMutation({ mutationFn: () => api.testProvider({ definition: definition() }) });
  const create = useMutation({
    mutationFn: () => api.createProvider(definition()),
    onSuccess: (result) => {
      setWarnings(result.warnings);
      onDone();
    },
  });
  const testResult = test.data as
    | {
        ok: boolean;
        models: string[];
        health: { status: string; message?: string };
        error?: { message: string };
      }
    | undefined;

  return (
    <Card className="mb-4" data-testid="add-provider-form">
      <CardHeader>
        <CardTitle>Add provider</CardTitle>
        <CardDescription>
          Credentials are never entered here. Reference a secret held by the backend, e.g.{" "}
          <code className="font-mono">env:OPENAI_API_KEY</code>. Endpoints are checked against the
          egress policy.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 md:grid-cols-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor="p-adapter">Adapter</Label>
          <NativeSelect
            id="p-adapter"
            value={adapterType}
            onChange={(e) => setAdapterType(e.target.value)}
          >
            {inference.map((a) => (
              <option key={a.adapter_type} value={a.adapter_type}>
                {a.display_name ?? a.adapter_type}
              </option>
            ))}
          </NativeSelect>
          {adapter?.description && (
            <p className="text-[11px] text-muted-foreground">{adapter.description}</p>
          )}
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="p-id">Provider id</Label>
          <Input
            id="p-id"
            placeholder="e.g. ollama-gpu"
            value={id}
            onChange={(e) => setId(e.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="p-name">Display name</Label>
          <Input id="p-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </div>
        {needsUrl && (
          <div className="flex flex-col gap-1">
            <Label htmlFor="p-url">Base URL</Label>
            <Input
              id="p-url"
              placeholder={
                adapterType === "ollama" ? "http://127.0.0.1:11434" : "http://vllm:8000/v1"
              }
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </div>
        )}
        {takesKey && (
          <div className="flex flex-col gap-1">
            <Label htmlFor="p-key">API key reference (optional)</Label>
            <Input
              id="p-key"
              placeholder="env:REMOTE_LLM_API_KEY"
              value={apiKeyRef}
              onChange={(e) => setApiKeyRef(e.target.value)}
            />
          </div>
        )}
        <div className="flex flex-col gap-1">
          <Label htmlFor="p-locality">Data locality</Label>
          <NativeSelect
            id="p-locality"
            value={locality}
            onChange={(e) => setLocality(e.target.value as typeof locality)}
          >
            <option value="">Adapter default ({adapter?.default_locality ?? "?"})</option>
            <option value="local">local — infrastructure you control</option>
            <option value="external">external — prompts leave your boundary</option>
          </NativeSelect>
        </div>
        <div className="flex flex-col gap-1 md:col-span-3">
          <Label htmlFor="p-extra">
            Advanced configuration (JSON, validated by the adapter schema)
          </Label>
          <Textarea
            id="p-extra"
            className="font-mono text-xs"
            value={extra}
            onChange={(e) => setExtra(e.target.value)}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2 md:col-span-3">
          <Button variant="outline" onClick={() => test.mutate()} disabled={!id || test.isPending}>
            <Zap /> Test connectivity
          </Button>
          <Button onClick={() => create.mutate()} disabled={!id || create.isPending}>
            <Plus /> Save provider
          </Button>
          {testResult && (
            <span className="text-xs" role="status">
              <StatusBadge status={testResult.ok ? "up" : "down"} />{" "}
              {testResult.ok
                ? `${testResult.models.length} model(s): ${testResult.models.slice(0, 4).join(", ")}`
                : (testResult.error?.message ?? testResult.health.message)}
            </span>
          )}
        </div>
        <ErrorAlert error={test.error ?? create.error} className="md:col-span-3" />
        {warnings.map((w) => (
          <p key={w} className="text-xs text-status-warn md:col-span-3">
            {w}
          </p>
        ))}
      </CardContent>
    </Card>
  );
}

function ProviderCard({ provider, onChanged }: { provider: ProviderView; onChanged: () => void }) {
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.updateProvider(provider.id, { enabled }),
    onSuccess: onChanged,
  });
  const health = useMutation({
    mutationFn: () => api.checkProviderHealth(provider.id),
    onSuccess: onChanged,
  });
  const test = useMutation({ mutationFn: () => api.testProvider({ provider_id: provider.id }) });
  const makeDefault = useMutation({
    mutationFn: () => api.updateSettings({ default_provider_id: provider.id, default_model: null }),
    onSuccess: onChanged,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteProvider(provider.id),
    onSuccess: onChanged,
  });
  const caps = provider.capabilities;
  const testResult = test.data as
    { ok: boolean; models: string[]; error?: { message: string } } | undefined;

  return (
    <Card data-testid={`provider-${provider.id}`}>
      <CardHeader className="flex-row items-start justify-between gap-2">
        <div>
          <CardTitle className="flex items-center gap-2">
            {provider.display_name ?? provider.id}
            {provider.is_default && (
              <Badge variant="outline">
                <Star className="size-3" aria-hidden /> default
              </Badge>
            )}
          </CardTitle>
          <CardDescription className="font-mono">
            {provider.id} · {provider.adapter_type} v{caps.adapter_version} · {provider.origin}
          </CardDescription>
        </div>
        <Switch
          checked={provider.enabled}
          onCheckedChange={(v) => toggle.mutate(v)}
          aria-label={`${provider.enabled ? "Disable" : "Enable"} ${provider.id}`}
        />
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={provider.health.status} />
          {provider.health.latency_ms != null && (
            <span className="text-muted-foreground">{ms(provider.health.latency_ms)}</span>
          )}
          <Badge variant={provider.locality === "external" ? "warn" : "secondary"}>
            {provider.locality}
          </Badge>
          <Badge variant="outline">
            sensitive prompts: {provider.allow_sensitive ? "allowed" : "blocked"}
          </Badge>
        </div>
        {provider.health.message && provider.health.status !== "up" && (
          <p className="text-status-bad">
            {provider.health.error_code ? `${provider.health.error_code}: ` : ""}
            {provider.health.message}
          </p>
        )}
        <div className="text-muted-foreground">
          Operations: {caps.operations.join(", ")} · streaming {caps.streaming ? "yes" : "no"} ·
          auth {caps.authentication} · model capabilities from {caps.model_capabilities_source}
        </div>
        {provider.secret_references.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {provider.secret_references.map((s) => (
              <Badge key={s.reference} variant={s.available ? "good" : "bad"}>
                {s.reference} · {s.available ? "available" : "missing"}
              </Badge>
            ))}
          </div>
        )}
        {Object.keys(provider.config).length > 0 && (
          <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-[11px]">
            {JSON.stringify(provider.config, null, 2)}
          </pre>
        )}
        <div className="flex flex-wrap gap-2 pt-1">
          <Button
            size="sm"
            variant="outline"
            onClick={() => test.mutate()}
            disabled={test.isPending}
          >
            <Zap /> Test
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => health.mutate()}
            disabled={health.isPending || !provider.enabled}
          >
            <Activity /> Health check
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => makeDefault.mutate()}
            disabled={provider.is_default || !provider.enabled || makeDefault.isPending}
          >
            <Star /> Make default
          </Button>
          {provider.origin === "runtime" && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
            >
              <Trash2 /> Remove
            </Button>
          )}
        </div>
        {testResult && (
          <p role="status">
            {testResult.ok
              ? `OK — ${testResult.models.length} model(s)`
              : `Failed — ${testResult.error?.message ?? "unreachable"}`}
          </p>
        )}
        <ErrorAlert
          error={toggle.error ?? health.error ?? test.error ?? makeDefault.error ?? remove.error}
        />
      </CardContent>
    </Card>
  );
}

export function ProvidersPage() {
  const queryClient = useQueryClient();
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers });
  const adapters = useQuery({ queryKey: ["adapters"], queryFn: api.adapters });
  const [adding, setAdding] = useState(false);
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["providers"] });
    void queryClient.invalidateQueries({ queryKey: ["deployments"] });
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Providers"
        description="Inference adapters registered in this build and the providers configured from them. Providers from the configuration profile can be enabled or disabled here; runtime providers can be added and removed without a restart."
        actions={
          <Button onClick={() => setAdding((a) => !a)} variant={adding ? "outline" : "default"}>
            <Plus /> {adding ? "Close" : "Add provider"}
          </Button>
        }
      />
      <ErrorAlert error={providers.error ?? adapters.error} className="mb-4" />
      {adding && adapters.data && (
        <AddProviderForm
          adapters={adapters.data as unknown as AdapterInfo[]}
          onDone={() => {
            setAdding(false);
            refresh();
          }}
        />
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        {(providers.data ?? []).map((p) => (
          <ProviderCard key={p.id} provider={p} onChanged={refresh} />
        ))}
      </div>

      <Card className="mt-6">
        <CardHeader>
          <CardTitle>Registered adapter types</CardTitle>
          <CardDescription>
            Adapters are compiled into the build and selected by id — configuration can never load
            arbitrary code.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-2 text-xs md:grid-cols-2">
          {((adapters.data ?? []) as unknown as AdapterInfo[]).map((a) => (
            <div key={`${a.kind}-${a.adapter_type}`} className="rounded border p-2">
              <div className="font-mono font-medium">
                {a.adapter_type} <Badge variant="outline">{a.kind}</Badge>
              </div>
              {a.description && <div className="text-muted-foreground">{a.description}</div>}
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
