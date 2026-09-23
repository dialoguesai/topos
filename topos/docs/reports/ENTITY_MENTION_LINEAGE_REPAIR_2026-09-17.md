# Entity-mention lineage: writer fixes and the on-disk repair

Branch `fix/entity-mention-lineage` (cut from `origin/main` at 1.3.57, c026bd52).
Status: **local branch, not pushed, not merged.** Owner's call on when it ships.

## Why

`entity_mentions(record_id, entity_id, canonical_table)` is the lineage a
per-record Off-limits exclusion would travel along (SCALABLE_GRANTS_DESIGN §3.4,
decision D8). The 17 September count-only measurement on the quarantined copy
(`CORPUS_MENTION_LINEAGE.md`, beta lineage) found it could not be certified:

| defect | measured | cause found in the code |
|---|---|---|
| mentions with no `canonical_table` | 17,203 of 33,286, all resolving to `conversation_messages`, all written after migration 71 | local sync (iMessage, Signal) passes enrichment message dicts that name no table; the job wrote whatever it was given |
| records extracted into `message_entities` and never linked into the spine | 11,637 `conversation_messages`, 2,751 `ai_chat_messages`, 894 `activity_events` | `message_entities` and the spine link were written in different transactions on different connections; the spine half was wrapped in `try/except` that logged and moved on |
| stamps that disagree with the row | 98 stamped `journal_entries` living in `location_events`; 7 stamped `ai_chat_messages` citing no row | fan-out children stamped with the parent's group before the 2026-08-27 stamp fix; orphaned ids |

## What changed (writers)

- `topos/features/entities/mention_lineage.py` — the record-kind → table map
  (`canonical_table_for_record`, in the shape of `embed_context`'s dimension
  map), `require_canonical_table`, `MentionLineageError`, and the repair.
- `EntityResolver.record_mention` refuses a mention without a table.
- `EntitiesJob.write_derived` writes `message_entities` and the spine link
  under one `batched_writes` hold; both orchestrator lanes call it; a spine
  failure rolls the NER rows back and is reported as the job's error;
  mentions whose record names no table are refused from both tables.
- `DerivedTablesManager.write_message_entities_rows` — the gate-free row
  writer the hook uses.
- `local_sync.stamp_conversation_table` stamps records before enrichment.
- `batched_writes` nested on the same connection defers to the outermost hold.

## The repair

`repair_mention_lineage(conn, dry_run=False)`:

1. **stamp** — unstamped mentions whose `record_id` is in exactly one canonical
   table get that table (same rule as migration 71, wider table list).
2. **re-stamp / quarantine** — a stamp whose table does not hold the id is
   replaced when the id is in exactly one other table; a mention whose id is in
   no table moves to `entity_mentions_quarantine` (every column kept, reason
   recorded); two answers leave the stamp alone. Entities are recounted.
3. **relink** — every `message_entities` row with no mention for its record
   gets one when its surface resolves to an EXISTING entity through the exact
   tiers (identifier, contact, name, alias). No fuzzy tier, no minting; the
   writer's confidence floor and value-label drop apply; owner tombstones and
   unbinds are honoured.

Doors: manifest step `repair-entity-mention-lineage` (`derived_rebuild`,
target `entity_mention_lineage`, consent auto) and
`python -m topos.features.entities.mention_lineage [--dry-run]`.

Not a numbered migration: the beta permissions lineage already holds 74–76
unpushed (`owner_only_records_v1`, `temporal_fields_v1`,
`permissions_read_path_indexes_v1`); a main-based 74 would collide, and a
checkout carrying it would fence the installed node out of the live database
the moment it opened it.

## How it ships — read before doing anything with the live node

1. **Stopped-node upgrade lane only.** Stop the node, upgrade the package,
   start it; the upgrade runner executes the step on first boot and ledgers
   it. Do not run the CLI from a development checkout against
   `~/.topos/database.db` while the installed node is running.
2. **Re-measure before D8's floor is narrowed.** Run
   `scripts/permissions_beta/corpus_mention_lineage.py` (beta lineage, lab
   CP venv, against the quarantined copy or a fresh copy taken after the
   upgrade). The global Off-limits refuse stays until `conversation_messages`
   stamp coverage reads 1.0 and the extracted-but-unlinked count is 0 for rows
   whose surfaces resolve. Rows the exact tiers cannot resolve stay unlinked
   by design, and the name scan stays a required second instrument.
3. The 15 September lab work (`beta-permissions-lab/*`,
   `scripts/permissions_beta/*`, node P) was not touched by this branch.

## The stopped-node lane (2026-09-18)

`python -m topos.features.entities.mention_lineage_lane --database PATH
[--dry-run] [--hash] [--report FILE]` runs the same repair on a named file with
no node attached — a stopped node's database or a copy. It opens the file raw
(no migration runner; `user_version` is read before and after and must not
move), refuses while a `-wal`/`-shm`/`-journal` sidecar exists, refuses a path
under `~/.topos` without `--node-stopped`, opens a dry run `immutable`, and
leaves no sidecars behind. Its report is counts only: the repair's counters,
per-table coverage before and after (`lineage_coverage`), and every
extracted-but-unlinked row classed by the writer's own filters
(`extracted_unlinked_breakdown`).

Run on a copy of the quarantined corpus the 17 September measurement used
(`0aea9d43…`): 17,203 stamped, 98 re-stamped, 7 quarantined, 99 linked;
`conversation_messages` stamp coverage 1.0; a second run wrote nothing and the
file hash did not change. The extracted-but-unlinked rows did not move and are
not a link gap: not one of them holds a span that passes the writer's filters
(value types only, or named spans below the 0.60 floor, or invalid surfaces).
The re-measure and the D8 thresholds are in
`audits/2026-09-14-permissions/MENTION_LINEAGE_REMEASURE_2026-09.md` (control
plane tree).

## Tests

- `tests/features/test_mention_lineage_stamp.py` — derivation, refusal,
  structured-field writer; the mutation guard for the stamp requirement.
- `tests/enrichment/test_entities_job_lineage.py` — atomicity (a spine
  failure rolls the NER rows back), refusal from both tables, both lanes,
  local-sync stamping.
- `tests/features/test_mention_lineage_repair.py` — the three passes, dry
  run, idempotency, CLI, upgrade target, manifest step.
- `tests/storage/test_write_gate.py` — nested-batch semantics.
- `tests/features/test_mention_lineage_lane.py` — the stopped-node lane:
  refusals, no migration, no sidecars, interruption and resume, coverage
  arithmetic, the unlinked-row classes, the `browser_visits` id column, and the
  indexed person lookup against a linear scan.
