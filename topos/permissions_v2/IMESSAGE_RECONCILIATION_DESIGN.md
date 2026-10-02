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

## Reader v3: the owner's own replies (`imessage-existing-comparison/v3`)

Status, 2026-10-01: built on `codex/imessage-provenance-forms`, not released.
v2 (`imessage-existing-comparison/v2`) added Foundation attributed bodies. v3 reads
what v2 reads, and two more native forms of a sent-by-me message. Neither carries a
word of anyone else's.

| Native form | v2 | v3 | Why |
|---|---|---|---|
| `reply_to_guid` set, no thread | refused | read; the pointer is neither captured nor compared | Messages sets this pointer to an earlier message of the chat on ordinary messages too; an inline reply is marked by `thread_originator_guid` instead. The pointer carries no text, and the canonical row does not store it. On one owner's node, a 31-day dry run refused 280 sent rows as thread forms, while the canonical store held 7 inline replies over a comparable 30 days. |
| Inline reply: `thread_originator_guid`, optionally `thread_originator_part` | refused | read, and the stored row must name the same originator and part | The reply's text is only what the owner typed. The sync stores the originator in `reply_to_message_id` and both fields in the metadata. The comparison requires all three to agree exactly, and an unset native thread requires them unset. A part without an originator, or a field that is not a short printable identifier, refuses the whole snapshot. |
| Reaction (tapback) | refused | refused | Its stored body is a synthesized `[reaction:N]` marker that points at someone else's message. There are no owner words. |
| Attachment only | refused | refused | Its stored body is the `[attachment]` marker. There are no owner words. |
| Attachment with a caption | refused | refused | The caption is the owner's, but releasing it without the attachment it describes needs its own decision. The census's `native_form_attachment_with_text` sizes it. |
| Forward, quote, subject, system, deleted, spam | refused | refused | Unchanged. |

The exact body, native identity, sender, dataset and native nanoseconds are still
required, as under v2. The native probe's decisions are otherwise unchanged. Replaying
`p2c_probe_equivalence.py`'s 48 synthetic cases against the probe at `a15b7b50`, comparing
refusals, captured rows (without the `reply_to_guid` column) and counts (without
`native_observed_*`), finds 45 identical. The three that differ are a chained row (now
captured), an inline reply whose stored row does not name its thread (now
`reconciliation_message_form` instead of `native_form_thread_reply`), and a row carrying
both a thread and an attachment (now counted under the attachment bucket, still refused).

A capture names the reader that made it. Every capture made by this build is v3, including
a refresh's. The first refresh of a v2 enrollment therefore moves it to v3, in the refresh's
one transaction. A v2 enrollment that is never refreshed keeps validating as v2. A wheel
without v3 reads a v3 enrollment as unknown (`ingest_enrollment_unknown`). That withholds
every iMessage proof of that enrollment and nothing else: other lanes and the ledger's
digest are unaffected. Reinstalling a wheel with v3 restores them. The v2 parser also
refuses a v3 capture that holds a reply (`snapshot_message_form_unsupported`), so an older
reader can only withhold, never widen.

The sync's enrolled-dataset guard (`local_sync.IMESSAGE_ENROLLMENT_CONTRACTS`) now counts an
active v2 or v3 recovery enrollment, not only the snapshot lane's. Before, the guard named
only `imessage-owner-snapshot/v1`. A node whose one enrollment came from the recovery
therefore accepted syncs into a second iMessage dataset, whose rows no enrollment can prove.

Count-only observations, which decide nothing:
- `native_observed_reply_pointer` and `native_observed_thread_reply`, with their
  `_exact_match` splits;
- `native_observed_content_mismatch_whitespace`. It counts a stored body that equals the
  native body without its surrounding whitespace. The sync's reader strips that
  whitespace, so such a row can never match exactly. This sizes the loss; it does not
  change the comparison.
