import type { ModelDeployment } from "@omf/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Loader2, MessageSquarePlus, Trash2, User } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { CapabilityBadges } from "@/components/CapabilityBadges";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Markdown } from "@/components/Markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Label, NativeSelect } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Composer, type ComposerCapabilities } from "@/features/playground/Composer";
import { fromConversation, useChat, type UiMessage } from "@/features/playground/useChat";
import { api } from "@/lib/api";
import { supports } from "@/lib/capabilities";
import { ms, num } from "@/lib/format";
import { cn } from "@/lib/utils";

const AUTO = "__auto__";

/** Capabilities offered for a selection = those claimed by *every* candidate deployment. */
export function capabilitiesFor(candidates: ModelDeployment[]): ComposerCapabilities {
  if (candidates.length === 0) return { vision: false, json: false };
  return {
    vision: candidates.every((d) => supports(d, "vision_input")),
    json: candidates.every((d) => supports(d, "structured_output")),
  };
}

function MessageView({ message }: { message: UiMessage }) {
  const isUser = message.role === "user";
  return (
    <div
      className={cn("flex gap-3", isUser && "flex-row-reverse")}
      data-testid={`message-${message.role}`}
    >
      <div className="mt-1 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted">
        {isUser ? <User className="size-4" aria-hidden /> : <Bot className="size-4" aria-hidden />}
      </div>
      <div className={cn("min-w-0 max-w-[85%]", isUser && "text-right")}>
        <div
          className={cn(
            "inline-block rounded-lg px-3 py-2 text-left",
            isUser ? "bg-primary text-primary-foreground" : "border bg-card",
          )}
        >
          {isUser ? (
            <p className="whitespace-pre-wrap text-sm">{message.text}</p>
          ) : message.text ? (
            <Markdown>{message.text}</Markdown>
          ) : message.pending ? (
            <Loader2
              className="size-4 animate-spin text-muted-foreground"
              aria-label="Waiting for first token"
            />
          ) : null}
          {isUser && message.images ? (
            <p className="mt-1 text-xs opacity-80">{message.images} image(s) attached</p>
          ) : null}
        </div>
        {message.error && <ErrorAlert className="mt-2" error={new Error(message.error.title)} />}
        {message.error?.code && (
          <p className="mt-1 font-mono text-[11px] text-muted-foreground">
            {message.error.code}
            {message.error.requestId ? ` · ${message.error.requestId}` : ""}
          </p>
        )}
        {!isUser && message.providerId && (
          <p
            className="mt-1 flex flex-wrap gap-x-3 font-mono text-[11px] text-muted-foreground"
            data-testid="message-meta"
          >
            <span>
              {message.providerId}/{message.model}
            </span>
            {message.timings && <span>ttft {ms(message.timings.ttft_ms)}</span>}
            {message.timings && <span>total {ms(message.timings.total_ms)}</span>}
            {message.timings?.tokens_per_second != null && (
              <span>{num(message.timings.tokens_per_second)} tok/s</span>
            )}
            {message.usage && (
              <span>
                {message.usage.prompt_tokens ?? "?"}→{message.usage.completion_tokens ?? "?"} tokens
                {message.usage.source === "estimated" ? " (est.)" : ""}
              </span>
            )}
            {message.finishReason && message.finishReason !== "stop" && (
              <span>finish: {message.finishReason}</span>
            )}
          </p>
        )}
      </div>
    </div>
  );
}

