import { describe, expect, it } from "vitest";
import { parseEvents, toStreamEvent, type RawEvent } from "./sse";

const encoder = new TextEncoder();

async function* chunksOf(bytes: Uint8Array, size: number): AsyncGenerator<Uint8Array> {
  for (let i = 0; i < bytes.length; i += size) yield bytes.slice(i, i + size);
}

async function collect(stream: AsyncIterable<Uint8Array>): Promise<RawEvent[]> {
  const events: RawEvent[] = [];
  for await (const event of parseEvents(stream)) events.push(event);
  return events;
}

const STREAM =
  'event: retrieval\ndata: {"retrieved":[],"abstained_before_generation":false}\n\n' +
  'event: text\ndata: {"text":"The canary runs for 30 minutes — café."}\n\n' +
  ": keep-alive\n\n" +
  'event: done\ndata: {"status":"answered"}\n\n';

describe("parseEvents", () => {
  it("reads each event with its name and data", async () => {
    const events = await collect(chunksOf(encoder.encode(STREAM), 1 << 20));
    expect(events.map((e) => e.event)).toEqual(["retrieval", "text", "done"]);
    expect(JSON.parse(events[1]!.data).text).toBe("The canary runs for 30 minutes — café.");
  });

  it.each([1, 2, 3, 7, 16])("gives the same events when chunks are %i bytes", async (size) => {
    // Small chunks split events, the blank line between them, and multi-byte characters.
    const whole = await collect(chunksOf(encoder.encode(STREAM), 1 << 20));
    expect(await collect(chunksOf(encoder.encode(STREAM), size))).toEqual(whole);
  });

  it("accepts CRLF line endings and a last event without a blank line", async () => {
    const text = "event: text\r\ndata: {}\r\n\r\nevent: done\r\ndata: {}";
    const events = await collect(chunksOf(encoder.encode(text), 5));
    expect(events.map((e) => e.event)).toEqual(["text", "done"]);
  });

  it("joins data lines and defaults the name to message", async () => {
    const events = await collect(chunksOf(encoder.encode("data: a\ndata: b\n\n"), 4));
    expect(events).toEqual([{ event: "message", data: "a\nb" }]);
  });

  it("yields nothing for an empty stream", async () => {
    expect(await collect(chunksOf(new Uint8Array(), 1))).toEqual([]);
  });
});

describe("toStreamEvent", () => {
  it("maps each API event", () => {
    expect(toStreamEvent({ event: "text", data: '{"text":"hi"}' })).toEqual({
      type: "text",
      text: "hi",
    });
    expect(
      toStreamEvent({ event: "citation", data: '{"index":0,"citation":{"chunk_id":3}}' }),
    ).toEqual({ type: "citation", index: 0, citation: { chunk_id: 3 } });
    expect(toStreamEvent({ event: "done", data: '{"status":"answered"}' })).toEqual({
      type: "done",
      response: { status: "answered" },
    });
    expect(
      toStreamEvent({ event: "error", data: '{"error":{"code":"x","message":"m"}}' }),
    ).toEqual({ type: "error", error: { code: "x", message: "m" } });
  });

  it("ignores events it doesn't know", () => {
    expect(toStreamEvent({ event: "progress", data: "{}" })).toBeNull();
  });
});
