# ADR-0012: Accounts, sessions and per-user isolation

- **Status:** Accepted, 2026-11-12. An engineering decision, delivering FR-1, NFR-1 and
  the password part of NFR-2. The owner approved the phase.
- **Date:** 2026-11-12
- **Deciders:** Engineering
- **Supersedes:** the "single tenant" scope of [ADR-0002](0002-v0-storage-and-scope.md) §3,
  when accounts are turned on. The move to PostgreSQL in that ADR is a separate step.

## Context

Until now the service had one optional static key and one shared set of documents: anyone
with the key could read and delete everything. NFR-1 requires that a user can never
retrieve, view or be cited content from another user's documents, with the ownership
filter applied inside retrieval queries, not after them. NFR-2 requires passwords hashed
with a memory-hard algorithm and rate limits on authentication.

The service is one process on SQLite, with a web interface on the same origin
([ADR-0011](0011-web-interface-served-by-the-backend.md)) and scripts that call the API.

## Decision drivers

- Isolation must hold in every code path, including ones added later.
- Existing deployments must keep working unchanged.
- A browser and a script authenticate differently, and each needs its own protection.
- No secret may be recoverable from a copy of the database.
- Nothing here may depend on SQLite, since PostgreSQL comes next.

## Options considered

| Question | Options | Chosen |
| --- | --- | --- |
| Where the owner filter goes | After retrieval, on the results; or inside each search | **Inside each search**, and again when chunks are loaded. Filtering afterwards lets a large library crowd a small one out of the candidates, and leaves one forgotten filter away from a leak |
| Sessions | Signed stateless tokens (JWT); or rows in the database | **Rows in the database.** They can be ended at once: on sign-out, a password change, or disabling an account. A signed token stays valid until it expires |
| Browser credential | A token in `sessionStorage`; or an `HttpOnly` cookie | **An `HttpOnly`, `SameSite=Strict` cookie**, which scripts can't read, with a CSRF token on requests that change state |
| Script credential | The user's password; or a personal token | **Personal API tokens**, shown once and revocable one by one |
| Password hash | bcrypt, scrypt, Argon2id | **Argon2id** (`argon2-cffi`), memory-hard, with its parameters stored in each hash |
| Sharing | Roles within a shared library; or one library per user | **One library per user.** Sharing between users is not in scope |

## Decision

1. **Opt-in.** `RAG_ACCOUNTS_ENABLED=true` turns accounts on. It can't be combined with
   `RAG_API_KEY`. With it off, nothing changes: every document belongs to the shared
   owner `''`.
2. **Every document has an owner.** `documents.owner_id` is set at upload and never
   changes. Every repository method, ingestion call and search takes the owner; API routes
   get it from one dependency. Another user's document is reported exactly like one that
   doesn't exist (404), including as the target of `?replaces=`.
3. **The filter is inside both searches.** Lexical search joins to `documents` and filters
   in the same statement as the `MATCH`. Vector search keeps each owner's rows of the
   in-memory matrix and scores only those. Loading the chunks checks the owner again.
4. **Uniqueness is per owner.** The same bytes uploaded by two users are two documents, and
   the second user isn't told the content already exists. A chunk key is unique within its
   document (migration 0005).
5. **Passwords** are hashed with Argon2id (64 MiB, 3 passes), at least 12 characters, and
   re-hashed at sign-in when the parameters change. An unknown address costs the same work
   as a known one, and gets the same answer as a wrong password.
6. **Sessions.** Signing in sets `rag_session`: `HttpOnly`, `SameSite=Strict`, `Secure`
   by default, lasting `RAG_SESSION_TTL_HOURS` (12). The database stores the SHA-256 of
   the token. Requests that change state must send the session's CSRF token as
   `X-CSRF-Token`. Sign-out, a password change (for every other session) and disabling an
   account end sessions immediately.
7. **Personal API tokens** (`rag_…`) are stored as SHA-256 and sent as a bearer token. They
   need no CSRF token, since a browser doesn't attach them. A token can't create tokens or
   change the password; those need a session.
8. **Authentication happens in the request guard**, before a body is read, as the static
   key's did. Failed sign-ins and invalid credentials draw from the same per-client
   allowance, after which the client is refused without a password being checked.
9. **Two roles.** An administrator creates, disables and deletes users and reads
   `/metrics`. An administrator can't read other users' documents. The last working
   administrator can't be removed. The first is created from the server's shell:
   `rag users create --admin`.
10. **Deleting a user deletes their documents**, with the chunks and index entries.

## Consequences

- A forgotten owner in a new route would be a leak. Two tests guard against it: one walks
  the OpenAPI schema and calls every route that takes a document id as another user, and
  one asks each search directly. Both were checked by breaking the code on purpose.
- Sessions cost one indexed read per request, run off the event loop.
- Rate limits and the lockout are still per client address and per process.
- `/ready` stays open and still reports totals across all users.
- There is no self-service sign-up, password reset by email, or second factor. An
  administrator sets a password for a user who has lost theirs.
- Evaluation, the CLI's `ingest`, `search` and `ask`, and the benchmark use the shared
  space; they are operator tools on the server.

## Revisit when

- Users need to share documents: add groups or per-document grants.
- The service is exposed to people the operator doesn't know: add sign-up with email
  verification, password reset and a second factor.
- More than one process serves requests: move the lockout and rate limits to a shared
  store, with PostgreSQL.
