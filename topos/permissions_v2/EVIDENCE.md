# First-family evidence qualification (P2b)

`evidence.py` implements a node-local qualification step for the
`owner_stated_fact/v1` family. It does not register an executable data form, issue
a grant, run a natural-language evaluator, or return fact/source contents. Every
qualified result explicitly carries `execution_enabled: false`.

The separate `evidence_reviews.py` owner service now supplies disabled-by-default
preview/read/review/revoke handlers. `QualifiedEvidence` itself remains processing
metadata; another adapter must independently authorize any executable output.

## What can qualify

An existing fact may qualify only when all of the following hold in a fresh
canonical SQLite snapshot:

- Its existing payload disclosure is exactly `scoped`. Missing, unknown and
  `owner_only` disclosures are withheld. An owner review never changes disclosure.
- The fact is about the uniquely identified owner and asserted by the owner.
  Existing actor-role and altitude representations must agree with that claim.
  A payload saying `stated` cannot override a row saying `inferred`, or vice versa.
- Every recursively referenced fact meets the same conditions. The recursive
  graph resolves without cycles, missing/deleted rows, duplicate references,
  ambiguous identities or exceeded bounds (128 records, depth 16).
- Every terminal source is an owner-authored canonical message. Conversation
  messages require native `is_from_self=1` and matching `owner_user_id`. AI messages
  require `sender_type=user` plus the exact source/conversation parent belonging
  to the owner. The parent row's revision is bound into the evidence revision.
- An explicit owner review covers every fact and terminal source at its current
  complete row revision. Every reviewed classification states owner authorship,
  direct self-statement, only owner subjects, known sensitivity and nonempty
  domains. Mixed subjects, quotations, unknowns and known independent copies
  withhold the entire candidate.
- Neither a record protection nor an unresolved entity protection can apply.
  Record protection is checked before reading that record. `owner_only` is checked
  before traversing a fact's sources. Since complete entity-mention lineage has
  not been certified, any entity protection conservatively withholds this family.

Native `FactStore.assert_fact` writes `extractor_version=fact_store_v1` and omits
altitude. That exact writer's absence (including a nullable schema column) may be
completed by the current owner review and independently checked native source
authorship. Other writers need an explicit `stated` representation; any explicit
unknown/inferred/null payload altitude withholds. This is a narrow compatibility
rule, not an inference from a pack name. Legacy `verified_by_owner` flags also
provide no qualification authority.

The owner review is a human semantic attestation. Native metadata can contradict
and veto it, but the resolver does not claim to prove that arbitrary prose is
about the owner, or that an owner's classification is truthful. Known quote and
forwarding metadata is rejected independently of the review.

## Identity, sets and revision binding

Let `R(f)` be the transitive closure of a candidate fact's references, split into
derived facts `D(f)` and terminal messages `L(f)`. A review must classify exactly
`D(f) ∪ L(f)`, including `f`; no first-source, first-row or partial-lineage fallback
exists. Qualification is a conjunction over the entire closure, not a union of
the sources that happened to pass.

Every identity contains a runtime-pinned environment, node, resource and owner.
Conversation leaves additionally require table, source, dataset and record IDs.
AI messages have no canonical dataset column, so they explicitly use
`dataset_kind=node_resource` with `dataset_id=null`; conversation labels are never
borrowed. Derived fact identity uses its table/record within the same node/resource,
with source identities supplied by the recursive references rather than fabricated.
Unregistered leaf tables, partial references and contradictory node/resource IDs
are withheld.

Every row revision hashes all SQLite columns, including exact legacy JSON text,
timestamps and tagged finite SQLite float values. The snapshot additionally binds
the complete graph, canonical file device/inode identity, and the actual canonical
protection clock/floor. Any changed candidate, source, parent or recursive edge
invalidates the review. A protection change invalidates it even if protection is
later lifted and the visible floor returns to its earlier shape.

## Owner review storage and trust

