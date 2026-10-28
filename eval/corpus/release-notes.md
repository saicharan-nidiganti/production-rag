# Release Notes

Highlights of recent Tessera releases. Minor fixes are listed in the changelog.

## Lens 3.2 (September 2026)

Lens 3.2 adds approximate count queries, which return results up to 10 times faster with an
error below 2%. The query result cache was extended from 5 to 10 minutes. Queries that scan
more than 50 GB now require an explicit `allow_large_scan` flag.

## Relay 2.0 (July 2026)

Relay 2.0 batches events in groups of up to 500 before writing them to the durable queue, which
cut ingestion costs by about a third. The maximum event size was raised from 256 KB to 1 MB.
Events larger than 1 MB are rejected with HTTP 413.

## Deprecations

Version 1 of the public API is deprecated and will be switched off on 31 March 2027. Clients
must move to version 2 before then. The legacy CSV export endpoint was removed in Lens 3.0;
use the Parquet export instead.
