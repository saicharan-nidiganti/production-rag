# Tessera Platform Overview

Tessera is a managed platform for collecting, storing and querying event data.

## Components

The platform has three main components. The ingest gateway, called Relay, receives events
over HTTPS and buffers them in a durable queue. The storage layer, called Strata, writes
events into columnar files partitioned by day. The query engine, called Lens, runs SQL
queries over Strata and caches frequent results for 10 minutes.

## Regions

Tessera runs in two regions: EU (Frankfurt) and US (Virginia). Each region is fully
independent, so an outage in one region does not affect the other. Cross-region replication
is not offered.

## Service level objectives

Relay targets 99.95% monthly availability for event ingestion. Lens targets a p95 query
latency of under 2 seconds for queries that scan less than 1 GB of data. Availability is
measured from synthetic probes that run every 30 seconds in each region.

## Support plans

Standard customers get email support with a response target of one business day.
Enterprise customers also get a dedicated technical account manager and 24/7 phone support
with a 1-hour response target for urgent issues.
