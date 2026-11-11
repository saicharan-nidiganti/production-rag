# ADR-0011: A web interface served by the backend, on the same origin

- **Status:** Accepted, 2026-11-11. An engineering decision, within the stack the owner
  proposed in [ADR-0001](0001-technology-stack.md) §11.
- **Date:** 2026-11-11
- **Deciders:** Engineering
- **Supersedes:** none. Implements roadmap Phase 6 without accounts, which stay in Phase 5.

## Context

The backend had only an HTTP API and a CLI. FR-10 requires that selecting a citation shows
the cited passage and where it is in its document, which needs an interface. ADR-0001 §11
chose React, TypeScript, Vite and Tailwind CSS.

Three things were undecided: where the interface's files are served from, how the browser
authenticates, and how it reads a streamed answer. The backend has one static API key, no
user accounts and no sessions ([ADR-0002](0002-v0-storage-and-scope.md)), and it sends no
CORS headers.

## Decision drivers

- One thing to deploy: the product is one process and one image.
- No new attack surface on the API: no CORS policy to get wrong, no cookies, no CSRF.
- The API key must not end up in a URL, a log line or long-lived browser storage.
- The interface must work with the offline configuration, with no build step for someone
  who only wants the API.

## Options considered

| Option | Notes |
| --- | --- |
| **The backend serves the built files, same origin** ✅ | One image and one port. No CORS. A strict Content-Security-Policy is easy, because everything comes from one origin |
| A separate static host or CDN | The usual choice at scale, but needs CORS on the API and a second thing to deploy and version |
| The reverse proxy serves the files | Works, and stays possible: the files are plain static output. But it makes the proxy required, and a plain `docker run` would have no interface |
| Server-rendered pages | Rejected in ADR-0001 §11: a poor fit for a streamed answer and an interactive citation panel |

For the key: a cookie session would need a login endpoint, CSRF protection and server-side
session state, all of which belong with accounts in Phase 5. `localStorage` keeps a secret
on disk indefinitely. `sessionStorage` keeps it for the tab's lifetime only.

## Decision

1. **The backend serves the interface when `RAG_WEB_DIR` is set.** It points at the built
   files (`web/dist`). The container image builds them in a Node stage and sets the variable;
   from source, the interface is opt-in. With it unset, nothing changes.
2. **The files are public; the data isn't.** `/`, `/index.html`, `/favicon.svg` and
   `/assets/*` pass the request guard without the key, because the page must load before a
   key can be typed and the files hold no data. Everything the page shows comes from `/v1`,
   which the key guards as before.
3. **The page is locked to its own origin.** It is sent with
   `Content-Security-Policy: default-src 'self'` (no inline or third-party scripts or
   styles, no framing), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
   The build has no runtime third-party requests: no CDN, fonts or analytics.
4. **The API key is asked for only when the server requires one**, sent as
   `Authorization: Bearer`, and kept in `sessionStorage`. A 401 at any time returns to the
   key screen.
5. **Streamed answers are read with `fetch` and a small Server-Sent Events parser**, because
   `EventSource` can't send a POST body. Stopping aborts the request, which stops the
   generation ([ADR-0010](0010-streaming-answers-over-sse.md)).
6. **`index.html` is never cached; the hashed script and style files are cached for a
   year.** A new version is picked up on the next load.

## Consequences

- One image now holds the API and the interface, and the image build needs Node. The wheel
  doesn't include the interface: it is a Python package, and the files are served from a
  directory.
- The interface inherits the API's limits: one shared key, so anyone who has it sees and
  can delete every document. There are no per-user views until Phase 5.
- A script injected into the page could read the key from `sessionStorage`. The policy in
  point 3 is the control: the page loads no code but its own, and answers and passages are
  rendered as text, never as HTML.
- The interface's packages are a second dependency tree. CI lints, type-checks, tests and
  builds it, audits the packages that ship to the browser, and Dependabot watches it.

## Revisit when

- Accounts arrive (Phase 5): replace the key screen with sign-in and an `HttpOnly` session
  cookie, with CSRF protection.
- The interface needs client-side routes or grows past one bundle.
- Several instances sit behind a load balancer: serve the files from the proxy or a CDN.
