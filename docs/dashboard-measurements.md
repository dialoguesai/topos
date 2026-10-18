# Owner dashboard measurements v1

The personal SQLite node records first observations at storage commit. The control plane exposes `GET /v1/dashboard/measurements?period_start=YYYY-MM-DD&period_end=YYYY-MM-DD` (exclusive UTC end, at most 366 days) to the verified first-party owner. The owner-only `get_dashboard_measurements` relay handler runs off the event loop. Shared hosted pools are unavailable: facts do not carry a reliable tenant partition.

## Definitions

- **Inbound records:** new canonical messages, activity events, calendar events, journal entries, profile records, financial transactions, location events, documents and transcript sessions. Counted by table and its stable primary key, grouped by source at first insertion. These are stored records, not raw export items or files. Parent conversations, contacts, transcript speakers and segments are excluded to avoid counting structural fan-out.
- **New facts:** first observation of an active `signal_objects` fact's logical `object_key`, grouped by signal dimension. Revisions, corroborations, conflicts returning an incumbent, and inserted closed historical revisions do not add new facts. Fact attribution by inbound source is not claimed.
- **Bytes:** unavailable. File-transfer size, local cursor offsets and canonical text length are different measures; none is presented as total inbound volume.

The identity ledger has no copied payloads. It intentionally keeps identity receipts after source-row deletion, so re-imports cannot inflate the counts. Counts describe arrivals, not current inventory. Identity-changing updates also reserve their new identity without an arrival. SQL triggers and receipts commit or roll back together, including replacement writes and concurrent retries. Keep the ledger as owner data: logical fact keys can themselves contain values.

## Coverage

Explicit source erasure removes its source-attributed receipts too, invalidating coverage for the affected family. Until instrumentation re-baselines the remaining identities, that metric is unavailable. Re-baselining starts a new observation window; erased history is never presented as measured zero. A later import of deliberately erased data belongs to that new window.

Migration 88 seeds existing identities with NULL observation times. It never reconstructs historical arrivals from mutable `ingested_at`, event time or fact validity time. Triggers record UTC observation time. Earlier dates are `count: null, coverage: unavailable`, the activation day and current day are partial, and only fully covered days may be measured zero. Lazy-created canonical tables get triggers before their writer runs. Aggregate coverage conservatively starts at the latest covered family start. A missing trigger makes its metric unavailable; repairing it baselines intervening rows and restarts coverage. Queries do not create schema.

## Rollout

Land/release the node migration with the node's normal backup and downgrade guard. Then advance the control-plane compatibility pin and deploy its owner relay endpoint; deploy the dashboard client. Old/offline nodes and missing endpoints remain unavailable, never fixture values or zeroes. This change does not migrate a running owner's database during development.
