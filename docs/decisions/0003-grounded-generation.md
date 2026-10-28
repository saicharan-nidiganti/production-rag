# ADR 0003: Grounded generation with Claude search-result citations

- **Status:** Accepted
- **Date:** 2026-10-28

## Context

Answers must be supported by retrieved evidence, and citations must point at evidence that
supports the claim (methodology principles 3 and 4). The system must also say when it can't
answer.

## Options considered

| Option | For | Against |
|---|---|---|
| **Claude `search_result` blocks with citations enabled** | Native, structured citations. Each cited span names its search result by index, so it maps exactly to a retrieved chunk. No output parsing. | Citations can't be combined with structured outputs (`output_config.format`). Claude-specific. |
| Prompt the model to emit `[n]` markers and parse them | Works with any model. | Markers can be malformed or point at nothing. Parsing is fragile. Citation correctness rests on prompt compliance. |
| Structured output with an answer plus a list of chunk ids | Strict schema. | No per-claim attribution. Mutually exclusive with native citations. |

## Decision

- Send each retrieved chunk as one `search_result` block (`source = doc:<id>#chunk-<id>`, a title
  built from the document title, heading path and page) with `citations.enabled = true` on every
  block (the API requires all or none), then the question as a text block.
- Model `claude-opus-5-5`, explicit `output_config.effort` (default `medium`; the model's default
  is also `medium`, but it's set explicitly so a model change doesn't silently change cost or
  quality). No sampling parameters.
- Server-side refusal fallback on by default (`fallbacks: "default"`, beta
  `server-side-fallback-2026-07-01`). Safety classifiers can flag benign requests, and the
  fallback re-runs the request on Anthropic's recommended model for that refusal category
  instead of failing it. It can be turned off with `RAG_ANTHROPIC_FALLBACKS=false`, which is also
  required on platforms without server-side fallback.
- Status rules: `answered` requires at least one citation. Uncited text is returned as
  `insufficient_evidence`, and `stop_reason: refusal` as `refused`.
- Abstain before calling the model when retrieval finds no usable evidence (ADR 0004 covers the
  threshold).
- Keep an offline `extractive` generator implementing the same interface, for tests, CI and
  keyless development.

## Consequences

- A citation can only reference a retrieved chunk. Whether the cited chunk actually *supports*
  the sentence still needs evaluation (citation precision in `rag eval --generation`).
- Treating uncited answers as `insufficient_evidence` is deliberately strict. A correct but
  uncited answer is reported as not grounded. That is the intended trade-off.
- Retrieved text is untrusted input to the model. The system prompt treats search results as
  data, not instructions. This mitigates prompt injection without eliminating it.
- No live Claude results are recorded yet: the environment where this milestone was built had no
  API credentials. `tests/integration/test_live.py` and `rag eval --generation` with
  `RAG_GENERATOR=anthropic` are ready to produce them.