export function PlaygroundPage() {
  const queryClient = useQueryClient();
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: () => api.deployments() });
  const conversations = useQuery({ queryKey: ["conversations"], queryFn: api.conversations });

  const [providerId, setProviderId] = useState<string>(AUTO);
  const [model, setModel] = useState<string>("");
  const [temperature, setTemperature] = useState(0.7);
  const [maxTokens, setMaxTokens] = useState(512);
  const [stream, setStream] = useState(true);

  const onConversation = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["conversations"] });
  }, [queryClient]);
  const chat = useChat(onConversation);

  const enabledProviders = (providers.data ?? []).filter((p) => p.enabled);
  const chatDeployments = (deployments.data ?? []).filter(
    (d) => d.state === "available" && supports(d, "chat"),
  );

  // Options for the model selector depend on the provider choice.
  const modelOptions = useMemo(() => {
    if (providerId === AUTO) {
      const ids = [
        ...new Set(chatDeployments.map((d) => d.model_id).filter((x): x is string => !!x)),
      ];
      return ids.map((id) => ({ value: id, label: id }));
    }
    return chatDeployments
      .filter((d) => d.provider_id === providerId)
      .map((d) => ({
        value: d.provider_model,
        label: d.model_id ? `${d.provider_model} (${d.model_id})` : d.provider_model,
      }));
  }, [providerId, chatDeployments]);

  // Keep the model valid when the provider changes; "" means the routing default model.
  const selectedModel = modelOptions.some((o) => o.value === model)
    ? model
    : providerId === AUTO
      ? ""
      : (modelOptions[0]?.value ?? "");

  const candidates = useMemo(() => {
    if (!selectedModel) return [];
    if (providerId === AUTO) return chatDeployments.filter((d) => d.model_id === selectedModel);
    return chatDeployments.filter(
      (d) => d.provider_id === providerId && d.provider_model === selectedModel,
    );
  }, [selectedModel, providerId, chatDeployments]);
  const capabilities = capabilitiesFor(candidates);

  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => bottom.current?.scrollIntoView({ block: "end" }), [chat.messages]);

  async function openConversation(id: string) {
    const conversation = await api.conversation(id);
    chat.load(conversation.id, fromConversation(conversation));
  }

  return (
    <div className="flex h-full">
      <aside
        className="hidden w-60 shrink-0 flex-col border-r lg:flex"
        aria-label="Conversation history"
      >
        <div className="p-3">
          <Button variant="outline" className="w-full" onClick={() => chat.load(null, [])}>
            <MessageSquarePlus /> New chat
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto px-2 pb-3">
          {(conversations.data ?? []).map((c) => (
            <div
              key={c.id}
              className={cn(
                "group flex items-center gap-1 rounded-md px-2 py-1.5 text-sm hover:bg-accent",
                chat.conversationId === c.id && "bg-accent font-medium",
              )}
            >
              <button
                type="button"
                className="min-w-0 flex-1 truncate text-left"
                onClick={() => void openConversation(c.id)}
              >
                {c.title}
              </button>
              <button
                type="button"
                aria-label={`Delete ${c.title}`}
                className="opacity-0 group-hover:opacity-100"
                onClick={async () => {
                  await api.deleteConversation(c.id);
                  if (chat.conversationId === c.id) chat.load(null, []);
                  onConversation();
                }}
              >
                <Trash2 className="size-3.5 text-muted-foreground" />
              </button>
            </div>
          ))}
          {conversations.data?.length === 0 && (
            <p className="px-2 text-xs text-muted-foreground">No conversations yet.</p>
          )}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-end gap-3 border-b px-4 py-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="provider">Provider</Label>
            <NativeSelect
              id="provider"
              className="w-48"
              value={providerId}
              onChange={(e) => {
                setProviderId(e.target.value);
                setModel("");
              }}
            >
              <option value={AUTO}>Auto (routing policy)</option>
              {enabledProviders.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name ?? p.id} {p.health.status !== "up" ? `· ${p.health.status}` : ""}
                </option>
              ))}
            </NativeSelect>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="model">Model</Label>
            <NativeSelect
              id="model"
              className="w-72"
              value={selectedModel}
              onChange={(e) => setModel(e.target.value)}
            >
              {providerId === AUTO && <option value="">Default model (settings)</option>}
              {modelOptions.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </NativeSelect>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="temperature">Temperature {temperature.toFixed(1)}</Label>
            <input
              id="temperature"
              type="range"
              min={0}
              max={2}
              step={0.1}
              value={temperature}
              onChange={(e) => setTemperature(Number(e.target.value))}
              className="w-32 accent-[var(--viz-series-1)]"
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="max-tokens">Max tokens</Label>
            <Input
              id="max-tokens"
              type="number"
              min={1}
              max={131072}
              className="w-24"
              value={maxTokens}
              onChange={(e) => setMaxTokens(Math.max(1, Number(e.target.value) || 1))}
            />
          </div>
          <label className="flex items-center gap-2 pb-2 text-xs text-muted-foreground">
            <Switch checked={stream} onCheckedChange={setStream} aria-label="Stream responses" />{" "}
            Stream
          </label>
          {candidates.length === 1 && candidates[0] && (
            <div className="pb-1.5" aria-label="Selected model capabilities">
              <CapabilityBadges deployment={candidates[0]} />
            </div>
          )}
          {providerId === AUTO && (
            <Badge variant="outline" className="mb-2">
              routed by policy
            </Badge>
          )}
        </div>

        <ErrorAlert error={providers.error ?? deployments.error} className="m-4" />

        <div className="flex-1 overflow-y-auto px-4 py-4">
          <div className="mx-auto flex max-w-3xl flex-col gap-4">
            {chat.messages.length === 0 && (
              <div className="mt-16 text-center text-sm text-muted-foreground">
                <p className="font-medium text-foreground">Same interface, any inference engine.</p>
                <p className="mt-1">
                  Pick a provider and model above — mock, Ollama or any OpenAI-compatible server.
                  Controls adapt to the model&apos;s declared capabilities.
                </p>
              </div>
            )}
            {chat.messages.map((m) => (
              <MessageView key={m.id} message={m} />
            ))}
            <div ref={bottom} />
          </div>
        </div>

        <div className="border-t px-4 py-3">
          <div className="mx-auto max-w-3xl">
            <Composer
              capabilities={capabilities}
              streaming={chat.busy}
              onCancel={() => void chat.cancel()}
              onSend={(input) =>
                void chat.send(input, {
                  providerId: providerId === AUTO ? null : providerId,
                  model: selectedModel || null,
                  temperature,
                  maxTokens,
                  stream,
                })
              }
            />
          </div>
        </div>
      </div>
    </div>
  );
}
