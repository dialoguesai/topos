# Merge notes: `beta/v2-bookkeeping-2`

Engine-side bookkeeping fixes from §7 of `SCALABLE_GRANTS_DESIGN.md` (W6/R8, R5/W4, R9,
R1, and the R6 follow-up), built on `beta/permissions-v2` at `212a0db4` in an isolated
worktree. **Do not merge before the M1 smoke and campaign run have finished on node P.**
The engine and any shadow host must then be deployed from the same commit, as for
`212a0db4`.

## What changes on disk, and what does not

| Fix | On-disk change | When it happens | Rollback to an older engine |
|---|---|---|---|
| W6/R8 streamed exclusion fingerprint (`exclusion_floor.py`) | none; the digest value is byte-identical | n/a | safe: same value, no marker or ledger row moves |
| W6/R8 revision cache (`protection_clock.current_protection_revision`) | none; process-local, never written | n/a | safe |
| R5/W4 event-log indexes (`protection_clock.EVENT_INDEXES`) | two index b-trees in the canonical database: `(artifact_key, generation)` and `(source, artifact_key, generation)` on `permissions_v2_protection_events` | at the first `ensure_protection_clock` on this engine, i.e. the first node start after the deploy; `IF NOT EXISTS` afterwards | safe: `clock_state` compares tables and triggers, never indexes, and an older engine serves a clock that carries them unchanged |
| R9 + R1 indexes (migration 76, `permissions_read_path_indexes_v1`, `always_run`) | four index b-trees: `entities(is_self) WHERE is_self=1`, `entity_merge_tombstones(merged_into)`, and `(length(content), substr(content,1,64))` on `conversation_messages` and `ai_chat_messages`; `PRAGMA user_version` stamped 76 | at the first `ensure_migrations_applied` on this engine, i.e. node start (the runner also re-arms on any later DDL, which is how a message table created by legacy DDL gets its index) | **not safe**: an engine whose registry stops at 75 refuses the database (`DowngradeGuardError`, "upgraded by a newer topos-node"). The step is additive and nothing an older engine reads, so the lossless recovery is to walk the stamp back (`PRAGMA user_version = 75`), per the 2026-08-19 schema-fence note; never restore a backup for this |
| R6 follow-up `fact_reviews(fact_id, active)` | already on the base: `212a0db4` creates `fact_reviews_current` in both review store files at open | n/a | n/a |

"Safe under a running node" means the fix needs no state change and an older engine can
open the same files. Every index here is built while the node starts; none is built by a
serving node mid-flight. Measured one-time costs at the first start: about 0.8 s per
500,000 protection events (28 MB + 36 MB of index against 36 MB of table), and about 3 s
per 1.2 million messages (101 MB of index against 874 MB of tables; a plain index on
`content` would have been 170 MB and grows with message length). Migration 76 is
`always_run`, so it triggers no pre-migration backup.

Migration 76 must land at a release cut like specs 63, 69 and 75: registering it stamps
the schema version past any engine that predates it. Do not run an engine built from this
branch against a node whose installed engine predates it. The hermetic test lane cannot
reach a live database, but a script that imports this tree and opens `~/.topos/database.db`
read-write would stamp it.

## Node P

At P's next start on the merged engine: `ensure_migrations_applied` stamps 76 and builds the
four read-path indexes; `ensure_protection_clock` verifies the clock as before, then builds
the two event-log indexes. No clock upgrade lane runs, no review store is re-enrolled, no
marker is re-pinned, no owner review goes stale. The cached revision starts empty in every
process and warms on the first read.

## Deviations from the §7 text, each with the measured reason

- **`entities(merged_into)` does not exist.** `merged_into` is a column of
  `entity_merge_tombstones`, whose primary key is the absorbed side; `composition_revision`
  asks `WHERE merged_into=?`, so that is the index built.
- **`(source)` became `(source, artifact_key, generation)`.** The closure revision's record
  terms constrain `source AND artifact_key`. With two single-column indexes and no statistics
  the planner ties them and picks the source index alone, a range over every Off-limits or
  exclusion event ever logged, filtered by key. Measured at 500,000 events: 97 ms bare,
  5.0 ms with `(source)`, 11.3 ms with `(source, generation)`, 2.9 ms with the composite,
  whose plan is two exact covering seeks. Everything a `(source)` index serves, it serves.
- **No hash-based expression index.** SQLite 3.47.1 (venv and deployed node alike) has no
  built-in hash function, and an index on an application-defined function makes every
  connection that has not registered it unable to INSERT into the table (verified: `unknown
  function`), which is any script or tool that opens the file. The key is two built-in
  deterministic expressions and the read path adds the full-text equality behind them.
  `content_hash` is not used, as the design said: nothing maintains it on write.

## Measurements (scratch databases built from the engine's own DDL; probes under the
## session scratchpad, `probes/*.py` and `*.log`)

- **Exclusions.** Built digest refuses `json_size` at 5,000 tombstones of the campaign
  shape (219 bytes a row) and at one 1.1 MB note. Streamed: 60.6 ms at 5,000, 125 ms at
  10,000, 171 ms for the note; about a fifth more per row than building (48 vs 40 ms at
  4,000). Node-wide revision at 10,000 tombstones: 127 ms uncached, 0.12 ms cached.
- **Event log.** 20,000-fact merge against an empty log: 38.5 s to 0.77 s. Against 500,000
  events: 545 ms per fact without the index (272 s for 500 facts; about three hours for
  20,000), 1.22 s for the whole 20,000-fact, 2,000-mention merge with them. Identity
  lookups 50 ms to 0.01 ms each; closure record filter 97 ms to 2.9 ms; entity floor 58 ms
  to 3.0 ms.
- **Known copies.** 1.2 million rows (1.0M + 0.2M, 874 MB): 0.31-0.38 s per cited message
  scanning, 10 µs with the index; the new predicate without the index scans at 0.42 s and
  answers the same; the bare predicate with the index present still scans (the rewrite is
  needed). Inserts 7.4 to 9.0 µs a row.

## Gate

- `tests/permissions_v2` was 1933 passed at the base; see the final result in the commit
  message. New tests: `test_exclusion_store_floor.py`, `test_protection_revision_cache.py`,
  `test_bookkeeping_indexes.py`, `tests/storage/test_permissions_read_path_indexes_migration.py`.
- Mutation checks (each guard knocked out in turn, its tests must go red): 15 mutants,
  14 killed, 1 equivalent. The equivalent one removes the migration's table-existence
  guard, which the column check already subsumes; it is kept for intent. The list is in
  the commit message.

## Pre-existing on the base, not fixed here

`python scripts/sync_migration_checksums.py --check` fails at `212a0db4` already:
`owner_only_records_v1` (spec 74, ledger-guarded) is missing from
`registry_checksums.json`. Spec 76 is `always_run` and therefore outside the checksum
file, so this branch neither causes nor cures it; whoever owns spec 74 runs `--write`.
