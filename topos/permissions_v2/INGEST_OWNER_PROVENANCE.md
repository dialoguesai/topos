# New-ingestion owner provenance, investigation v1

Status (2026-09-15): the initial-self creation race below is repaired. A separate,
default-disabled [immutable snapshot lane](INGEST_SNAPSHOT_DESIGN.md) now implements
explicit owner attestation, signed commands, new-row provenance and revocable
evidence checks. The live-sync binding described below remains proposed; native
account identity is not established by existing readers. This is not a backfill.
The copied corpus remains unchanged. Its
zero current `prefers` intersection is a valid narrow-contract result, not proof
of data corruption. Source-event time and fact applicability precision remain a
separate investigation.

## Current producer findings

`ingestion/local_sync.py::_run_imessage_sync_impl` builds canonical staging rows
without `owner_user_id`. `ConversationsTablesManager.upsert_message_batch` reads
that optional field as supplied, and `SQLiteCanonicalStore` inserts its null
value. This is an ongoing producer omission, not just an old migration artifact.
A current re-ingestion changes batch/ingest bookkeeping and may heal content,
but does not overwrite the existing ownership field. That behavior does not
repair the 94,753 copied conversation rows whose owner field is SQL null.

The generic `canonical_pipeline.build_staging_record` independently omits
`owner_user_id`, `from_self` and `is_from_self`, even when present upstream.
The messenger mapper also omits those fields, and generic post-canonical output
currently drops sender/self provenance. Simply forwarding arbitrary imported
fields would be unsafe: the upload/direct-ingest payload is not trusted proof
of dataset ownership or of who wrote a message.

Dedicated Signal staging does carry an owner value, but local/export fallbacks
can substitute the dataset ID. `ingestion.manager._owner_user_id_from_dataset_id`
uses a colon prefix elsewhere. Sync routes accept the dataset from their caller.
Neither string convention is an authenticated owner binding. The next package
must not extend these fallbacks into a permission proof. Display-name storage in
`storage/user_identity.py` likewise is not ownership evidence.

Dataset ownership and message authorship are different axes. A verified owner
may ingest messages written by somebody else. Canonical owner metadata cannot
promote `is_from_self`, change an explicit actor role, lift ambient source posture,
approve a fact's subject or override an exclusion.

## Self identities are not globally unique

The native entity model intentionally permits multiple `is_self` rows. Contact
seeding creates an entity per contact and copies that contact's self marker.
`features/entities/owner.py` selects a deterministic fact-bearing self entity and
exposes a plural helper for callers that need all self identities. The current
P2b resolver deliberately requires one unambiguous self entity. An unavailable
P2b owner-subject proof therefore does not establish that the native model is
corrupt or that the owner made no statements.

Do not replace the P2b check with the native helper merely to obtain a positive.
Its ordering is useful retrieval behavior, not authenticated proof that every
self alias is interchangeable for authorization. Any future explicit owner/entity
binding needs current owner-authenticated provenance, durable revision and
invalidation, all protected aliases preserved, and a separately agreed capability
contract. No entity merge, deletion, `is_self` rewrite or historical fact rewrite
is included here.

One separate producer race was concrete: `facts.extract._owner_entity_id` selected
an initial self entity before entering the write gate, then created one without
checking again. Two connections that both observed no row each created a new
entity. The fix repeats the exact existing selector under the same process write
gate as creation/commit. It keeps the normal read path and existing fact-count/
entity-ID ordering unchanged. The guarantee covers cooperating writers within the
existing single-node process; it is not a new cross-process database uniqueness
constraint. Existing multiple self rows stay intact.

A deterministic two-connection barrier regression failed with two self rows on
the old code and passes with one shared ID after the fix. Additional positives
verify reuse and preservation of all existing self rows plus fact-bearing
selection. No source/corpus/model access is involved.

## Proposed bounded authenticated owner-binding package

