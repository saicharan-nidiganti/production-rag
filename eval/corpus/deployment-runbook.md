# Deployment Runbook

This runbook describes how services on the Tessera platform are released to production.
It applies to every team that owns a production service.

## Release pipeline

Every change reaches production through the release pipeline. A merge to the main branch
triggers a build, the full test suite and a container image scan. Images with a critical
vulnerability finding are blocked from promotion. A successful build is promoted to the
staging environment automatically, where it must soak for at least two hours before it can
be promoted to production.

## Canary releases

Production rollouts start with a canary. The canary receives 5% of production traffic for
30 minutes. During that window the pipeline compares the canary's error rate and p99 latency
with the stable fleet. If the canary's error rate is more than 0.5 percentage points above
the stable fleet, or its p99 latency is more than 20% higher, the rollout halts
automatically. After a clean canary window the rollout proceeds to 25%, then 50%, then 100%
of traffic, pausing 10 minutes at each step.

## Rollback

To roll back, run `tessera rollout undo <service>`. The command shifts all traffic back to
the previous release within about two minutes and keeps the failed release's pods running
for one hour so engineers can inspect them. Rolling back never requires approval: any
engineer on the owning team may roll back at any time. Database migrations are not
reverted by a rollback, which is why migrations must stay backward compatible with the
previous release.

## Deployment freeze windows

Production deployments are not allowed on Fridays after 14:00 UTC, during the last two
weeks of December, or during a declared SEV1 incident. Exceptions require written approval
from the on-call engineering director. Emergency fixes for an active incident are exempt
from the Friday freeze but still go through the canary.
