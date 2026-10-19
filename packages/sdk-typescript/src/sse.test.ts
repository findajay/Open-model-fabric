import { describe, expect, it } from "vitest";

import { OmfApiError, OmfClient } from "./client.js";
import { SseParser } from "./sse.js";

describe("SseParser", () => {
  it("parses events split across arbitrary chunk boundaries", () => {
    const stream =
      'id: 0\nevent: message.started\ndata: {"a":1}\n\n: keep-alive\n\nevent: message.delta\ndata: {"delta":"he"}\n\n';
    for (const size of [1, 3, 7, stream.length]) {
      const parser = new SseParser();
      const events = [];
      for (let i = 0; i < stream.length; i += size)
        events.push(...parser.push(stream.slice(i, i + size)));
      expect(events.map((e) => e.event)).toEqual(["message.started", "message.delta"]);
      expect(events[0]?.id).toBe("0");
      expect(JSON.parse(events[1]!.data)).toEqual({ delta: "he" });
    }
  });

  it("supports CRLF and multi-line data", () => {
    const events = new SseParser().push("event: x\r\ndata: line1\r\ndata: line2\r\n\r\n");
    expect(events).toEqual([{ event: "x", data: "line1\nline2" }]);
  });
});

describe("OmfClient", () => {
  it("streams normalised events from a fetch body and sends the bearer token", async () => {
    const body =
      'event: message.started\ndata: {"request_id":"r"}\n\nevent: message.delta\ndata: {"request_id":"r","index":0,"delta":"Hi"}\n\n' +
      'event: message.completed\ndata: {"request_id":"r","message_id":"m"}\n\n';
    let auth: string | null = null;
    const client = new OmfClient({
      token: () => "tok",
      fetch: async (_url, init) => {
        auth = new Headers(init?.headers).get("authorization");
        return new Response(body, { headers: { "content-type": "text/event-stream" } });
      },
    });
    const events = [];
    for await (const e of client.streamChat({ messages: [{ role: "user", content: "x" }] }))
      events.push(e.event);
    expect(events).toEqual(["message.started", "message.delta", "message.completed"]);
    expect(auth).toBe("Bearer tok");
  });

  it("turns error envelopes into OmfApiError", async () => {
    const client = new OmfClient({
      fetch: async () =>
        new Response(
          JSON.stringify({
            error: {
              code: "provider_unavailable",
              message: "down",
              request_id: "req_1",
              retryable: true,
              details: {},
            },
          }),
          { status: 503 },
        ),
    });
    await expect(client.providers()).rejects.toMatchObject({
      status: 503,
      code: "provider_unavailable",
      requestId: "req_1",
      retryable: true,
    } satisfies Partial<OmfApiError>);
  });
});
