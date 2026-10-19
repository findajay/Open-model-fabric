/**
 * Minimal, spec-compliant Server-Sent Events parser for `fetch` response bodies.
 *
 * `EventSource` cannot send POST bodies or Authorization headers, so the SDK streams
 * with `fetch` and parses the `text/event-stream` format itself.
 */

export interface RawSseEvent {
  event: string;
  data: string;
  id?: string;
}

export class SseParser {
  private buffer = "";
  private event = "";
  private data: string[] = [];
  private id: string | undefined;

  /** Feed a decoded text chunk; returns the events completed by this chunk. */
  push(chunk: string): RawSseEvent[] {
    this.buffer += chunk;
    const out: RawSseEvent[] = [];
    let newline: number;
    while ((newline = this.buffer.search(/\r\n|\r|\n/)) >= 0) {
      const line = this.buffer.slice(0, newline);
      const sepLength = this.buffer.startsWith("\r\n", newline) ? 2 : 1;
      this.buffer = this.buffer.slice(newline + sepLength);
      const event = this.line(line);
      if (event) out.push(event);
    }
    return out;
  }

  private line(line: string): RawSseEvent | undefined {
    if (line === "") {
      if (this.data.length === 0) {
        this.event = "";
        return undefined;
      }
      const event: RawSseEvent = {
        event: this.event || "message",
        data: this.data.join("\n"),
        ...(this.id !== undefined ? { id: this.id } : {}),
      };
      this.event = "";
      this.data = [];
      return event;
    }
    if (line.startsWith(":")) return undefined; // comment / keep-alive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data.push(value);
    else if (field === "id") this.id = value;
    return undefined;
  }
}

export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<RawSseEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      yield* parser.push(decoder.decode(value, { stream: true }));
    }
    yield* parser.push(decoder.decode() + "\n\n");
  } finally {
    reader.releaseLock();
  }
}
