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

## ChatGPT lane (`chatgpt-owner-snapshot/v1`)

Owner decision, 16 Sept: AI-chat prompts the owner typed count as owner-authored
evidence. Before this lane nothing could prove that. app_ingest, store_message,
start_ingestion and source install/test can write an `ai_chat_messages` row with
`sender_type` "human" under the owner's conversation (a conversation's owner is
only the dataset id prefix, and app_ingest defaults a missing role to "human");
the rows carried no writer provenance; and the upsert replaced a stored body while
keeping its role and conversation. The owner's own ChatGPT extension uses
app_ingest too, so a door's identity is not proof either.

**Selection and wire.** Describe and enroll name `reader_contract`
`chatgpt-owner-snapshot/v1` with `source_id` `chatgpt-owner-snapshot`; revoke,
enqueue, status and run name the lane by `source_id`. The iMessage contract is
the default and never travels: its commands are byte-identical to before (the
golden fixture still verifies), and an explicit `reader_contract` naming it is
refused rather than normalized, because both ends sign `model_dump()`. A command
for one lane cannot see the other lane's enrollment or job
(`ingest_enrollment_unknown`, `ingest_job_unknown`), and each runner claims only
its own lane's jobs. The control plane mirror carries the same models, so a CP
sends the field only for the ChatGPT lane and an older node's closed model
refuses that command instead of misreading it.

**Durable state.** Same doors, owner-only signed commands, enrollment, durable
job, marker file and revocation as iMessage. The provenance tables are shared and
their schema is unchanged, so an installed iMessage ledger keeps its schema digest.
The pinned snapshot descriptor already records `reader_contract`; a link's lane
(source id, attested sentence, `<id>.json` file, canonical table) is read from its
enrollment's descriptor, never from the row. An AI-chat link's row identity is the
stored row (role, sender, time, sequence, source, source record, actor role, origin
marker), a SHA-256 content revision of its body, and its one parent conversation's
id, `owner_user_id` and source. Moving the row, re-binding the parent or replacing
the body stops the link from matching. Ingestion time and sync batch are excluded.

**Closed reader** (`topos/ingestion/chatgpt_owner_snapshot.py`). One
`conversations.json`, at most 16 MiB, 1,000 conversations, 20,000 mapping nodes,
1,000 emitted rows, 64 KiB per body and 1 MiB of text. Only the active branch
(`current_node` ancestry) is read. An owner prompt (`sender_type` "human",
`actor_role` "authored") is a visible, finished `user` node with no author name
or author metadata, `text` or the text parts of `multimodal_text`, message
metadata inside a closed allow-list, and no set message or mapping-node field
beyond the ones an ordinary export carries (a group chat must record each sender
somewhere, so that place is closed too). Hidden and custom-instruction nodes, canvas,
automation, scheduled-task, starter-prompt and targeted-reply metadata, and any
key the reader does not know, drop the prompt; its reply is kept. Assistant text
is kept as `sender_type` "assistant" (`actor_role` "addressed"); system, tool and
non-text nodes are dropped. If any user node on any branch names an author,
carries author metadata or a participant/shared-link marker, or the conversation
carries one, the whole conversation is withheld: a group chat or a continued
shared link cannot show which prompts the owner typed. Time is `create_time` read
as an exact decimal of seconds since the Unix epoch: 2022 or later, microsecond
representable, not in the future and not out of order along the branch.
Duplicate keys, non-finite numbers, inconsistent ids, broken parent/child links,
unknown roles and malformed content reject the whole snapshot. Ids are
`chatgpt-owner-snapshot:<conversation>:<node>`; an existing row or conversation
with a lane id refuses the whole batch.

**Evidence.** An `ai_chat_messages` leaf is owner-authored only when its
`sender_type` is "human" or "user", the row, its identity and its one parent
carry `chatgpt-owner-snapshot`, the parent's owner is the binding owner, and
`_validate_native_origin` proves a live link whose identity (content revision
included) matches the row. Any other AI-chat row is `not_owner_authored`,
including legacy "user" rows that qualified before: this is deliberately
stricter. Assistant rows are never owner speech. The quote/metadata veto, posture,
copies and every other release check are unchanged. A tagged or linked row whose
proof fails, including after revocation, withholds as
`native_owner_provenance_unavailable` at inspection, as for iMessage.

**Store guards.** The shared `ai_chat_messages` upsert leaves a row that holds a
provenance link untouched except its sync batch and ingestion time, so no door can
rewrite a proven prompt's body, source or marker. The conversation upsert refuses a
write naming a different `owner_user_id` for an existing conversation. Unlinked
legacy rows upsert exactly as before.

**No facts.** The lane derives no facts; a p2a locator over a prompt must come
from elsewhere (the canary seeds one). `ingest_snapshot_facts` would generalize
at the query, but the supersession guard would not: `features/facts/evidence_time.py`
orders only `conversation_messages` references that name a dataset and an
`is_from_self` row, and an AI-chat reference carries neither, so ChatGPT facts
would lose the evidence-time ceiling the iMessage lane relies on and an older
prompt enrolled later could bring back an old belief.

Known limits, all fail-closed:

- Re-exporting after new chats needs a fresh dataset, and the overlapping
  conversations collide with the earlier enrollment's rows, so the whole later
  snapshot is refused. There is no incremental or merge path.
- The prompt allow-list is conservative. A real export whose prompts carry
  metadata this reader does not know drops those prompts.
- `CanonicalTablesManager.update_message_sequences` (legacy canonicalizer)
  renumbers every row in a conversation. A legacy write into a lane conversation
  id can therefore change a lane row's `sequence`, which withholds its proof and
  stales its review. The ChatGPT UI mapper that app_ingest uses prefixes its
  conversation ids (`chatgpt:`), so reaching a lane conversation needs a writer that
  does not; the renumbering itself is not guarded here.
- Identical prompt text anywhere in either leaf table is an independent copy, so
  a prompt the owner also sent through the extension does not qualify.

Known limit that is NOT fail-closed: every place a message records its sender
(author, metadata, message and node fields) is closed, but the conversation-level
participant and shared-link markers are a deny-list (`_PARTICIPANT_MARKERS`), and
no marker name was checked against a real export. A group chat or shared-link
continuation marked only by a conversation-level key this reader does not list,
whose other members' prompts carry no sender at all, is read as the owner's
prompts (a synthetic `members` roster does exactly that); so is a creator-written
GPT starter or suggested prompt that no field marks. Such a row is linked and
passes the evidence proof, and only the owner's attestation and per-leaf review
stand between it and a recipient. Check a real export's group chat, shared-link
continuation and GPT conversation shapes before enrolling one.

## Regression evidence

Tests cover actual UDS and signed relay identity boundaries, SHA mismatch, strict
native parsing, imported metadata spoofing, exact batch rollback, native author
separation, historical preservation, stale claims, source/owner ABA, revoked
enrollment, removed origins, old database restoration, replay, both crash orders
and uncertain-ack recovery. Fixtures contain synthetic data only.