`EvidenceReviewStore` is a separately configured private SQLite service dependency;
`qualify` never accepts a request-supplied review or a `reviewed=true` flag. Creating
or revoking a review requires a verified `OWNER_APP` principal over `uds` or signed
`cp_relay` with `acting_user` equal to the pinned node owner. Owner class alone,
the same actor under a third-party class, and TCP headers are insufficient.

The store requires an absolute path, a private file owned by the process user,
and no symlink anywhere in its parent path. It pins the review file's device/inode,
canonical file identity and persisted resource identity, checking them on every
open. Replacing either database or tampering with the stored identity withholds.
The store persists its observed protection-clock ID and highest generation; a
seen rollback is rejected during use and across a normal service restart. A new
clock is not silently accepted. Review IDs are immutable and cannot be replayed
after revocation; one current review per fact is loaded authoritatively.
First enrollment requires an exclusively created new file. Missing schemas or
identity/clock metadata in an existing file are never silently recreated.

This protects the service against copied/replaced databases, configuration mixups
and stale state. It is not tamper-proof storage against a host administrator who
can rewrite trusted files, code and all durable high-water state together. Backup
restore/re-enrollment needs an explicit lifecycle procedure before deployment.

## Copies and limits of this family

The resolver rejects exact independent message-content copies across both supported
canonical message tables and normalized duplicate active fact claims. It also
requires an owner attestation that no other independent copies are known. These
checks do not certify semantic deduplication, paraphrases, copies in unsupported
tables, or complete lineage across the wider ingestion graph. The existing
provenance stubs do not supply that proof. Unknown copies remain a withholding
condition; this family is not a general declassification mechanism.

The audited baseline of 200 pack facts with `owner_only` disclosure remains
unqualified, including the ten with fully populated references. The regression
suite reconstructs that aggregate shape using synthetic facts; no owner corpus
content is embedded in source or test fixtures.

## Integration boundary

`inspect_for_review(fact_id)` is owner-only and produces metadata for an explicit
review. `record_review(...)` compares that inspection with a fresh snapshot before
persisting it. `qualify(fact_id, reviews=trusted_store)` returns either a bounded
withholding reason or `QualifiedEvidence` containing only snapshots, classifications
and review hashes/IDs. Classification labels are owner-authored labels, not an
automatically trusted global scope vocabulary.

The result is private processing metadata, not a recipient-readable proof or an
executable form. Ordinary typed Python objects can be constructed or mutated by
in-process code; a future adapter must call the trusted resolver itself and must
not accept serialized qualification from a recipient. Before any release, a
certified adapter must separately establish policy/evaluator assignments, allowed
input use and output release, registered output form, vocabulary mappings, and
fresh final authority/evidence checks. Node final checks plus CP final forwarding
checks must enforce cancellation/revocation; this module makes no immediate
cross-service revocation claim and mounts no query route.

## Owner review service and wire contract

The four owner-only relay types are `permissions_v2_evidence_preview`,
`permissions_v2_evidence_review_read`, `permissions_v2_evidence_review_record`, and
`permissions_v2_evidence_review_revoke`. Every payload has exactly
`{binding: EvidenceBinding, request: <request model>}`. The CP must inject the
configured binding, never forward a recipient-selected one. Engine handlers check
the exact owner principal and full node/resource binding before opening the store
or returning content. Both UDS and signed CP relay require an explicit matching
`acting_user`; client headers confer no authority.

Authoritative request/response JSON schemas live in
`fixtures/permissions_v2/evidence_reviews/`. A successful handler's `payload` is
the corresponding response model directly. Preview includes exact SQLite cells:
null has its own tag; integers use decimal strings, floats use `float.hex()`,
blobs use lowercase hex, and text is preserved verbatim. This representation
does not relax the policy contract's prohibition on JSON floats. Preview is capped
at 512 KiB and does not truncate an oversized row into reviewable evidence.

Owners can inspect existing owner-only content without changing its disclosure.
Incomplete lineage returns only the root record and a bounded reason, with
`snapshot=null`; the UI must not offer a review of a nonexistent complete snapshot.
Every new classification remains an explicit owner decision. Neither legacy
confirmation nor viewing the preview is review consent.

