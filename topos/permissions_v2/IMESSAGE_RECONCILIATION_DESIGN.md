# Existing iMessage evidence: bounded native comparison v1

Status: comparison and preflight only. No enrollment, canonical mutation,
permission widening or release is authorized by this component's output.

The existing snapshot ingest contract intentionally skips historical collisions.
Keep that rule. A separate reconciliation operation must establish correspondence
between an existing canonical row and the owner's actual native message before
a durable provenance service can link it. A source declaration or sent-by-me flag
alone is insufficient.

## First build boundary

`imessage-existing-comparison/v1` consumes an immutable bounded SQLite snapshot
through the existing strict plain-text native reader. It additionally requires
native message GUID, chat GUID and chat identifier. Native GUIDs are unique;
joins are unambiguous. The staging result contains the snapshot SHA and exact
native identities, body and UTC time. It is data, never an owner capability.

A canonical match requires the fixed iMessage source, the explicitly selected
dataset, native message/source-record ID, conversation ID, both GUIDs, chat
identifier, exact body and exact UTC event time. Only native owner-sent human
messages qualify for comparison. A contradictory canonical owner, sender, role,
message type, reply/forward/reaction marker or metadata field refuses. Unknown
valued metadata refuses. A missing native identity, guessed source, normalized
body, timestamp tolerance or ambiguous duplicate cannot establish a match.

The comparator fingerprints the complete canonical consent surface with the
existing row revision function. It does not stamp `owner_user_id`, rewrite
references, create facts, call models, enroll sources or mutate the grant.
Comparison outputs cannot be passed to the existing release path as authority.

## Subsequent publication boundary

Before any real row gains authority, a distinct owner-authenticated reconciliation
operation must pin the snapshot bytes, owner/node/resource, account attestation,
source/dataset, source generation and comparison contract. It must atomically
publish revocable record links and complete derived-fact references under a
private rollback-resistant marker. New legacy writes gain no authority. Recheck
native bytes and canonical revisions at publication and record revisions, source
generation, enrollment and revocation on every later evidence read.

Canonical rows may keep their historical NULL owner only when that new service
proves their exact private link. The current `_load` owner check stays unchanged
until that service and its adversarial controls exist. A datasetless reference
can be completed only from an exact unique active link; a missing source remains
withheld. New facts should reference only the particular rows proven by the
operation, with complete source and dataset identities, in the same transaction.

## Gates

- Exact synthetic native/canonical positive with no canonical changes.
- Reject received, forwarded, quoted, reaction, reply, system, deleted, attributed
  or attachment forms under this deliberately narrow reader.
- Reject wrong owner/source/dataset/GUID/conversation/time/body and duplicates.
- Fingerprint changes for consent-relevant metadata/content; operational receipts
  alone must not create or remove proof.
- Bound snapshot bytes, row counts, text sizes and SQLite work; no path supplied
  by a recipient; no raw content in diagnostic output.
- Real preflight reports only counts and refusal classes. A match is not evidence
  qualification. Live release additionally needs the future publication boundary,
  fact derivation, all grant filters and fresh authority.

This contract recognizes native structural quote/forward markers. It does not
prove that arbitrary plain prose contains no quotation or indirect reference.
The existing Off-limits and fact qualification checks remain necessary.
