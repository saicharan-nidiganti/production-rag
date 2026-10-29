# Security Policy

## Supported versions

Security fixes are made on `main` and shipped in the next release. Only the latest
release (currently 0.1.x) and `main` are supported; older releases don't receive fixes.

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Report vulnerabilities privately through GitHub:
**Security** tab → **Report a vulnerability**
([direct link](https://github.com/saicharan-nidiganti/production-rag/security/advisories/new)).

Please include:

- A description of the issue and its impact
- Steps to reproduce or a proof of concept
- Affected commit or version

This is a single-maintainer project. Reports are handled on a best-effort
basis, with the aim of acknowledging them within 7 days.

## Scope

Issues of particular interest include:

- Access to another user's documents, chunks or answers (tenant isolation)
- Authentication or session weaknesses
- Prompt injection that causes the system to ignore its rules, leak data, or
  fabricate citations
- Unsafe file handling (path traversal, parser exploits, resource exhaustion)
- Exposure of secrets or document contents in logs, errors or responses
