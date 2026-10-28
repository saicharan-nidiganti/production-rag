# Incident Response Policy

This policy defines how Tessera detects, classifies and resolves production incidents.

## Severity levels

Incidents are classified into four severity levels.

- **SEV1**: a full outage or data loss affecting many customers. The on-call engineer must
  acknowledge the page within 5 minutes, and status page updates are posted every 30 minutes.
- **SEV2**: a major feature is unavailable or badly degraded for some customers.
  Acknowledgement is required within 15 minutes.
- **SEV3**: a minor feature is degraded and a workaround exists. Acknowledgement is required
  within one business day.
- **SEV4**: a cosmetic issue or a problem with no customer impact. It is tracked as a normal
  ticket.

## Roles during an incident

Every SEV1 and SEV2 incident has an incident commander, who coordinates the response and
makes decisions, and a communications lead, who owns the status page and customer updates.
The incident commander does not debug the problem directly. For SEV1 incidents, the
incident commander must be a member of the senior on-call rotation.

## Escalation

If the primary on-call engineer does not acknowledge a page in time, the paging system
escalates to the secondary on-call engineer, and then to the engineering manager of the
owning team. Anyone can raise an incident's severity; only the incident commander can lower
it.

## Postmortems

A blameless postmortem is required for every SEV1 and SEV2 incident. The draft is due
within 5 business days of resolution and is reviewed at the weekly reliability review.
Every postmortem lists action items, each with an owner and a due date.
