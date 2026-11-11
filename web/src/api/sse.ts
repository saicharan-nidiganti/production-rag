// A parser for Server-Sent Events read from a fetch body. EventSource can't send a POST,
// and the question goes in the body, so the stream is read by hand.

import type { StreamEvent } from "./types";

export interface RawEvent {
  event: string;
  data: string;
}

/**
 * Splits a stream of text chunks into events. A chunk can end anywhere, even inside a
 * multi-byte character or between the two newlines that end an event, so text is buffered
 * until a blank line arrives.
 */
export async function* parseEvents(chunks: AsyncIterable<Uint8Array>): AsyncGenerator<RawEvent> {
  const decoder = new TextDecoder();
  let buffer = "";
  for await (const chunk of chunks) {
    buffer += decoder.decode(chunk, { stream: true }).replace(/\r\n/g, "\n");
    let end: number;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const event = parseBlock(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
      if (event) yield event;
    }
  }
  const last = parseBlock(buffer + decoder.decode());
  if (last) yield last;
}

function parseBlock(block: string): RawEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // a comment, used as a keep-alive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length ? { event, data: data.join("\n") } : null;
}

/** A raw event as one of the API's stream events; null for one this client doesn't know. */
export function toStreamEvent(raw: RawEvent): StreamEvent | null {
  const body = JSON.parse(raw.data);
  switch (raw.event) {
    case "retrieval":
      return { type: "retrieval", ...body };
    case "text":
      return { type: "text", text: body.text };
    case "citation":
      return { type: "citation", index: body.index, citation: body.citation };
    case "done":
      return { type: "done", response: body };
    case "error":
      return { type: "error", error: body.error };
    default:
      return null; // a newer server may add events; ignoring them keeps old clients working
  }
}

export async function* readBody(body: ReadableStream<Uint8Array>): AsyncGenerator<Uint8Array> {
  const reader = body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      yield value;
    }
  } finally {
    // Runs when the consumer stops early, too: cancelling tells the server to stop generating.
    await reader.cancel().catch(() => undefined);
  }
}
