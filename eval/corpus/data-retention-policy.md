# Data Retention and Protection Policy

This policy describes how long Tessera keeps customer data and how it is protected.

## Retention periods

Application logs are retained for 30 days. Audit logs are retained for 13 months to support
annual compliance reviews. Database backups are taken every 6 hours and retained for 35 days.
Customer content is kept for as long as the customer's account is active.

## Deletion requests

When a customer deletes their account, their content is removed from production systems
within 14 days. Copies in backups expire naturally when those backups age out, so all copies
are gone within 35 days of the deletion request. Customers receive a confirmation email once
deletion from production systems is complete.

## Encryption

Customer data is encrypted at rest with AES-256, with keys managed in the cloud provider's
key management service and rotated yearly. All traffic between clients and the platform,
and between internal services, uses TLS 1.3. Older TLS versions are rejected.

## Data residency

Customers on the Enterprise plan can pin their data to the EU region (Frankfurt) or the US
region (Virginia). Data pinned to a region, including its backups, never leaves that region.
