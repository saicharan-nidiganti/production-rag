# Public API Usage Limits

The Tessera public API enforces rate limits and quotas to keep the service fair and stable.

## Rate limits by plan

Rate limits are applied per API key. The Standard plan allows 600 requests per minute. The
Enterprise plan allows 6,000 requests per minute. Both plans permit short bursts of up to
twice the per-minute limit, provided the average over five minutes stays within the limit.

## Handling 429 responses

When a client exceeds its rate limit, the API responds with HTTP status 429 and a
`Retry-After` header giving the number of seconds to wait. Clients should wait at least that
long and then retry with exponential backoff and jitter. Requests rejected with 429 do not
count against the monthly quota.

## Monthly quotas

Each plan includes a monthly request quota: 5 million requests for Standard and 100 million
for Enterprise. Usage above the quota is billed per additional million requests. Quotas reset
at 00:00 UTC on the first day of each calendar month.

## API keys

API keys are created in the admin console. Keys must be rotated at least every 90 days, and
the console warns key owners 14 days before a key reaches that age. Each organization can
have at most 50 active keys. A revoked key stops working within 60 seconds.
