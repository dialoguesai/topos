# Inbound claim audit history

Rechecks overwrite `truth_checks`, and Calibrate submissions overwrite the latest
label. The forward audit preserves these actions without changing verification,
model choice, timeouts, retry limits, retrieval, or label-as-evidence settings.

Two lazy tables share the existing `truth_` owner-only access rule:

- `truth_events`: monotonic sequence, stable check ID, event kind (`attempt`,
  `label`, `dismiss`, `skip`), UTC action time, versioned metadata JSON.
- `truth_evidence_edges`: event sequence and child/parent check IDs, citation kind,
  and the parent's attempt, verdict, label and timestamps as observed when the
  child attempt is persisted. Repeated identical references collapse per attempt.

The event and its state mutation commit together. Failed or duplicate writes do
not create an extra successful event. Existing checks are not backfilled. Event
sequence is an ordering key, not a count: rollbacks and erasure may leave gaps.

Attempt metadata retains existing stage timings, bounded failure classes, source,
model, engine fingerprint, reader cap, attempt number, claim-bearing status,
contradiction flag, label exposure and citation count. Missing timings are unknown;
read timing does not separate model admission, loading and generation. Engine
fingerprints identify the existing engine manifest, not every installed module.

Label events retain the previous and new values and the evidence assessment, but
never notes or claim text. `true` is explicitly `owner_testimony`; it is not an
independent corroboration or an automatic acceptance. Dismissal removes acceptance;
skip leaves it unchanged. Attempt events say acceptance is `not_measured`.

`owner_label_citation` requires an explicit citation of an active `verified_claim`
object linked to an inbound check. `source_record_reuse` means the source record
has a check; it does not prove the verifier consumed that derived claim or label.
Parent values are snapshots at persistence, not at retrieval. Unknown/unchecked
parents and standalone own-claim objects are not invented as verified parents.
Self-edges remain visible for audit; cycles require graph analysis and do not
establish causation. No edge is automatically called independent evidence.

No source text, claim text, notes or free-form error messages enter these tables.
The existing record-forgetting path deletes events for that check and edges with
either endpoint. History is retained only while privacy erasure permits it.
Object retirement alone does not prove a write failure or erase a check; consumers
must distinguish active, retired-only, and absent objects when reconciling labels.

The owner can read the tables through existing owner table APIs. Other callers
remain refused by prefix, including future truth tables. An older node without the
tables has unavailable history, not zero activity. Upgrading starts forward
coverage; rolling back leaves additive tables unread by the old code. Do not
truncate them or reconstruct past events from current rows.

This stream covers inbound checks and their Calibrate actions. It does not yet
measure all extracted claims, semantic uniqueness, automatic acceptance elsewhere,
selected-but-uncited evidence, pack-fact derivation, or cross-pipeline causal
provenance. No accuracy improvement is claimed from this instrumentation.

## Validation

Synthetic regression tests first reproduced missing attempt/label history and
lost stage timings. Tests cover duplicate/retry writes, label transitions,
immutable parent snapshots, self-citations, owner access, legacy table creation,
erasure and rollback. No live data was rechecked and no model was called.
This is persistence/measurement work, not a response-quality behavior change;
paired answer-quality/live-model gates are not claimed. Full release CI and
exact-tree sharing gates remain required before release.
