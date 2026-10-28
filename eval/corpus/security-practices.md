# Security Practices

These practices apply to every Tessera employee and contractor with access to company systems.

## Access reviews

Access to production systems is reviewed every quarter by each team's engineering manager.
Access that is not re-approved within 10 business days of the review is revoked automatically.
Departing employees lose all access within 4 hours of their last working day.

## Multi-factor authentication

All employee accounts require multi-factor authentication. Production access additionally
requires a hardware security key, and SMS codes are not accepted as a second factor. A lost
security key must be reported to the security team within 24 hours.

## Vulnerability management

Container images and dependencies are scanned daily. Critical vulnerabilities must be patched
within 7 days, high-severity ones within 30 days and medium-severity ones within 90 days. An
external penetration test is performed once a year, and its findings are tracked like any other
vulnerability.

## Secrets handling

Secrets are stored in the central secrets vault and injected at runtime. They must never be
committed to source control. A secret that is exposed, for example in a log or a chat message,
must be rotated within 24 hours. Every secret has a named owner and is rotated at least once a
year.
