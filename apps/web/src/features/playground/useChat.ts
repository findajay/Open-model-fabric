import type {
  ChatRequest,
  Conversation,
  TokenUsage,
  Timings,
  ToolCall,
  ToolDefinition,
} from "@omf/sdk";
import { useCallback, useRef, useState } from "react";

import { describeError } from "@/components/ErrorAlert";
import { api } from "@/lib/api";

import type { Attachment } from "./Composer";

export interface UiMessage {
  id: string;
  role: "user" | "assistant" | "system" | "tool";
  text: string;
  images?: number;
  toolCalls?: ToolCall[];
  toolCallId?: string | null;
  providerId?: string;
  model?: string;
  usage?: TokenUsage | null;
  timings?: Timings;
  finishReason?: string | null;
  error?: { title: string; code?: string; requestId?: string | null };
  pending?: boolean;
  requestId?: string;
}

export interface ChatSettings {
  providerId: string | null;
  model: string | null;
  temperature: number;
  maxTokens: number;
  stream: boolean;
}

export interface ToolResult {
  callId: string;
  content: string;
}

type RequestMessage = ChatRequest["messages"][number];

function newRequestId(): string {
  return `ui_${crypto.randomUUID().replace(/-/g, "").slice(0, 20)}`;
}

export function fromConversation(conversation: Conversation): UiMessage[] {
  return conversation.messages.map((m) => {
    const meta = (m.metadata ?? {}) as Record<string, unknown>;
    const content = m.content;
    const text =
      typeof content === "string"
        ? content
        : content.map((p) => (p.type === "text" ? p.text : "")).join("");
    const images =
      typeof content === "string" ? 0 : content.filter((p) => p.type === "image").length;
    return {
      id: m.id ?? newRequestId(),
      role: m.role,
      text,
      images,
      toolCalls: m.tool_calls ?? [],
      toolCallId: m.tool_call_id,
      providerId: meta.provider_id as string | undefined,
      model: meta.model as string | undefined,
      usage: meta.usage as TokenUsage | undefined,
      timings: meta.timings as Timings | undefined,
      finishReason: meta.finish_reason as string | undefined,
      error:
        meta.status && meta.status !== "completed"
          ? { title: `request ${String(meta.status)}` }
          : undefined,
    };
  });
}

/** Tool calls of the last assistant message that no tool message has answered yet. */
export function pendingToolCalls(messages: UiMessage[]): ToolCall[] {
  const last = messages.at(-1);
  if (!last || last.role !== "assistant" || last.pending) return [];
  return last.toolCalls ?? [];
}

