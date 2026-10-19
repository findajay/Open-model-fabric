import { OmfApiError, OmfClient, type ProviderView } from "@omf/sdk";
import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

// Same-origin in dev (Vite proxies /api to the Open Model Fabric API).
const client = new OmfClient({ baseUrl: "" });

interface Turn {
  role: "user" | "assistant";
  text: string;
  meta?: string;
}

function App() {
  const [providers, setProviders] = useState<ProviderView[]>([]);
  const [providerId, setProviderId] = useState<string>("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const conversationId = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    client.providers().then((ps) => setProviders(ps.filter((p) => p.enabled)), console.error);
  }, []);

  async function send() {
    const text = input.trim();
    if (!text) return;
    setInput("");
    setBusy(true);
    setTurns((t) => [...t, { role: "user", text }, { role: "assistant", text: "" }]);
    const update = (patch: Partial<Turn>) =>
      setTurns((t) => t.map((turn, i) => (i === t.length - 1 ? { ...turn, ...patch } : turn)));
    abort.current = new AbortController();
    let reply = "";
    try {
      // Provider switching is just a different provider_id: no client changes needed.
      for await (const event of client.streamChat(
        {
          messages: [{ role: "user", content: text }],
          provider_id: providerId || null,
          conversation_id: conversationId.current,
          persist: conversationId.current === null,
        },
        { signal: abort.current.signal },
      )) {
        if (event.event === "message.started")
          conversationId.current = event.data.conversation_id ?? null;
        if (event.event === "message.delta") update({ text: (reply += event.data.delta) });
        if (event.event === "message.completed")
          update({ meta: `${Math.round(event.data.timings.total_ms)} ms` });
        if (event.event === "request.failed") update({ meta: `error: ${event.data.error.code}` });
      }
    } catch (error) {
      update({
        meta: error instanceof OmfApiError ? `${error.code}: ${error.message}` : String(error),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <h1>Open Model Fabric — minimal chat</h1>
      <label>
        Provider{" "}
        <select value={providerId} onChange={(e) => setProviderId(e.target.value)}>
          <option value="">routing default</option>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.id}
            </option>
          ))}
        </select>
      </label>
      {turns.map((t, i) => (
        <p key={i}>
          <strong>{t.role}:</strong> {t.text} {t.meta && <small>({t.meta})</small>}
        </p>
      ))}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <input value={input} onChange={(e) => setInput(e.target.value)} style={{ width: "70%" }} />
        {busy ? (
          <button type="button" onClick={() => abort.current?.abort()}>
            Stop
          </button>
        ) : (
          <button type="submit">Send</button>
        )}
      </form>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