Recording requires the exact inspected snapshot and the expected current review
hash (or explicit null when no current review exists). Timestamps come from the
server. Retrying the identical active review ID/content returns the original
review, including its timestamp; changing its contents or replaying a revoked ID
conflicts. Revoke binds fact ID, review ID and review hash, so an old UI cannot
revoke a replacement review. Responses include authoritative current review state
and qualification, including explicit absence after revocation. Deleted facts can
still have their reviews revoked. Conflict errors use 409, unknown records/reviews
404, invalid payloads 400, wrong owner/target 403, and unavailable/disabled storage
503. Oversized previews use 413. Error responses contain no candidate contents.

## Runtime enrollment and existing-only recipient access

Owner review integration requires `TOPOS_PERMISSIONS_V2_ENABLED=true`, the paired
node configuration, `TOPOS_PERMISSIONS_V2_EVIDENCE_REVIEWS_ENABLED=true`, and an
explicit `evidence_review_store_path` in the private canonical database's
`permissions-v2` directory. The path cannot alias the node key, config, ledger or
lock. The default is disabled and unconfigured.

The first authorized owner preview/read enrolls the private store. A separate
private durable marker is written as pending before store creation, then activated
with the canonical/resource/store identity only after enrollment succeeds. Crash
interruption, missing marker, missing store, identity loss, file replacement or
protection-clock rollback requires explicit recovery and is never silently fixed.
The marker and store together do not provide protection against a privileged host
deleting or rolling back all trusted durable state at once.

Trusted server adapters use `Runtime.evidence_reviews(require_existing=True)`.
This never enrolls and exposes only an already pinned service; it does not grant
recipient access to owner preview/read methods. `EvidenceResolver.with_qualified`
calls a trusted in-process callback with current qualification and private rows
while holding the canonical read transaction, the node write gate and a private
review write transaction. The callback must finish its own final policy checks
and immediate transport delivery before returning, and must not mutate evidence
or reviews. The transaction guarantee assumes the configured single writer and
shared write gate; an external writer bypassing that gate in SQLite WAL mode is
outside this guarantee. No serialized qualification is accepted as authorization.

## Native authorship and source posture vetoes

A materialized canonical `actor_role` on any fact or terminal source row is a
restriction: only the exact value `authored` or SQL NULL/absent is eligible.
Addressed, participated, observed, ambient, inferred, unknown, empty, or malformed
roles cannot be overridden by review labels, a pack name, or native self flags.
SQL NULL is legacy absence, not authorship proof: all existing owner identity,
native sender, stated fact, recursive lineage and explicit review checks remain.

The resolver applies the native `record_role` ambient posture cap using only its
own canonical SQLite read snapshot and immutable bundled source defaults. It does
not call legacy posture helpers that can open the process default database or
suppress a failed settings read. Conversation overrides match the exact dataset
and source. Datasetless AI cannot borrow another dataset's permissive override;
any ambient override for its source vetoes, and permissive overrides cannot lift
an ambient source default without a certified dataset binding.

Absent legacy posture configuration inherits `mixed`, which merely leaves native
per-row attribution in place. Explicit malformed values, malformed schemas,
ambiguous active installs/overrides, and incompatible concrete runtime source
scope bindings withhold. A missing runtime posture inherits its bundled default;
a runtime's bare `mixed` default retains a bundled non-mixed declaration, matching
the native registry read semantics. An exact conversation owner override still
takes precedence. Unknown sources with no explicit posture remain legacy mixed
and require all native authorship and owner-review proof.

Every terminal source revision includes a reserved `_p2b_source_revision` hash of
its effective posture and configuration inputs, including the applicable owner
overrides, active runtime definition/revision and bundled default. A posture/input
change invalidates the old review, even if it does not alter message text. A
physical column colliding with this marker is rejected. The marker is omitted
from owner preview cells and does not change any public wire schema. Changing a
posture and restoring all of its original inputs before another read is not a
separate durable revocation event; owner Off-limits has its own monotonic clock.
