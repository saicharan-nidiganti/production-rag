# Requirements

Status: **Accepted for v1** (2026-10-28). Items marked *assumption* are product
defaults that the project owner can change. Changing one may change the scope of
later phases.

The **v0 status** columns record what the v0 backend delivers (2026-10-29).
[status.md](status.md) has the current summary.

## 1. Purpose

Ask My Docs lets a user upload their own documents, ask questions in natural
language, and receive answers that are grounded in those documents, with
citations to the exact source passages. When the documents do not contain the
answer, the system says so instead of guessing.

## 2. Users and primary use cases

- **Individual user (v1):** uploads private documents (reports, manuals,
  contracts, papers, notes) and asks questions across them.
- **Typical questions:** fact lookup ("What is the notice period?"),
  synthesis across sections or documents ("Summarize the risks in these two
  reports"), and questions the documents cannot answer.

Each user's documents are private to that user. Sharing and teams are out of
scope for v1 (see §6).

## 3. Functional requirements

Priority uses MoSCoW: **M**ust, **S**hould, **C**ould.

| ID | Requirement | Priority | Phase | v0 status |
| --- | --- | --- | --- | --- |
| FR-1 | Users can register, sign in and sign out. All data is scoped to the signed-in user. | M | 5 | Not started (v0 is single tenant) |
| FR-2 | Upload documents in PDF (text-based), DOCX, TXT, Markdown and HTML. *Assumption:* default limit 25 MB per file, configurable. | M | 2 | Partial: PDF, TXT, Markdown; 25 MiB default. DOCX and HTML pending |
| FR-3 | Each document shows an ingestion status: `queued`, `processing`, `ready` or `failed`, with an actionable reason on failure. Failed documents can be retried. | M | 2 | Done: background workers, failure code and message, retry endpoint; survives restarts ([ADR-0006](decisions/0006-background-ingestion.md)) |
| FR-4 | Re-uploading identical content (same SHA-256) for the same user does not create a duplicate. The existing document is returned. | M | 2 | Done (across all documents, not yet per user) |
| FR-5 | Users can delete a document. This removes the stored file, its chunks and its vectors. | M | 2 | Done: the raw upload is kept only until processing succeeds, and deletion removes it with the chunks |
| FR-6 | Users can ask a question across all of their documents or a selected subset. | M | 3–4 | Partial: all documents; subset selection pending |
| FR-7 | Answers are generated only from retrieved evidence and carry inline citations that identify the document, the page (where the format has pages) and the passage. | M | 4 | Done: document, heading path, page, passage |
| FR-8 | When the evidence is insufficient, the system states that the documents do not contain the answer. Partially supported answers identify which parts are unsupported. | M | 4 | Partial: retrieval-level abstention catches off-topic questions; on-topic unanswerable ones rely on the generator; partial-support marking relies on the model prompt |
| FR-9 | Answers stream to the client as they are generated. | S | 4 | Not started |
| FR-10 | Selecting a citation shows the cited passage and its location in the source document. | M | 6 | Partial: the API returns cited text and offsets; UI in Phase 6 |
| FR-11 | Follow-up questions can use the earlier turns of the same conversation. | S | 4 | Not started |
| FR-12 | Scanned (image-only) PDFs are processed with OCR. Until then they fail with a clear "no extractable text" message. | S | 8 | Interim behavior done (clear error); OCR not started |
| FR-13 | Users can see their recent questions and answers. | C | 6 | Not started |

## 4. Non-functional requirements

| ID | Area | Requirement | v0 status |
| --- | --- | --- | --- |
| NFR-1 | Isolation | A user can never retrieve, view or be cited content from another user's documents. Ownership filters are applied inside retrieval queries, not after them. | Not met (single tenant) |
| NFR-2 | Security | Uploaded files are validated by content (not extension), stored under generated names, and never executed. Passwords are hashed with a memory-hard algorithm. Rate limits apply to authentication, upload and question endpoints. | Partial: content-validated, never stored or executed; no passwords or rate limits yet |
| NFR-3 | Prompt injection | Document text is treated as untrusted data. Instructions inside documents must not change system behavior, leak data or alter citation rules. | Partial: untrusted-data framing; injection cases in golden v2, not yet measured with an LLM |
| NFR-4 | Privacy | Document contents, questions and answers are not written to application logs. Documents are sent only to the model providers configured by the operator. | Met |
| NFR-5 | Reliability | A failed ingestion leaves no partial chunks or vectors behind. Reprocessing a document is idempotent. | Met: chunks are written in the transaction that marks a document ready; a worker that lost its claim discards its work; idempotent by hash |
| NFR-6 | Traceability | Every answer records which chunks were retrieved and cited, with the retrieval configuration and model versions used. | Partial: responses carry retrieved and cited chunks and the model; answers aren't stored |
| NFR-7 | Performance | Initial targets in §7. They are measured on a reference dataset, not assumed. | Partial: measured on golden v1, not yet on the 10k-chunk reference |
| NFR-8 | Accessibility | The web interface meets WCAG 2.1 AA for core flows (sign in, upload, ask, read citations). | Not started (no UI) |
| NFR-9 | Reproducibility | A new developer can run the full stack locally from the README. Evaluation runs are repeatable from a versioned dataset and configuration. | Met |
| NFR-10 | Maintainability | Typed code, automated lint/type/test checks in CI, and ADRs for significant decisions. | Met |

## 5. Constraints

- **Team and budget:** a single developer with a minimal budget. Free and
  open-source components are preferred. A paid service is used only with the
  owner's explicit approval.
- **Licensing:** the project is MIT-licensed. Runtime dependencies must use
  permissive licenses that are compatible with MIT. Do not use AGPL or GPL
  libraries (for example, PyMuPDF is AGPL and is excluded). LGPL libraries
  used unmodified as separate packages (for example, a PostgreSQL driver) are
  acceptable.
- **Public repository:** no secrets, credentials or real user documents in git.
  Test and evaluation documents must be synthetic or public-domain.
- **Environments:** local development on Windows (via Docker), CI on Linux
  (GitHub Actions). The same configuration must work in both.
- **Language:** English documents and questions for v1.

## 6. Out of scope for v1

- Teams, organizations, document sharing, SSO
- Spreadsheets, slide decks, images, audio and video as inputs
- Understanding charts, figures or complex tables beyond their extracted text
- Multilingual retrieval
- Fine-tuning models
- Agents, tool use or web search during answering

## 7. Success criteria

These are **initial targets**. They are re-calibrated after the Phase 3 baseline
is measured, and any change to them is recorded with its reason. Metrics are
computed on the current golden dataset (see [methodology.md](methodology.md)).

### Retrieval

| Metric | Initial target |
| --- | --- |
| Recall@5 (answerable questions with at least one relevant chunk in the top 5) | ≥ 0.85 |
| MRR@10 | ≥ 0.70 |

### Answers and citations

| Metric | Initial target |
| --- | --- |
| Citation validity: every citation refers to a chunk that was retrieved for this answer | 100% (enforced in code) |
| Citation precision: the cited passage supports the claim it is attached to | ≥ 0.95 |
| Groundedness: the share of answer claims that are supported by the cited evidence | ≥ 0.90 |
| Answer correctness against reference answers | ≥ 0.80 |
| Correct abstention on unanswerable questions | ≥ 0.90 |
| False abstention on answerable questions | ≤ 0.10 |

### Security

| Check | Target |
| --- | --- |
| Cross-tenant isolation test suite | 0 leaks |
| Prompt-injection test suite (malicious documents) | 0 successful injections |
| Secret scanning on every commit | 0 findings |

### Performance (reference: 10,000 chunks per user, local development hardware)

| Metric | Initial target |
| --- | --- |
| Retrieval latency, p95 (excluding reranking) | < 300 ms |
| Time to first answer token, p95 (excluding model-provider queueing) | < 3 s |
| Ingestion of a 100-page text PDF, end to end | < 60 s |

### Engineering

- CI passes lint, type checks, tests and build on `main`.
- Setup from a clean machine works by following the README.

## 8. Open decisions

| ID | Decision | Needed by | Owner |
| --- | --- | --- | --- |
| D-1 | How changes reach GitHub (session push access, or push from the developer PC) | Resolved 2026-10-29: sessions on the developer PC push verified changes to `main` | Project owner |
| D-2 | Generation model provider: paid API, or local via Ollama | Resolved 2026-10-29: Anthropic as the configurable hosted provider, offline extractive mode kept ([ADR-0003](decisions/0003-generation-provider-claude.md)) | Project owner |
| D-3 | Storage for v0: SQLite as built, or PostgreSQL now (Docker Desktop or a native install on Windows) | Resolved 2026-10-29: SQLite for v0 ([ADR-0002](decisions/0002-v0-storage-and-scope.md)); PostgreSQL arrives with Phase 5 accounts | Project owner |
