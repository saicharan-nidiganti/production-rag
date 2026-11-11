// The HTTP client. Every call goes to the page's own origin: the backend serves this app,
// or the dev server forwards to it.

import { parseEvents, readBody, toStreamEvent } from "./sse";
import type {
  Document,
  DocumentList,
  DocumentStatus,
  ErrorBody,
  IngestResponse,
  StreamEvent,
} from "./types";

const KEY = "rag.apiKey";

/** The key lives in sessionStorage: it is gone when the tab closes, and never in a URL. */
export const apiKey = {
  get: (): string | null => sessionStorage.getItem(KEY),
  set: (key: string): void => sessionStorage.setItem(KEY, key),
  clear: (): void => sessionStorage.removeItem(KEY),
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  /** Seconds to wait before trying again, from Retry-After on a 429. */
  readonly retryAfter: number | null;

  constructor(status: number, body: ErrorBody, retryAfter: number | null = null) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.requestId = body.request_id ?? null;
    this.retryAfter = retryAfter;
  }

  get unauthorized(): boolean {
    return this.status === 401;
  }
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const key = apiKey.get();
  if (key) headers.set("Authorization", `Bearer ${key}`);
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(0, { code: "network_error", message: "Couldn't reach the server." });
  }
  if (!response.ok) throw await toError(response);
  return response;
}

async function toError(response: Response): Promise<ApiError> {
  const retry = Number(response.headers.get("Retry-After"));
  const fallback = {
    code: `http_${response.status}`,
    message: `The server answered ${response.status}.`,
    request_id: response.headers.get("X-Request-ID"),
  };
  let body: ErrorBody = fallback;
  try {
    const parsed = await response.json();
    if (parsed?.error?.message) body = parsed.error;
  } catch {
    // not JSON, for example a proxy's error page
  }
  return new ApiError(response.status, body, Number.isFinite(retry) && retry > 0 ? retry : null);
}

const json = async <T>(path: string, init?: RequestInit): Promise<T> =>
  (await request(path, init)).json() as Promise<T>;

const post = (body: unknown, signal?: AbortSignal): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
  signal,
});

export const api = {
  /** True when the server accepts the stored key, or needs none. */
  async checkAccess(): Promise<boolean> {
    try {
      await request("/v1/documents?limit=1");
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.unauthorized) return false;
      throw error;
    }
  },

  listDocuments(status?: DocumentStatus, limit = 100): Promise<DocumentList> {
    const query = new URLSearchParams({ limit: String(limit) });
    if (status) query.set("status", status);
    return json(`/v1/documents?${query}`);
  },

  getDocument: (id: string): Promise<Document> =>
    json(`/v1/documents/${encodeURIComponent(id)}`),

  upload(file: File, replaces?: string): Promise<IngestResponse> {
    const form = new FormData();
    form.append("file", file);
    const query = replaces ? `?replaces=${encodeURIComponent(replaces)}` : "";
    return json(`/v1/documents${query}`, { method: "POST", body: form });
  },

  retry: (id: string): Promise<Document> =>
    json(`/v1/documents/${encodeURIComponent(id)}/retry`, { method: "POST" }),

  async remove(id: string): Promise<void> {
    await request(`/v1/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  /** The answer as it is written. Aborting the signal stops the generation. */
  async *ask(question: string, signal?: AbortSignal): AsyncGenerator<StreamEvent> {
    const response = await request("/v1/query/stream", post({ question }, signal));
    if (!response.body) throw new ApiError(0, { code: "no_stream", message: "No answer came." });
    for await (const raw of parseEvents(readBody(response.body))) {
      const event = toStreamEvent(raw);
      if (event) yield event;
    }
  },
};
