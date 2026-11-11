import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Citation, Document, QueryResponse, SearchHit } from "./api/types";
import { App } from "./App";
import { apiKey } from "./api/client";

// --- A small fake of the API, by method and path ---

type Handler = (request: { url: URL; init: RequestInit }) => Response | Promise<Response>;

const json = (body: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });

const problem = (status: number, code: string, message: string): Response =>
  json({ error: { code, message, request_id: "req-123" } }, status);

const NO_ACCOUNTS = { accounts_enabled: false, user: null, csrf_token: null };
const ADA = { id: "u1", email: "ada@example.com", role: "admin" };
const SIGNED_IN = { accounts_enabled: true, user: ADA, csrf_token: "csrf-abc" };
const SIGNED_OUT = { accounts_enabled: true, user: null, csrf_token: null };

function serve(routes: Record<string, Handler>) {
  const calls: { key: string; url: URL; init: RequestInit }[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://localhost");
    const key = `${init.method ?? "GET"} ${url.pathname}`;
    calls.push({ key, url, init });
    const handler = routes[key];
    if (!handler && key === "GET /v1/auth/status") {
      // A server without accounts: the status route needs the key exactly as the others do.
      const probe = await routes["GET /v1/documents"]?.({ url, init });
      return probe?.status === 401 ? probe : json(NO_ACCOUNTS);
    }
    if (!handler) throw new Error(`no fake for ${key}`);
    return handler({ url, init });
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

function doc(overrides: Partial<Document> = {}): Document {
  return {
    id: "d1",
    source_name: "runbook.md",
    title: "Deployment Runbook",
    content_type: "text/markdown",
    size_bytes: 1908,
    num_chunks: 5,
    content_hash: "h",
    metadata: {},
    status: "ready",
    error: null,
    attempts: 1,
    created_at: "2026-11-10T09:00:00Z",
    updated_at: "2026-11-10T09:00:01Z",
    replaces: null,
    ...overrides,
  };
}

const list = (...items: Document[]) => json({ items, total: items.length, limit: 100, offset: 0 });

const PASSAGE = {
  chunk_id: 7,
  chunk_key: "k",
  document_id: "d1",
  document_title: "Deployment Runbook",
  source_name: "runbook.md",
  heading: "Deployment Runbook > Canary releases",
  page: null,
  start_char: 120,
  end_char: 190,
};
const HIT: SearchHit = {
  ...PASSAGE,
  // Hard-wrapped, as source files are: the wrap falls inside the cited sentence.
  text: "Before a rollout, a canary is deployed. The canary receives 5% of\ntraffic for 30 minutes. Then it continues.",
  score: 0.9,
  vector_score: 0.8,
  lexical_score: 3,
  rerank_score: null,
};
const CITATION: Citation = { ...PASSAGE, cited_text: "The canary receives 5% of\ntraffic for 30 minutes." };
const ANSWER: QueryResponse = {
  status: "answered",
  answer: "It runs for 30 minutes.",
  segments: [{ text: "It runs for 30 minutes.", citations: [0] }],
  citations: [CITATION],
  retrieved: [HIT],
  model: "extractive-v1",
  stop_reason: null,
  usage: null,
  timings_ms: { total: 5 },
};

function stream(...events: [string, unknown][]): Response {
  const body = events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join("");
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

const answered = () =>
  stream(
    ["retrieval", { retrieved: [HIT], abstained_before_generation: false }],
    ["text", { text: "It runs for " }],
    ["citation", { index: 0, citation: CITATION }],
    ["text", { text: "30 minutes." }],
    ["done", ANSWER],
  );

// --- Access ---

describe("access", () => {
  it("goes straight in when the server needs no key", async () => {
    serve({ "GET /v1/documents": () => list() });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Ask" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("asks for a key, rejects a wrong one, and remembers a right one for the tab", async () => {
    const user = userEvent.setup();
    const calls = serve({
      "GET /v1/documents": ({ init }) =>
        new Headers(init.headers).get("Authorization") === "Bearer right-key-0123456789"
          ? list()
          : problem(401, "unauthorized", "missing or invalid API key"),
    });
    render(<App />);
    const field = await screen.findByLabelText("API key");
    expect(field).toHaveAttribute("type", "password");

    await user.type(field, "wrong-key");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("That key wasn't accepted")).toBeVisible();
    expect(apiKey.get()).toBeNull();

    await user.clear(field);
    await user.type(field, "right-key-0123456789");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { name: "Ask" })).toBeVisible();
    expect(apiKey.get()).toBe("right-key-0123456789");
    expect(calls.every((call) => !call.url.href.includes("right-key"))).toBe(true); // never in a URL

    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByLabelText("API key")).toBeVisible();
    expect(apiKey.get()).toBeNull();
  });

  it("says so when the server can't be reached, and tries again", async () => {
    const user = userEvent.setup();
    let up = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        if (!up) throw new TypeError("Failed to fetch");
        return list();
      }),
    );
    render(<App />);
    expect(await screen.findByText("Can't reach the server")).toBeVisible();
    up = true;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Ask" })).toBeVisible();
  });
});

