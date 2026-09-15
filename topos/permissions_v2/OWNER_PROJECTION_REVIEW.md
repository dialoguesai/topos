# Authenticated owner preference output review

The owner service can now inspect, record, read and revoke a review of the exact
`owner_stated_fact.scalar.v1` value. It is separate from evidence classification,
grant activation and recipient release. The feature is disabled by default.

The node derives the candidate itself from the current canonical resolver and
its enrolled evidence-review store. Requests never supply trusted qualification,
source rows, a database path or an execution decision. An owner submits the exact
candidate and hash they inspected, an expected current output-review revision,
and an explicit output classification. The server checks all three under the
canonical and private review write gates before recording its own timestamp.

The output must remain the exact existing owner-stated `prefers` scalar, with
the registered lexical grammar. No paraphrasing, model generation or automatic
redaction occurs. Output sensitivity cannot be lower than the maximum current
evidence sensitivity. Evidence/source ownership, source posture, owner-only,
entity and record protection, unknowns and lineage checks remain prerequisites.
Approving output never overrides any of them.

The private `projection_review_store_path` must be an absolute, distinct file
inside the configured node's private `permissions-v2` directory. Enrollment uses
its own versioned pending/active marker with exact resource, durable canonical
identity, store identity and review authority-digest binding. The store also has
a distinct contract singleton. A missing store, lost marker, swapped evidence-store
file, pending enrollment, replaced store, older store restored in place, damaged
identity or protection-clock rollback cannot silently reset it. A remount that
renumbers device/inode values does not close it.
Existing evidence enrollment is required even for owner output preview; an
output request never enrolls evidence implicitly. Recipients cannot enroll either
store or call the owner preview/read/mutation operations.

Node operations are `permissions_v2_projection_preview` and
`permissions_v2_projection_review_read|record|revoke`, over a verified owner
channel with exact configured owner/resource binding. The independent node flag
is `TOPOS_PERMISSIONS_V2_PROJECTION_REVIEWS_ENABLED=true`, in addition to the
existing policy and evidence-review flags. The CP has a separate paired owner
proxy and flag. All responses advertise `authorization_status: not_evaluated`
and `execution_enabled: false`; a current review is not recipient authorization.

Review IDs are immutable and retries of the same current submission are
idempotent. Replacing a review requires the exact current revision. Revocation
requires the stored ID/revision and cannot accidentally revoke a replacement.
An owner can revoke a stale review even after its canonical fact disappears.
Changing or revoking evidence invalidates output without removing the stored
review history. Revoking output does not remove its independent evidence review.

The trusted `with_reviewed` adapter obtains qualification and the current stored
output review afresh, then holds the canonical, evidence and output review gates
through its callback. Only a trusted release adapter may supply that in-process
callback; it must still verify signed policy, current authority, and actual
transport completion. The callback is never a serialized request parameter.

Tests cover owner authentication, exact dispatch binding, separate enrollment,
restart and revocation persistence, candidate and CAS changes, immutable retry
IDs, damaged/swapped stores, sensitivity downgrade rejection, and both SQLite
review locks remaining held during the final callback. Synthetic live/browser
results are recorded separately from these scratch-store tests.
