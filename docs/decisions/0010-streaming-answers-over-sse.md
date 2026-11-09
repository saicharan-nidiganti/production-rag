# ADR-0010: Stream answers over Server-Sent Events

- **Status:** Accepted, 2026-11-10. An engineering decision, taken to deliver FR-9.
- **Date:** 2026-11-10
- **Deciders:** Engineering
- **Supersedes:** none. Resolves the "no streaming" notes in
  [ADR-0002](0002-v0-storage-and-scope.md) §4 and [ADR-0003](0003-generation-provider-claude.md).

## Context

FR-9 requires answers to stream to the client as they are generated. With Claude, an answer
can take several seconds; streaming lets a client show text and sources as they arrive. The
existing `POST /v1/query` returns one JSON document, and its shape is part of the `/v1`
contract ([api.md](../api.md#conventions)).

Two facts about Claude's stream shape the design. Citations arrive as their own events
(`citations_delta`), separate from text. And a safety classifier can decline partway: with
server-side fallbacks on, the stream keeps what was sent and continues on the fallback model
after a `fallback` block; without one, it ends in a refusal, and the partial text must be
discarded.

## Decision drivers

1. Clients see text and citations as soon as they exist.
2. The final answer is exactly what `POST /v1/query` returns, so clients and tests can rely
   on one shape, and refusals can't leave partial text standing as an answer.
3. Works through ordinary HTTP infrastructure (proxies, `curl`, browsers' `fetch`).
4. A client that goes away stops the generation, since Claude output is billed.
5. No new infrastructure or dependency.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **A separate `POST /v1/query/stream` answering with Server-Sent Events** ✅ | Plain HTTP; named events fit text, citation, done and error; `curl -N` and `fetch` read it; `/v1/query` stays unchanged | No resume after a dropped connection; proxies must not buffer it |
| `stream: true` on `POST /v1/query` | One endpoint | One path with two response types; breaks the response model in the OpenAPI schema |
| WebSockets | Two-way | Needs an upgrade-capable proxy and a different auth path; nothing goes from client to server during an answer |
| Newline-delimited JSON over chunked HTTP | Simple | No event names or standard client parsing; reimplements SSE |

## Decision

1. `POST /v1/query/stream` takes the `/v1/query` body and answers with `text/event-stream`:
   - `retrieval`, once, first.
   - `text` and `citation` as the answer is written. A citation's `index` is its position in
     the final `citations`.
   - `done`, last: the `/v1/query` response, which is authoritative. When its `status` is
     `refused`, the streamed text is discarded and replaced by `done.answer`.
   - `error`, instead of `done`, when generation fails after the response started; same
     envelope as error responses, with the request id.
2. Retrieval runs before the response starts, so its failures, validation, authentication
   and rate limits are ordinary HTTP errors. The stream counts against the question rate
   limit, like `/v1/query`.
3. Generators gain `stream()`, which yields text and citation events and ends with the
   complete answer. The Claude generator uses the SDK's `messages.stream` and builds the final
   answer from `get_final_message()` with the same code as `generate()`, so both paths give
   identical answers. The extractive generator streams its answer sentence by sentence.
4. A disconnected client closes the iterator chain down to the SDK stream, which closes the
   HTTP connection to the Claude API. Starlette doesn't close a synchronous body iterator on
   disconnect, so the endpoint uses its own wrapper that does.
5. An `error` event inside Claude's stream reaches the SDK caller as an API error with the
   stream's HTTP status, 200; it is classified by its error type (`overloaded_error` and
   `api_error` are retryable), not by that status.
6. The response sets `Cache-Control: no-cache` and `X-Accel-Buffering: no`, so proxies like
   nginx pass events through as they come.

Verified by tests:

- The Claude generator runs against the real SDK with a mock HTTP transport replaying Messages
  API stream events: text and citations stream in order, and the final answer equals
  `generate()` on the same content. Also covered: thinking blocks, a mid-stream fallback (text
  kept), a refusal (text withdrawn), uncited answers, citations outside the retrieved chunks,
  errors before and during the stream, and closing early closing the HTTP response.
- The service: the stream ends with what `answer()` returns, abstention streams no text, and
  closing early stops the generator.
- The endpoint: event order; `done` equal to `/v1/query`; citation indexes; abstention; error
  events (retryable or not, with the request id; unexpected errors reveal nothing); retrieval
  failures, validation and authentication before the stream; the shared rate limit; and the
  disconnect wrapper closing the events.
- The CI smoke test reads a streamed answer from a real server.
- Each protection was checked by removing it: the tests fail without the close on disconnect
  (at both layers), without the rate-limit mapping, without classifying in-stream errors by
  type, and with a final answer built from the streamed text.

## Consequences

### Positive

- Clients can render an answer and its sources as Claude writes them, and the final event
  gives the same answer `/v1/query` would.
- Abandoned requests stop generating within one event.
- `rag ask --stream` shows the same in a terminal.

### Negative and risks

- A dropped connection can't be resumed (no event ids); the client asks again.
- The paid Claude path is verified against the SDK's parsing of recorded event shapes, not
  live; it shares the paid evaluation's blocker (status.md, D-4).
- Before the first text there can be a pause while Claude thinks (adaptive thinking streams
  empty thinking blocks by default), and no heartbeat is sent: a proxy's idle timeout must
  exceed it.
- `generation` timing includes the time the client spent reading the stream.

## Revisit when

- Clients need to resume: add event ids and `Last-Event-ID`.
- Proxies drop quiet streams: send SSE comment heartbeats while waiting.
- Conversations arrive (FR-11): the stream carries a conversation id.
