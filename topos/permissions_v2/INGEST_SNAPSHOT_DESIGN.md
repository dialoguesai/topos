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
   the job done. A late error rolls the whole batch back. There is no enrichment,
   raw staging, legacy checkpoint or implicit global database connection.
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

## Regression evidence

Tests cover actual UDS and signed relay identity boundaries, SHA mismatch, strict
native parsing, imported metadata spoofing, exact batch rollback, native author
separation, historical preservation, stale claims, source/owner ABA, revoked
enrollment, removed origins, old database restoration, replay, both crash orders
and uncertain-ack recovery. Fixtures contain synthetic data only.
