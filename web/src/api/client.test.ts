import { describe, expect, it, vi } from "vitest";
import { api, ApiError, apiKey } from "./client";

function respond(body: unknown, init: ResponseInit = {}): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function mockFetch(...responses: Response[]) {
  const mock = vi.fn<typeof fetch>();
  for (const response of responses) mock.mockResolvedValueOnce(response);
  vi.stubGlobal("fetch", mock);
  return mock;
}

const headersOf = (mock: ReturnType<typeof mockFetch>, call = 0): Headers =>
  new Headers(mock.mock.calls[call]![1]?.headers);

describe("api", () => {
  it("sends the stored key as a bearer token, and none when there is no key", async () => {
    const fetch = mockFetch(respond({ items: [] }), respond({ items: [] }));
    await api.listDocuments();
    expect(headersOf(fetch).has("Authorization")).toBe(false);

    apiKey.set("k".repeat(24));
    await api.listDocuments("failed");
    expect(headersOf(fetch, 1).get("Authorization")).toBe(`Bearer ${"k".repeat(24)}`);
    expect(fetch.mock.calls[1]![0]).toBe("/v1/documents?limit=100&status=failed");
  });

  it("turns an error body into an ApiError with its code and request id", async () => {
    mockFetch(
      respond(
        { error: { code: "rate_limited", message: "too many", request_id: "r1" } },
        { status: 429, headers: { "Retry-After": "7" } },
      ),
    );
    const error = await api.listDocuments().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 429,
      code: "rate_limited",
      message: "too many",
      requestId: "r1",
      retryAfter: 7,
    });
  });

  it("survives an error that isn't JSON", async () => {
    mockFetch(new Response("<html>Bad Gateway</html>", { status: 502 }));
    await expect(api.listDocuments()).rejects.toMatchObject({ status: 502, code: "http_502" });
  });

  it("reports a network failure in plain words", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(api.listDocuments()).rejects.toMatchObject({
      status: 0,
      code: "network_error",
      message: "Couldn't reach the server.",
    });
  });

  it("checks access: true when accepted, false on 401, and rethrows anything else", async () => {
    mockFetch(
      respond({ items: [] }),
      respond({ error: { code: "unauthorized", message: "no" } }, { status: 401 }),
      respond({ error: { code: "internal_error", message: "boom" } }, { status: 500 }),
    );
    expect(await api.checkAccess()).toBe(true);
    expect(await api.checkAccess()).toBe(false);
    await expect(api.checkAccess()).rejects.toMatchObject({ status: 500 });
  });

  it("uploads as multipart, naming the document it replaces", async () => {
    const fetch = mockFetch(respond({ document: { id: "new" }, created: true }, { status: 202 }));
    const file = new File(["# hi"], "notes.md", { type: "text/markdown" });
    await api.upload(file, "old id");
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe("/v1/documents?replaces=old%20id");
    expect(init?.method).toBe("POST");
    expect((init?.body as FormData).get("file")).toBe(file);
  });

  it("streams an answer as events, in order", async () => {
    const body =
      'event: retrieval\ndata: {"retrieved":[],"abstained_before_generation":false}\n\n' +
      'event: text\ndata: {"text":"Thirty "}\n\nevent: text\ndata: {"text":"minutes."}\n\n' +
      'event: done\ndata: {"status":"answered","answer":"Thirty minutes."}\n\n';
    const fetch = mockFetch(new Response(body, { headers: { "Content-Type": "text/event-stream" } }));
    const types: string[] = [];
    let text = "";
    for await (const event of api.ask("How long?")) {
      types.push(event.type);
      if (event.type === "text") text += event.text;
    }
    expect(types).toEqual(["retrieval", "text", "text", "done"]);
    expect(text).toBe("Thirty minutes.");
    expect(JSON.parse(fetch.mock.calls[0]![1]!.body as string)).toEqual({ question: "How long?" });
  });

  it("raises an ordinary error when the stream never starts", async () => {
    mockFetch(respond({ error: { code: "validation_error", message: "bad" } }, { status: 422 }));
    const run = async () => {
      for await (const event of api.ask("")) void event;
    };
    await expect(run()).rejects.toMatchObject({ status: 422, code: "validation_error" });
  });
});
