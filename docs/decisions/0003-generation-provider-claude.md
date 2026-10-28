# ADR-0003: Generation provider — Claude with search-result citations

- **Status:** Proposed, 2026-10-29. Implemented in v0; awaiting the project
  owner's decision D-2 (paid API or local model).
- **Date:** 2026-10-29
- **Deciders:** Project owner, with engineering analysis
- **Supersedes:** none. It fills in [ADR-0001](0001-technology-stack.md) §8 and
  replaces the citation-marker parsing described in the v1 architecture.

## Context

Answers must be supported by retrieved evidence, and citations must point at
evidence that supports the claim (methodology principles 3 and 4, FR-7, FR-8).
ADR-0001 §8 asks for an `LLMProvider` interface, with the default provider
pending D-2. Session 0 recommended free local embeddings plus a paid LLM API
for answers. The target architecture planned numbered evidence blocks with
`[n]` markers, verified after generation.

## Decision drivers

1. Citation validity enforced by construction, not by parsing model output
2. Correct abstention when evidence is missing
3. Tests and CI never call a paid API
4. Swappable provider behind an interface

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **Claude, with each chunk sent as a `search_result` block and citations enabled** | Native, structured citations. Each cited span names its search result by index, so it maps exactly to a retrieved chunk, and `cited_text` is the chunk's own text. No output parsing. | Claude-specific. Citations can't be combined with structured outputs. Paid. |
| Any LLM, with `[n]` markers parsed and verified afterwards | Provider-neutral. | Markers can be malformed or point at nothing, so validity depends on post-processing. |
| A local model via Ollama | Free. Documents never leave the machine. | Weaker instruction following for citations. Needs a GPU, or is slow on CPU. |

## Decision

Proposed:

- `Generator` protocol (`generate(question, chunks) -> GeneratedAnswer`) with
  two implementations:
  - `AnthropicGenerator`: model `claude-opus-5-5`; one `search_result` block per
    retrieved chunk (source `doc:<id>#chunk-<id>`, title built from the document
    title, heading path and page) with `citations.enabled = true` on every block
    (the API requires all or none); explicit `output_config.effort`, default
    `medium`; no sampling parameters (removed in API version `2026-10-21`);
    server-side refusal fallback on by default (`fallbacks: "default"`, beta
    `server-side-fallback-2026-07-01`), so a safety-classifier false positive is
    retried on Anthropic's recommended model instead of failing.
  - `ExtractiveGenerator`: an offline, deterministic baseline that quotes the
    retrieved sentences overlapping the question, each cited. It is used by
    tests, CI and keyless development.
- Status rules: `answered` requires at least one citation. Uncited text is
  returned as `insufficient_evidence`, and `stop_reason: refusal` as `refused`.
- Abstention has two layers, as the v1 architecture planned: a retrieval-score
  threshold before generation ([ADR-0004](0004-embeddings-hybrid-retrieval-and-abstention.md)),
  then the model, which is told to say when the results don't answer the
  question.
- The system prompt treats search results as data, not instructions.

## Consequences

### Positive

- Citation validity (a requirements target of 100%, enforced in code) holds by
  construction: an out-of-range index is dropped and a citation can't name an
  unretrieved source.
- The pipeline runs end to end without credentials.

### Negative and risks

- Questions and retrieved chunks are sent to Anthropic on every query
  (NFR-4 says documents go only to providers the operator configures, which
  this satisfies, but operators must know it).
- Reporting uncited answers as `insufficient_evidence` is deliberately strict:
  a correct but uncited answer is reported as not grounded.
- Live Claude results have not been measured yet: the development PC has no
  API credentials. `rag eval --generation` with `RAG_GENERATOR=anthropic`
  produces them.
- The extractive baseline answers some unanswerable questions (3 of 6 declined
  on golden v1), because word overlap is a weak relevance signal. It is a
  floor, not a candidate for production.
- No streaming yet (FR-9).

## Revisit when

- D-2 is decided in favor of a local model: add an `OllamaGenerator` (or
  similar) behind the same protocol, with `[n]` marker verification.
- Live evaluation shows citation precision or abstention below the
  requirements targets.
