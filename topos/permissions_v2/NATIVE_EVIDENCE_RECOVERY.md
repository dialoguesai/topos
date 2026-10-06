# Existing native iMessage evidence recovery (beta candidate)

This owner maintenance operation recovers evidence for **existing canonical rows**.
It does not sync history, edit a grant, rewrite message ownership, or expose an
unfiltered query API. Recipient search remains the signed p2c path.

## Authority and bounds

`POST /v1/sharing/imessage/recover` is available only on the verified
owner Unix socket. The paired runtime supplies owner/node/resource identity;
request fields cannot select native paths, canonical files, a model host, labels,
or prepared facts. The request supplies a dataset, UTC interval of at most 31
days, and the existing iMessage account ownership attestation. Both permissions
and snapshot ingestion feature switches must be enabled; the snapshot root must
be the paired node's private configured directory.

The native read takes a read-only SQLite snapshot including WAL, selects at most
1,000 sent-by-me rows in the interval, and retains only exact canonical matches.
It does not copy chat.db or move the sync cursor. Native message form, GUID,
conversation, source/dataset, content, and timestamp representation are checked.
The separately versioned reader accepts bounded Foundation attributed strings
without Objective-C deserialization. Native nanoseconds are retained; every
release must pass both the signed canonical-time window and native-time ceiling.
Unsupported/ambiguous forms, quotations and forwarding metadata are withheld.

## Preparation and publication

The immutable private capture is independently parsed again. Off-limits checks
run before local preparation and again in normal qualification/release. Local
preparation uses the installed pinned Qwen model and reviewed classification
rubric over the whole message. It never truncates a message for classification,
pulls a model, uses environment HTTP proxies, or falls back to a hosted model.
At most 96 first-person candidates are attempted in a 600-second scheduling
window; each inference call has a 25-second timeout. Model output contains only
closed predicates and literal atomic object spans; subject/authorship come from
native proof and the contract's reserved `self` identity bound to the paired
owner. Recovery does not choose or attest legacy graph aliases; a graph entity
shadowing that reserved literal still blocks it.

Publication rechecks current canonical rows and snapshot bytes, atomically writes
revocable provenance links plus complete fact references, then marks the job
complete. Existing FactStore exclusion and revision rules apply. It does not
upgrade a legacy sender flag into native proof or weaken ordinary snapshot
import collision handling. A later canonical mutation, lost snapshot, source
change, incomplete job or revoked enrollment makes the proof unusable.

### Relation grounding correction

Literal containment of a proposed object is insufficient: a plan to visit a
place does not establish residence there. Machine recovery now accepts only the
complete, explicit first-person statement forms in `native_claim_grounding.py`.
The same floor runs in qualification for already-published native-backed facts,
including the final source release recheck. Unsupported relations withhold as
`native_fact_relation_unproven`; owner review cannot bypass that floor. Original
messages and stored claims are not rewritten by this read-time correction.

This grammar intentionally has limited coverage. It must not be represented as
a general semantic verifier or expanded by treating an arbitrary object mention
as evidence for a relation. Additional message eligibility requires its own
contract and qualification path.

The whole-message privacy labels are a **machine classification ceiling**, not a
human review. They can add categories and raise sensitivity relative to a fact's
review. Missing/invalid classification withholds a recovered source even if a
later producer adds a fact or an owner reviews the fact. Every contributing row
still passes the current grant's predicates and the existing protection checks.
Semantic classification can be wrong; positive and adversarial recipient tests
and owner-side review of actual releases remain necessary.

## Current limits

- One immutable enrollment per dataset; this recovery does not implement ongoing
  enrollment refresh. A repeated recovery refuses an already enrolled dataset. The
  owner-run refresh ([NATIVE_EVIDENCE_REFRESH.md](NATIVE_EVIDENCE_REFRESH.md))
  re-proves that one enrollment against a fresh capture.
- A crash after enrollment but before publication remains closed and requires
  explicit recovery work; it cannot silently enroll a different snapshot.
- Unprocessed messages have no source classification and cannot release.
- The API returns counts/codes, never message bodies, fact values or entity IDs.
- Neither matching counts nor unit tests prove useful recipient results. The
  acceptance gate is multiple real queries in the recipient UI, with citations
  and owner-side checks of the exact released records.
