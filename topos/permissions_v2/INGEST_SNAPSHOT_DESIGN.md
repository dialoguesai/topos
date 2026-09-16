# Owner-attested snapshot ingestion — beta implementation

The first provenance lane is an explicitly owner-attested, immutable iMessage
snapshot. It does not infer Apple account ownership from a path, OS user, dataset
name or native `is_from_me` bit. It does not change legacy sync or backfill any
historical owner fields. Both node and CP feature flags default off.

## Authority and flow

1. The operator provisions a closed, private snapshot under the paired node's
   fixed `permissions-v2/ingest-snapshots` directory. No request accepts a path.
2. The owner describes a snapshot ID and sees its SHA-256 and size. Enrollment
   requires that exact digest, an explicit dataset and the ownership attestation.
3. The frontend's verified owner identity authorizes a separate signed command
   containing the complete request and node/owner/resource/environment binding.
   The engine requires real owner UDS authority or the verified CP relay principal
   as well as the signed command. Ordinary HTTP keys and headers cannot elevate.
4. Enrollment, queued job, random worker claim and per-record origins persist in
   the canonical database. A job loads its authority from this state, never from
   imported fields. Old local-sync jobs cannot acquire it.
5. The worker reads only pinned snapshot bytes. One bounded canonical transaction
   preflights every identity, inserts new parents/messages/origin links and marks
   the job done. Before it marks the job done, the same transaction runs the
   rules fact floor over the rows this job linked (below). A late error rolls
   the whole batch back, facts included. There is no other enrichment, no LLM
   pass, raw staging, legacy checkpoint or implicit global database connection.
6. Native self and correspondent messages share the verified dataset owner but
   retain distinct authorship. An owner evidence review cannot promote a native
   correspondent to owner-authored evidence.
7. Each new row carries a reserved origin hint backed by its durable record link.
   Evidence qualification revalidates the enrollment, completed job, exact row,
   source generation and immutable snapshot. Revocation withholds existing
   reviewed outputs. Removing the hint does not permit a new review to bypass
   a known origin. Missing provenance state fails closed for conversation
   evidence once this node has enrolled the lane.

## Storage and recovery

The canonical tables and exact source-clock trigger schema are paired with a
private external marker containing the node identity, the durable canonical
identity (resource binding, protection clock identity and exact path; never
device/inode numbers, which a bind mount renumbers across a VM restart), a
source-generation floor and a digest of all provenance authority rows. The marker detects replacement
and in-place rollback of canonical state, including consumed commands and revoked
enrollments, across process restarts.

Each mutation publishes a durable pending marker before the SQLite commit, then
an active marker after commit. Either interrupted ordering leaves the service
closed. Missing tables, triggers or markers are never silently rebuilt. Recovery
of a pending marker requires an explicit reviewed recovery procedure; no automatic
repair is implemented. Restoring both canonical storage and its external private
authority floor is outside this rollback guarantee and must not be used to revive
old grants, commands or enrollments.

Exact enrollment retries recover the original active or revoked metadata without
resurrection. Different source bytes cannot reuse that dataset.
Enqueue is idempotent per enrollment. Failed or expired jobs can be explicitly
run with a new random claim; a stale worker cannot write or fail the newer claim.
Completed jobs cannot repeat. Status is a signed, read-only recovery operation.

The source clock conservatively invalidates enrollment after native source
configuration changes, including changes followed by reversal. An idempotent
startup write of the same owner, or unrelated UI configuration, does not do so.
Changing a source setting can require a fresh dataset/enrollment; this first
lane has no automatic reauthorization or provenance migration.

## Current supported subset

- Snapshot: regular file, one link, mode `0400`, no symlink ancestors or journal
  sidecars, SQLite DELETE journal header, at most 16 MiB. Its durable identity is
  the exact bytes; device/inode are compared only within one read. The digest is
  checked again before and after the canonical batch.
- Native reader: at most 1,000 ordinary plain-text messages, 64 KiB per message,
  1 MiB total text, one unambiguous chat join, literal integer `is_from_me` 0/1,
  exact sender identity for correspondents.
- Time: fixed modern Apple nanoseconds since 2001, microsecond-representable
  subset, exact UTC. Missing, ambiguous, future or unsupported times withhold;
  ingestion time is never substituted for source event time.
- Attachments, attributed bodies, reactions, replies, deleted/system forms,
  ambiguous native joins and unsupported schemas reject the entire snapshot.
- Historical unlinked rows with matching identity are skipped unchanged. Any
  conflicting source, dataset, conversation, source record or owner rejects all
  writes. Linked immutable replay must match the actual canonical row exactly.