/** Chat state machine: streaming via SSE (or JSON), cancellation, persistence, tools. */
export function useChat(onConversation: (id: string) => void) {
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const active = useRef<{ requestId: string; controller: AbortController } | null>(null);
  // Tool results are sent with the same tool definitions as the turn that requested them.
  const lastTools = useRef<ToolDefinition[]>([]);

  const patch = (id: string, update: Partial<UiMessage>) =>
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...update } : m)));

  const load = useCallback((id: string | null, msgs: UiMessage[]) => {
    setConversationId(id);
    setMessages(msgs);
  }, []);

  const run = useCallback(
    async (
      outgoing: RequestMessage[],
      shown: UiMessage[],
      settings: ChatSettings,
      extra: { json: boolean; tools: ToolDefinition[] },
    ) => {
      const requestId = newRequestId();
      const assistantId = `a_${requestId}`;
      setMessages((prev) => [
        ...prev,
        ...shown,
        { id: assistantId, role: "assistant", text: "", pending: true, requestId },
      ]);
      setBusy(true);
      const body: ChatRequest = {
        messages: outgoing,
        provider_id: settings.providerId,
        model: settings.model,
        temperature: settings.temperature,
        max_tokens: settings.maxTokens,
        response_format: extra.json ? "json" : "text",
        conversation_id: conversationId,
        persist: conversationId === null,
        ...(extra.tools.length ? { tools: extra.tools } : {}),
      };
      const controller = new AbortController();
      active.current = { requestId, controller };
      try {
        if (!settings.stream) {
          const response = await api.chat(body);
          patch(assistantId, {
            text: typeof response.message.content === "string" ? response.message.content : "",
            toolCalls: response.message.tool_calls ?? [],
            pending: false,
            providerId: response.provider_id,
            model: response.model,
            usage: response.usage,
            timings: response.timings,
            finishReason: response.finish_reason,
          });
          if (response.conversation_id && !conversationId) {
            setConversationId(response.conversation_id);
            onConversation(response.conversation_id);
          }
          return;
        }
        let text = "";
        for await (const event of api.streamChat(body, { signal: controller.signal, requestId })) {
          switch (event.event) {
            case "message.started":
              patch(assistantId, { providerId: event.data.provider_id, model: event.data.model });
              if (event.data.conversation_id && !conversationId) {
                setConversationId(event.data.conversation_id);
                onConversation(event.data.conversation_id);
              }
              break;
            case "message.delta":
              text += event.data.delta;
              patch(assistantId, { text });
              break;
            case "usage.reported":
              patch(assistantId, { usage: event.data.usage });
              break;
            case "message.completed":
              patch(assistantId, {
                pending: false,
                timings: event.data.timings,
                finishReason: event.data.finish_reason,
                toolCalls: event.data.tool_calls ?? [],
              });
              break;
            case "request.failed":
              patch(assistantId, {
                pending: false,
                error: {
                  title: event.data.error.message,
                  code: event.data.error.code,
                  requestId: event.data.request_id,
                },
              });
              break;
          }
        }
      } catch (error) {
        if (controller.signal.aborted) {
          patch(assistantId, {
            pending: false,
            error: { title: "Stopped", code: "request_cancelled", requestId },
          });
        } else {
          patch(assistantId, { pending: false, error: describeError(error) });
        }
      } finally {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId && m.pending ? { ...m, pending: false } : m)),
        );
        active.current = null;
        setBusy(false);
      }
    },
    [conversationId, onConversation],
  );

  const send = useCallback(
    async (
      input: { text: string; images: Attachment[]; json: boolean; tools?: ToolDefinition[] },
      settings: ChatSettings,
    ) => {
      const content: RequestMessage["content"] = input.images.length
        ? [
            ...(input.text ? [{ type: "text" as const, text: input.text }] : []),
            ...input.images.map((img) => ({
              type: "image" as const,
              media_type: img.mediaType,
              data_base64: img.dataBase64,
            })),
          ]
        : input.text;
      lastTools.current = input.tools ?? [];
      const shown: UiMessage = {
        id: `u_${newRequestId()}`,
        role: "user",
        text: input.text,
        images: input.images.length,
      };
      await run([{ role: "user", content }], [shown], settings, {
        json: input.json,
        tools: lastTools.current,
      });
    },
    [run],
  );

  /** Second half of the loop: the user ran the tools and returns their results. */
  const sendToolResults = useCallback(
    async (results: ToolResult[], settings: ChatSettings) => {
      const outgoing: RequestMessage[] = results.map((r) => ({
        role: "tool",
        tool_call_id: r.callId,
        content: r.content,
      }));
      const shown: UiMessage[] = results.map((r) => ({
        id: `t_${newRequestId()}`,
        role: "tool",
        text: r.content,
        toolCallId: r.callId,
      }));
      await run(outgoing, shown, settings, { json: false, tools: lastTools.current });
    },
    [run],
  );

  const cancel = useCallback(async () => {
    const current = active.current;
    if (!current) return;
    try {
      await api.cancel(current.requestId); // server-side cancel (closes upstream generation)
    } finally {
      current.controller.abort();
    }
  }, []);

  return { messages, conversationId, busy, send, sendToolResults, cancel, load };
}
