# Documentation

Each topic has one authoritative document. Start with the repository
[README](../README.md) for an overview, then come here for depth.

| Document | Read it to | Audience |
| --- | --- | --- |
| [requirements.md](requirements.md) | See the scope, the functional and non-functional requirements, and each one's state | Everyone |
| [architecture.md](architecture.md) | Understand the components, the request and ingestion flows, the code layout and its enforced layering, and the path to v1 | Contributors, reviewers |
| [api.md](api.md) | Call the HTTP API: every endpoint, field, status code and error, with real responses | API users |
| [configuration.md](configuration.md) | Look up any environment variable and its default (a test keeps it complete) | Operators, contributors |
| [deployment.md](deployment.md) | See what a deployment needs: the supported targets, hardware, environments, the release process, reliability, scaling, cost and recovery requirements | Operators, reviewers |
| [operations.md](operations.md) | Deploy, migrate, monitor, back up and troubleshoot, including background ingestion | Operators |
| [runbooks.md](runbooks.md) | Respond to an incident: severity, first steps, a procedure per failure, rollback, verifying recovery, the incident record | Operators |
| [observability.md](observability.md) | Find every log event and metric, correlate a request, set alerts, and see what is never recorded | Operators, reviewers |
| [performance.md](performance.md) | See the performance targets, run the benchmark, read the measured results and capacity estimates, and understand the regression gate | Operators, contributors |
| [data-lifecycle.md](data-lifecycle.md) | See what is stored and for how long, trace evidence to its source, back up, verify, restore and delete | Operators, reviewers |
| [security.md](security.md) | Review the controls, data flows, third-party models, supply chain and known gaps | Reviewers, operators |
| [evaluation.md](evaluation.md) | Understand the dataset, the metrics, current results, the regression gates and the paid-evaluation plan | Contributors, reviewers |
| [decisions/](decisions/README.md) | Read why each consequential technical choice was made, with options and trade-offs (ADRs) | Contributors, reviewers |
| [release-criteria.md](release-criteria.md) | See what a release must pass, which checks block it, what it records, and the limitations accepted for it | Maintainers, reviewers |
| [roadmap.md](roadmap.md) | See the phase-by-phase plan and what is done | Everyone |
| [status.md](status.md) | See current progress, the checks run each session, open decisions and blockers | Maintainers |

Contributing guidelines are in [CONTRIBUTING.md](../CONTRIBUTING.md), vulnerability
reporting in [SECURITY.md](../SECURITY.md), and notable changes in
[CHANGELOG.md](../CHANGELOG.md).

## How status is described

The documents use the same words for how far a capability has got:

| Term | Meaning |
| --- | --- |
| **Implemented and verified** | In the code, covered by tests or a gate that runs in CI |
| **Implemented, not verified live** | In the code and tested offline, but its behavior with a live external service hasn't been measured (today: Claude's answers) |
| **Planned** | On the [roadmap](roadmap.md), not in the code |
| **Proposed** | An ADR awaiting the owner's decision; nothing depends on it yet |
| **Deferred** | Deliberately postponed, with the reason recorded |