This is **owner-attested snapshot provenance**, not provider-verified live account
ownership. Live iMessage account enrollment, Signal keys/accounts, generic uploads,
larger resumable corpora and additional output families remain separate work.
Classification, owner-only/black-hole protections, disclosure ceilings, lineage,
owner reviews and recipient grants remain independent required release checks.

## Owner facts from the lane (step 4)

This lane is the only producer that writes fact references complete enough for
the p2b evidence chain: `{table, record_id, source_id, dataset_id}`. The shared
extractors, their `_source_ref` and every loader stay unchanged. They also serve
legacy sync, the shared-key Signal upload, reprocess and backfill, so widening
them would give a forged or legacy row a complete reference.

`IngestProvenanceService.derive_owner_facts` runs inside the job's batch, after
the rows and links are written and before `finish`
(`ingest_snapshot_facts.py`):

- **Rows.** Only rows linked to this enrollment, revision and job, in this
  dataset and source, re-checked against their stored identity, in event-time
  order. A changed identity raises and rolls the batch back.
- **Rules only.** `extract.extract_rules_facts`, which never imports the LLM
  pass, never infers a table, and never chooses or creates a self entity. The
  owner-authored gate is unchanged, so a native correspondent's message
  produces nothing.
- **Subject.** The one entity that is both `is_self` and actively attested by
  the owner (`identity.attested_subjects`). With none or more than one,
  nothing is extracted. The shared extractor's own choice (the self row with
  the most facts, or a new `Owner` row) is exactly what the attested contract
  refuses. The owner therefore attests identity before running the job.
- **Labels.** A value that fails `fact_contract.atomic_label_syntax` is not
  written, so no unreleasable scoped owner fact is minted.
- **Time.** Each fact's `topos-fact-temporal/v1` evidence is the linked row's
  `event_at` as `native_source_clock`. The store receives a `LinkedRowTrust`,
  so its supersession guard can order evidence from rows this job, or an
  earlier still-valid enrollment, proved (`features/temporal/TEMPORAL_FIELDS.md`,
  reader 1). The trust also gives the ceiling the guard checks each time
  against: the moment the owner attested that snapshot's bytes, which no
  message in it can postdate. An older statement enrolled after a newer one is
  kept as history instead of bringing the old value back; a revoked enrollment
  stops vouching, so its rows no longer hold anything back. The trusted writer's column tuple and `_record_identity` are
  unchanged, and the lane writes no `event_time_json`.
- **Result.** The signed job result keeps its exact shape: `finish` and the
  signed acknowledgement pin those fields, and the control plane mirrors them.
  Facts are found through the owner's fact reads, not the result.

Limits the owner can hit, all fail-closed and none visible in the signed result:

- **Order matters.** Extraction reads the attestation when the job runs. A job
  run before the owner attested produces no fact, and a completed job cannot run
  again, so recovering needs a new snapshot and dataset.
- **A run that derives nothing still succeeds.** No attested self, an identity
  read error, a refused label and a queued conflict all return the same result
  as a derived fact. The counts exist (`derive_owner_facts` returns them) but
  the signed result cannot carry them without a protocol version.
- **A stronger incumbent wins.** A message statement is asserted at 0.6; a
  resume fact is 0.9. Belief revision's confidence margin queues the lane's
  value as a `fact_conflicts` row and keeps the incumbent, so the lane's fact is
  never active and never releasable until the owner resolves the conflict.
- **Discovery.** The control plane's `/facts/capabilities` profile still lists
  only p2b-v1 to v3 (changing it breaks the frontend's key-count check), so a
  client cannot discover p2b-v4 there even though activation accepts it.

Known availability limit: if the legacy extractor later runs over the lane's
own rows (a reprocess or backfill), it asserts the same owner fact with a
dataset-less reference. The store's refresh merges that reference into the lane
fact, which then withholds as `lineage_identity_incomplete`. It fails closed
and never qualifies with an unproven leaf. A separate fact key for lane facts
would avoid this, but it would also split the owner's single-valued belief in
two, so it is left as a decision for when the lane runs continuously.

## Regression evidence

Tests cover actual UDS and signed relay identity boundaries, SHA mismatch, strict
native parsing, imported metadata spoofing, exact batch rollback, native author
separation, historical preservation, stale claims, source/owner ABA, revoked
enrollment, removed origins, old database restoration, replay, both crash orders
and uncertain-ack recovery. Fixtures contain synthetic data only.