describe("accounts", () => {
  it("signs in with an email address and password, and shows who is signed in", async () => {
    const user = userEvent.setup();
    let signedIn = false;
    const calls = serve({
      "GET /v1/auth/status": () => json(signedIn ? SIGNED_IN : SIGNED_OUT),
      "POST /v1/auth/login": ({ init }) => {
        const body = JSON.parse(init.body as string);
        if (body.password !== "correct horse battery") {
          return problem(401, "unauthorized", "wrong email address or password");
        }
        signedIn = true;
        return json(SIGNED_IN);
      },
      "GET /v1/documents": () => list(),
      "POST /v1/documents": () => json({ document: doc({ status: "queued" }), created: true }, 202),
      "POST /v1/auth/logout": () => new Response(null, { status: 204 }),
    });
    render(<App />);
    const email = await screen.findByLabelText("Email address");
    expect(email).toHaveAttribute("autocomplete", "username");
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "current-password");

    await user.type(email, "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "a guess");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Wrong email address or password")).toBeVisible();

    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("ada@example.com")).toBeVisible();
    expect(apiKey.get()).toBeNull(); // the session is a cookie the page can't read
    expect(sessionStorage.length).toBe(0);

    // Changes carry the CSRF token; reads don't need it.
    await user.click(screen.getByRole("button", { name: "Documents" }));
    await user.upload(await screen.findByLabelText("Choose files to upload"), new File(["# x"], "x.md"));
    await waitFor(() => {
      const upload = calls.find((call) => call.key === "POST /v1/documents");
      expect(new Headers(upload?.init.headers).get("X-CSRF-Token")).toBe("csrf-abc");
    });
    const read = calls.find((call) => call.key === "GET /v1/documents");
    expect(new Headers(read?.init.headers).has("X-CSRF-Token")).toBe(false);

    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByLabelText("Email address")).toBeVisible();
    const logout = calls.find((call) => call.key === "POST /v1/auth/logout");
    expect(new Headers(logout?.init.headers).get("X-CSRF-Token")).toBe("csrf-abc");
  });

  it("goes straight in when the browser is already signed in", async () => {
    serve({ "GET /v1/auth/status": () => json(SIGNED_IN), "GET /v1/documents": () => list() });
    render(<App />);
    expect(await screen.findByText("ada@example.com")).toBeVisible();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });

  it("shows a lockout as it is, with when to try again", async () => {
    const user = userEvent.setup();
    serve({
      "GET /v1/auth/status": () => json(SIGNED_OUT),
      "POST /v1/auth/login": () =>
        json({ error: { code: "rate_limited", message: "too many failed attempts" } }, 429, {
          "Retry-After": "40",
        }),
    });
    render(<App />);
    await user.type(await screen.findByLabelText("Email address"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "whatever it is");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Too many requests")).toBeVisible();
    expect(screen.getByText("Try again in 40 seconds.")).toBeVisible();
  });

  it("returns to sign-in when the session ends", async () => {
    const user = userEvent.setup();
    let alive = true;
    serve({
      "GET /v1/auth/status": () => json(SIGNED_IN),
      "GET /v1/documents": () => (alive ? list(doc()) : problem(401, "unauthorized", "sign in")),
      "DELETE /v1/documents/d1": () => problem(401, "unauthorized", "sign in"),
      "POST /v1/auth/logout": () => problem(401, "unauthorized", "sign in"),
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Documents" }));
    await user.click(await screen.findByRole("button", { name: "Delete runbook.md" }));
    alive = false;
    await user.click(screen.getByRole("button", { name: "Delete runbook.md" }));
    expect(await screen.findByLabelText("Email address")).toBeVisible();
  });
});

// --- Asking ---

describe("asking", () => {
  it("streams an answer, numbers its sources and shows the cited passage in context", async () => {
    const user = userEvent.setup();
    const calls = serve({ "GET /v1/documents": () => list(), "POST /v1/query/stream": answered });
    render(<App />);
    await user.type(await screen.findByLabelText("Your question"), "How long does the canary run?{Enter}");

    expect(await screen.findByText("It runs for 30 minutes.")).toBeVisible();
    const sent = calls.find((call) => call.key === "POST /v1/query/stream");
    expect(JSON.parse(sent?.init.body as string)).toEqual({ question: "How long does the canary run?" });
    expect(screen.getByLabelText("Your question")).toHaveValue("");

    // The marker in the text and the entry in the list both open the passage.
    await user.click(screen.getByRole("button", { name: "Source 1: Deployment Runbook" }));
    const panel = screen.getByRole("complementary", { name: "Source passage" });
    expect(panel).toHaveFocus();
    expect(within(panel).getByText("Deployment Runbook > Canary releases")).toBeVisible();
    // In the list, the document's title isn't repeated in front of the section.
    expect(screen.getByRole("button", { name: /^.1. Deployment Runbook ?· Canary releases$/ })).toBeVisible();
    expect(within(panel).getByText("runbook.md")).toBeVisible();
    const cited = within(panel).getByText("The canary receives 5% of traffic for 30 minutes.");
    expect(cited.tagName).toBe("MARK"); // highlighted inside the surrounding passage
    expect(panel).toHaveTextContent("Before a rollout, a canary is deployed.");

    expect(screen.getByText("1 passages were considered")).toBeInTheDocument();
  });

  it("says when the documents don't hold the answer", async () => {
    const user = userEvent.setup();
    const none: QueryResponse = {
      ...ANSWER,
      status: "insufficient_evidence",
      answer: "The documents don't contain enough information to answer.",
      segments: [{ text: "The documents don't contain enough information to answer.", citations: [] }],
      citations: [],
      retrieved: [],
    };
    serve({
      "GET /v1/documents": () => list(),
      "POST /v1/query/stream": () =>
        stream(["retrieval", { retrieved: [], abstained_before_generation: true }], ["done", none]),
    });
    render(<App />);
    await user.type(await screen.findByLabelText("Your question"), "What is the wifi password?{Enter}");
    expect(await screen.findByText("Not enough evidence in your documents")).toBeVisible();
    expect(screen.queryByText("Sources")).not.toBeInTheDocument();
  });

  it("shows a failure that arrives inside the stream, keeping the text so far", async () => {
    const user = userEvent.setup();
    serve({
      "GET /v1/documents": () => list(),
      "POST /v1/query/stream": () =>
        stream(
          ["retrieval", { retrieved: [HIT], abstained_before_generation: false }],
          ["text", { text: "It runs" }],
          ["error", { error: { code: "generation_unavailable", message: "Claude API error (overloaded_error during the stream)", request_id: "req-9" } }],
        ),
    });
    render(<App />);
    await user.type(await screen.findByLabelText("Your question"), "How long?{Enter}");
    expect(await screen.findByText(/overloaded_error/)).toBeVisible();
    expect(screen.getByText("It runs")).toBeVisible();
    expect(screen.getByText("req-9")).toBeVisible();
  });

  it("shows a rate limit with when to try again", async () => {
    const user = userEvent.setup();
    serve({
      "GET /v1/documents": () => list(),
      "POST /v1/query/stream": () =>
        json({ error: { code: "rate_limited", message: "too many query requests; retry after 12 s" } }, 429, { "Retry-After": "12" }),
    });
    render(<App />);
    await user.type(await screen.findByLabelText("Your question"), "How long?{Enter}");
    expect(await screen.findByText("Too many requests")).toBeVisible();
    expect(screen.getByText("Try again in 12 seconds.")).toBeVisible();
  });

  it("doesn't send an empty question, and Shift+Enter adds a line", async () => {
    const user = userEvent.setup();
    const calls = serve({ "GET /v1/documents": () => list() });
    render(<App />);
    const field = await screen.findByLabelText("Your question");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await user.type(field, "   {Enter}");
    await user.clear(field);
    await user.type(field, "one{Shift>}{Enter}{/Shift}two");
    expect(field).toHaveValue("one\ntwo");
    expect(calls.some((call) => call.key === "POST /v1/query/stream")).toBe(false);
  });
});

// --- Documents ---

async function openDocuments(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Documents" }));
}

describe("documents", () => {
  it("shows an empty library, then an upload that becomes ready", async () => {
    const user = userEvent.setup();
    // Empty until the upload; then queued for one listing, and ready after that.
    let listedSinceUpload = -1;
    const calls = serve({
      "GET /v1/documents": () => {
        if (listedSinceUpload < 0) return list();
        return listedSinceUpload++ === 0
          ? list(doc({ status: "queued", num_chunks: 0, title: "runbook" }))
          : list(doc());
      },
      "POST /v1/documents": () => {
        listedSinceUpload = 0;
        return json({ document: doc({ status: "queued" }), created: true }, 202);
      },
    });
    render(<App />);
    await openDocuments(user);
    expect(await screen.findByText("No documents yet")).toBeVisible();

    const file = new File(["# Runbook"], "runbook.md", { type: "text/markdown" });
    await user.upload(screen.getByLabelText("Choose files to upload"), file);
    expect(await screen.findByText("Queued")).toBeVisible();
    // It keeps checking while something is being processed, then stops.
    expect(await screen.findByText("Ready", {}, { timeout: 4000 })).toBeVisible();
    expect(screen.getByText("5 passages")).toBeVisible();
    const upload = calls.find((call) => call.key === "POST /v1/documents");
    expect((upload?.init.body as FormData).get("file")).toBe(file);
  });

  it("explains a failed document and retries it", async () => {
    const user = userEvent.setup();
    const failed = doc({
      status: "failed",
      num_chunks: 0,
      error: { code: "extraction_failed", message: "document contains no extractable text" },
    });
    const calls = serve({
      "GET /v1/documents": () => list(failed),
      "POST /v1/documents/d1/retry": () => json(doc({ status: "queued" }), 202),
    });
    render(<App />);
    await openDocuments(user);
    expect(await screen.findByText("document contains no extractable text")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry runbook.md" }));
    await waitFor(() => expect(calls.some((c) => c.key === "POST /v1/documents/d1/retry")).toBe(true));
  });

  it("deletes only after a second, explicit step", async () => {
    const user = userEvent.setup();
    const calls = serve({
      "GET /v1/documents": () => list(doc()),
      "DELETE /v1/documents/d1": () => new Response(null, { status: 204 }),
    });
    render(<App />);
    await openDocuments(user);
    await user.click(await screen.findByRole("button", { name: "Delete runbook.md" }));
    expect(calls.some((c) => c.key.startsWith("DELETE"))).toBe(false);

    await user.click(screen.getByRole("button", { name: "Keep" }));
    expect(screen.queryByRole("button", { name: "Keep" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete runbook.md" }));
    await user.click(screen.getByRole("button", { name: "Delete runbook.md" }));
    await waitFor(() => expect(calls.some((c) => c.key === "DELETE /v1/documents/d1")).toBe(true));
  });

  it("uploads a new version as a replacement", async () => {
    const user = userEvent.setup();
    const calls = serve({
      "GET /v1/documents": () => list(doc()),
      "POST /v1/documents": () => json({ document: doc({ id: "d2", status: "queued", replaces: "d1" }), created: true }, 202),
    });
    render(<App />);
    await openDocuments(user);
    await screen.findByText("Deployment Runbook");
    await user.click(screen.getByRole("button", { name: "Replace runbook.md with a new version" }));
    await user.upload(screen.getByLabelText("Choose the new version"), new File(["v2"], "runbook.md"));
    await waitFor(() => {
      const upload = calls.find((c) => c.key === "POST /v1/documents");
      expect(upload?.url.searchParams.get("replaces")).toBe("d1");
    });
  });

  it("names the file an upload error belongs to", async () => {
    const user = userEvent.setup({ applyAccept: false }); // as a drop would, past the picker
    serve({
      "GET /v1/documents": () => list(),
      "POST /v1/documents": () => problem(415, "unsupported_document_type", "unsupported file type .exe"),
    });
    render(<App />);
    await openDocuments(user);
    await user.upload(
      await screen.findByLabelText("Choose files to upload"),
      new File(["x"], "setup.exe"),
    );
    expect(await screen.findByText("setup.exe: unsupported file type .exe")).toBeVisible();
    expect(screen.getByText("req-123")).toBeVisible();
  });

  it("returns to the key screen when the key stops working", async () => {
    const user = userEvent.setup();
    apiKey.set("k".repeat(24));
    let valid = true;
    serve({
      "GET /v1/documents": () => (valid ? list(doc()) : problem(401, "unauthorized", "missing or invalid API key")),
      "DELETE /v1/documents/d1": () => problem(401, "unauthorized", "missing or invalid API key"),
    });
    render(<App />);
    await openDocuments(user);
    await user.click(await screen.findByRole("button", { name: "Delete runbook.md" }));
    valid = false;
    await user.click(screen.getByRole("button", { name: "Delete runbook.md" }));
    expect(await screen.findByLabelText("API key")).toBeVisible();
  });
});