Begin with the native local iMessage/Signal job lane. At enqueue, authenticate an
actual owner operation and bind its source and dataset to the node owner. A
verified owner-app CP relay stamp must match the canonical owner; direct local
owner-key authority must arrive over the real UDS channel. A payload role,
`owner_user_id`, native-app header, dataset prefix, shared key or later worker
principal is not sufficient. Read the canonical owner from the same explicit node
connection; reject missing, ambiguous or conflicting identities.

Persist a server-created immutable attestation alongside the job, not a
recipient/imported field that the worker is invited to trust. Proposed closed
attestation fields are:

```text
version: ingest-owner-binding/v1
job_id, node_instance_id, canonical_file_incarnation
owner_id, source_id, dataset_id
owner_operation_id, authenticated_channel, authorized_at
source_enrollment_revision, binding_revision
```

These fields alone are not proof. Only the verified enqueue service may create
the durable record. It must bind the exact server-selected job and approved
native source enrollment in one transaction; the worker loads it independently
from trusted storage and compares it to the current node/source/dataset and job.
Do not accept an attestation object from job options or uploaded JSON. The exact
store schema and source-enrollment proof must be designed before implementation.
A historical queued job without this record cannot acquire owner provenance by
being resumed. Request-token expiry and durable authorization of a long-running
owner job are separate concepts; retain an auditable owner operation rather than
pretending its old access token is still active.

For new rows, pass an explicit trusted ingest context through the worker and
canonicalizer. Stamp canonical ownership from that context. Payload ownership is
ignored as authority and conflicting hints are rejected/reported through the
owner operation. Preserve native sender/self metadata only through its actual
registered reader/parser contract. Downstream enrichment receives those same
canonical ownership/authorship fields; it must not reconstruct them from sender
labels or a missing field.

No generic default should label every local DB row as owned by the current node.
No old null field is filled on re-sync, and no existing non-null owner is changed.
A message-ID collision with another dataset/source/owner must be rejected or
withheld before altering its body, not treated as a successful new provenance
stamp. Historical repair requires a separate evidence/migration plan. Missing
new attestation may retain an explicitly unproven record if the chosen ingestion
contract supports that; it must never create a trusted owner field or successful
attribution receipt.

## Exact integration seams

| Codepoint | Required future change |
| --- | --- |
| `api/ingestion_sources.py::sync_source`, `core/handlers/sources.py::handle_source_sync` | Obtain verified owner context from the authentication fabric; no caller owner field; bind source/dataset/node |
| `ingestion/local_sync_jobs.py::enqueue_local_sync` | Create immutable durable attestation with the new job; no attachment to an unproven old active job |
| `pipeline/job_runner.py::_execute_local_sync` | Load and validate the stored attestation before native source access; fail closed on stale binding/restart/source change |
| `ingestion/local_sync.py::run_imessage_sync`, `_run_imessage_sync_impl`, `run_signal_sync` | Receive trusted context explicitly and stamp only newly ingested rows; eliminate dataset-string inference in this attested lane |
| `local_sync.py::run_signal_upload`, corresponding HTTP/relay upload handlers | Separate later upload contract; owner authentication alone does not validate imported authorship claims |
| `canonical_pipeline.build_staging_record`, `canonicalize_normalized_batch`; `mappers/messenger_mapper.py` | Separate trusted ownership from untrusted payload; preserve registered native role/self fields and post-canonical provenance |
| `ConversationsTablesManager.upsert_message_batch`, `SQLiteCanonicalStore._upsert_conversation_message` | Require consistent context for new trusted ownership; preserve old fields; guard full-identity collisions before any update |
| `ingestion/manager.py`, `ingest_helpers.py`, `reprocess.py` | Remain unproven unless explicitly given a validated trusted context by their authenticated entry service; no dataset-prefix upgrade |

The future regression gate must cover actual HTTP/relay authentication, a signed
CP owner versus same-owner third-party client, UDS versus TCP/header spoofing,
exact owner/source/dataset/node mismatches, raw-payload spoofing, old/resumed jobs,
restart and source revocation, new non-self messages, ambient sources, ID
collisions, metadata survival through canonical/enrichment stages, and unchanged
historical rows. Positive tests must use an actual verified enqueue path, not a
constructed `trusted=True` object. Native sender attribution and current
permissions remain independent checks.
