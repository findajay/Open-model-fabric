import type { components } from "./schema.js";
import { readSse } from "./sse.js";

type S = components["schemas"];

export type ProviderView = S["ProviderView"];
export type ProviderCreate = S["ProviderCreate"];
export type ProviderUpdate = S["ProviderUpdate"];
export type ProviderHealth = S["ProviderHealth"];
export type ModelDefinition = S["ModelDefinition"];
export type ModelDeployment = S["ModelDeployment"];
export type ModelDetail = S["ModelDetail"];
export type ModelCreate = S["ModelCreate"];
export type ChatRequest = S["ChatRequest"];
export type ChatMessage = S["ChatMessage"];
export type InferenceResponse = S["InferenceResponse"];
export type EmbeddingsRequest = S["EmbeddingsRequest"];
export type EmbeddingResponse = S["EmbeddingResponse"];
export type Conversation = S["Conversation"];
export type ComputeEnvironment = S["ComputeEnvironment"];
export type ComputeLifecycleOperation = "provision" | "start" | "stop" | "deprovision";
export type StorageStatus = S["StorageStatus"];
export type StorageArtifact = S["StorageArtifact"];
export type BenchmarkRun = S["BenchmarkRun"];
export type BenchmarkRequest = S["BenchmarkRequest"];
export type BenchmarkDataset = S["BenchmarkDataset"];
export type InferenceRecord = S["InferenceRecord"];
export type MetricsSummary = S["MetricsSummary"];
export type SystemInfo = S["SystemInfo"];
export type AuditEntry = S["AuditEntry"];
export type SecretReferenceInfo = S["SecretReferenceInfo"];
export type ErrorDetail = S["ErrorDetail"];
export type Capability = S["CapabilityClaim"]["capability"];
export type Evidence = S["CapabilityClaim"]["evidence"];
export type TokenUsage = S["TokenUsage"];
export type Timings = S["Timings"];

/** Normalised streaming events (see docs/architecture/api.md). */
export type StreamEvent =
  | { event: "message.started"; data: S["MessageStarted"] }
  | { event: "message.delta"; data: S["MessageDelta"] }
  | { event: "usage.reported"; data: S["UsageReported"] }
  | { event: "message.completed"; data: S["MessageCompleted"] }
  | { event: "request.failed"; data: S["RequestFailed"] };

const STREAM_EVENTS = new Set([
  "message.started",
  "message.delta",
  "usage.reported",
  "message.completed",
  "request.failed",
]);

export class OmfApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;

  constructor(status: number, detail: ErrorDetail) {
    super(detail.message);
    this.name = "OmfApiError";
    this.status = status;
    this.code = detail.code;
    this.requestId = detail.request_id ?? null;
    this.retryable = detail.retryable ?? false;
    this.details = (detail.details ?? {}) as Record<string, unknown>;
  }
}

export interface OmfClientOptions {
  /** Base URL of the API, e.g. "http://localhost:8080". Empty string = same origin. */
  baseUrl?: string;
  /** Returns the bearer token for the platform API (never a provider credential). */
  token?: () => string | null | undefined;
  fetch?: typeof fetch;
}

interface Envelope<T> {
  data: T;
  meta: { request_id?: string | null };
}

export class OmfClient {
  private readonly baseUrl: string;
  private readonly token: () => string | null | undefined;
  private readonly fetchImpl: typeof fetch;

  constructor(options: OmfClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "").replace(/\/$/, "");
    this.token = options.token ?? (() => undefined);
    this.fetchImpl = options.fetch ?? ((...args) => globalThis.fetch(...args));
  }

  // ------------------------------------------------------------------ transport

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    const headers: Record<string, string> = { ...extra };
    const token = this.token();
    if (token) headers.Authorization = `Bearer ${token}`;
    return headers;
  }

  private async raw(
    method: string,
    path: string,
    body?: unknown,
    init: RequestInit = {},
  ): Promise<Response> {
    const isForm = typeof FormData !== "undefined" && body instanceof FormData;
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method,
      ...init,
      headers: this.headers(
        body !== undefined && !isForm ? { "Content-Type": "application/json" } : {},
      ),
      body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
    });
    if (!response.ok) throw await toError(response);
    return response;
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await this.raw(method, path, body);
    const envelope = (await response.json()) as Envelope<T>;
    return envelope.data;
  }

  // ------------------------------------------------------------------ system

  system = () => this.request<SystemInfo>("GET", "/api/v1/system");
  ready = async () => (await this.fetchImpl(`${this.baseUrl}/health/ready`)).json();
  settings = () =>
    this.request<{ runtime: Record<string, unknown>; read_only: Record<string, unknown> }>(
      "GET",
      "/api/v1/settings",
    );
  updateSettings = (changes: Record<string, unknown>) =>
    this.request<{ runtime: Record<string, unknown>; read_only: Record<string, unknown> }>(
      "PATCH",
      "/api/v1/settings",
      changes,
    );
  metrics = (window = 500, operation: "chat" | "embeddings" = "chat") =>
    this.request<MetricsSummary>(
      "GET",
      `/api/v1/metrics/summary?window=${window}&operation=${operation}`,
    );
  recentRequests = (limit = 50) =>
    this.request<InferenceRecord[]>("GET", `/api/v1/requests/recent?limit=${limit}`);
  audit = (limit = 100) => this.request<AuditEntry[]>("GET", `/api/v1/audit?limit=${limit}`);
  secrets = () => this.request<SecretReferenceInfo[]>("GET", "/api/v1/secrets");

  // ------------------------------------------------------------------ providers

  adapters = () => this.request<Record<string, unknown>[]>("GET", "/api/v1/adapters");
  providers = () => this.request<ProviderView[]>("GET", "/api/v1/providers");
  provider = (id: string) => this.request<ProviderView>("GET", `/api/v1/providers/${enc(id)}`);
  createProvider = (body: ProviderCreate) =>
    this.request<{ provider: ProviderView; warnings: string[] }>("POST", "/api/v1/providers", body);
  updateProvider = (id: string, body: ProviderUpdate) =>
    this.request<{ provider: ProviderView; warnings: string[] }>(
      "PATCH",
      `/api/v1/providers/${enc(id)}`,
      body,
    );
  deleteProvider = (id: string) =>
    this.request<{ ok: boolean }>("DELETE", `/api/v1/providers/${enc(id)}`);
  testProvider = (body: { provider_id?: string; definition?: ProviderCreate }) =>
    this.request<Record<string, unknown>>("POST", "/api/v1/providers/test", body);
  checkProviderHealth = (id: string) =>
    this.request<ProviderHealth>("POST", `/api/v1/providers/${enc(id)}/health`);

  // ------------------------------------------------------------------ models

  models = () => this.request<ModelDefinition[]>("GET", "/api/v1/models");
  model = (id: string) =>
    this.request<ModelDetail>("GET", `/api/v1/models/${id.split("/").map(enc).join("/")}`);
  registerModel = (body: ModelCreate) =>
    this.request<ModelDefinition>("POST", "/api/v1/models", body);
  deployments = (
    options: { providerId?: string; capability?: Capability; refresh?: boolean } = {},
  ) => {
    const q = new URLSearchParams();
    if (options.providerId) q.set("provider_id", options.providerId);
    if (options.capability) q.set("capability", options.capability);
    if (options.refresh) q.set("refresh", "true");
    return this.request<ModelDeployment[]>("GET", `/api/v1/deployments${q.size ? `?${q}` : ""}`);
  };
  verifyDeployment = (providerId: string, model: string) =>
    this.request<{
      deployment: ModelDeployment;
      checks: { capability: string; result: string; detail: string }[];
    }>("POST", "/api/v1/deployments/verify", { provider_id: providerId, model });

  // ------------------------------------------------------------------ chat

  chat = (body: ChatRequest) =>
    this.request<InferenceResponse>("POST", "/api/v1/chat/completions", body);

  /**
   * Stream a chat completion. Aborting `signal` closes the connection, which cancels
   * the request server-side; `cancel(requestId)` cancels it explicitly.
   */
  async *streamChat(body: ChatRequest, options: { signal?: AbortSignal; requestId?: string } = {}) {
    const response = await this.fetchImpl(`${this.baseUrl}/api/v1/chat/stream`, {
      method: "POST",
      headers: this.headers({
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...(options.requestId ? { "X-Request-ID": options.requestId } : {}),
      }),
      body: JSON.stringify(body),
      signal: options.signal ?? null,
    });
    if (!response.ok) throw await toError(response);
    if (!response.body) throw new Error("streaming not supported by this fetch implementation");
    for await (const raw of readSse(response.body)) {
      if (!STREAM_EVENTS.has(raw.event)) continue;
      yield { event: raw.event, data: JSON.parse(raw.data) } as StreamEvent;
    }
  }

  cancel = (requestId: string) =>
    this.request<{ request_id: string; cancelled: boolean }>(
      "POST",
      `/api/v1/chat/requests/${enc(requestId)}/cancel`,
    );

  // ------------------------------------------------------------------ conversations

  /** Embed one or more texts (routed to a model with the `embeddings` capability). */
  embeddings = (body: EmbeddingsRequest) =>
    this.request<EmbeddingResponse>("POST", "/api/v1/embeddings", body);

  conversations = () => this.request<Conversation[]>("GET", "/api/v1/conversations");
  conversation = (id: string) =>
    this.request<Conversation>("GET", `/api/v1/conversations/${enc(id)}`);
  createConversation = (title?: string) =>
    this.request<Conversation>("POST", "/api/v1/conversations", { title });
  deleteConversation = (id: string) =>
    this.request<{ ok: boolean }>("DELETE", `/api/v1/conversations/${enc(id)}`);

  // ------------------------------------------------------------------ infrastructure

  compute = (refresh = false) =>
    this.request<ComputeEnvironment[]>("GET", `/api/v1/compute${refresh ? "?refresh=true" : ""}`);
  /** Lifecycle operation (admin). The environment must allow it (`allow_lifecycle`). */
  computeOperation = (
    id: string,
    operation: ComputeLifecycleOperation,
    options: { wait?: boolean; timeoutS?: number } = {},
  ) => {
    const q = new URLSearchParams();
    if (options.wait) q.set("wait", "true");
    if (options.timeoutS !== undefined) q.set("timeout_s", String(options.timeoutS));
    return this.request<ComputeEnvironment>(
      "POST",
      `/api/v1/compute/${enc(id)}/${operation}${q.size ? `?${q}` : ""}`,
    );
  };
  storage = () => this.request<StorageStatus[]>("GET", "/api/v1/storage");
  artifacts = (storageId: string, prefix = "") =>
    this.request<StorageArtifact[]>(
      "GET",
      `/api/v1/storage/${enc(storageId)}/artifacts?prefix=${encodeURIComponent(prefix)}`,
    );
  uploadArtifact = (storageId: string, key: string, file: Blob, filename = "upload") => {
    const form = new FormData();
    form.set("key", key);
    form.set("file", file, filename);
    return this.request<StorageArtifact>(
      "POST",
      `/api/v1/storage/${enc(storageId)}/artifacts`,
      form,
    );
  };
  verifyArtifact = (storageId: string, key: string) =>
    this.request<{ ok: boolean; message: string | null }>(
      "POST",
      `/api/v1/storage/${enc(storageId)}/artifact/verify?key=${encodeURIComponent(key)}`,
    );

  // ------------------------------------------------------------------ benchmarks

  datasets = () => this.request<BenchmarkDataset[]>("GET", "/api/v1/benchmarks/datasets");
  benchmarks = () => this.request<BenchmarkRun[]>("GET", "/api/v1/benchmarks");
  benchmark = (id: string) => this.request<BenchmarkRun>("GET", `/api/v1/benchmarks/${enc(id)}`);
  startBenchmark = (body: BenchmarkRequest) =>
    this.request<BenchmarkRun>("POST", "/api/v1/benchmarks", body);
  exportBenchmark = async (id: string, format: "csv" | "json"): Promise<Blob> =>
    (await this.raw("GET", `/api/v1/benchmarks/${enc(id)}/export?format=${format}`)).blob();
}

function enc(value: string): string {
  return encodeURIComponent(value);
}

async function toError(response: Response): Promise<OmfApiError> {
  try {
    const body = (await response.json()) as { error?: ErrorDetail };
    if (body.error) return new OmfApiError(response.status, body.error);
  } catch {
    // not JSON
  }
  return new OmfApiError(response.status, {
    code: "http_error",
    message: `HTTP ${response.status}`,
    request_id: response.headers.get("x-request-id"),
    retryable: response.status >= 500,
    details: {},
  });
}
