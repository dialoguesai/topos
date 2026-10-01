# Observed Off-limits boundary for fact-backed messages

Status: local implementation for review, not installed on the owner's node.
Scope: fix blanket withholding first; measure Clark's signed searches next;
expand evidence coverage only after that measurement.

## Contract and release unit

A separate protected entity no longer withholds every fact. A whole fact's
support closure is checked: its root, derived ancestors and terminal messages.
A protected contributor withholds that closure. Another fact with independent
support remains eligible. All existing source, category, sensitivity, event
window, authorship, lineage, copy and owner-deselection checks remain necessary.
This version adds no excerpts or substring redaction.

The implementation follows the contact-card/observed-association decision
recorded in the 26 September handoff. It does not implement the stronger semantic
coverage proposal in `ENTITY_COVERAGE_DESIGN.md`, and an empty NER/mention result
is never presented as a certificate of semantic absence.

## Restriction input

`EntityBoundary` derives vetoes in one canonical SQLite transaction from saved
protected names and aliases, current matching entities, merge history, observed
mentions, entity contact links, contact usernames and phone/email/service
identifiers. Preemptive names and reminted/merged ids retain protection. Ambiguous
associations enlarge the veto set. Re-flagging preserves saved aliases when the
live inventory shrinks.

Recorded mention spellings also close matching reminted identities and their
contact links. Identity associations are queued rather than repeatedly scanning
the whole universe; mention reads use bounded batches and an aggregate row cap.
Phone digits are normalized before adding the local-number variant, including
numbers stored with Arabic or Persian decimal digits.

Full stored string surfaces, recursively decoded object/array JSON, rendered
content and native identity fields are scanned. Normalization handles combining
and format characters, HTML escapes, separators and a pinned small confusable
map. Short initials and names (under four characters) use whole-token
matching to avoid `M.E.` matching `message`. Since boundary v3 a short
name's pet-name and inflected forms also match as whole tokens (`Abe` as
`Abey` or `Abie`, `Sam` as `Sammy` or `Sams`), never an ordinary word that
merely starts with it (`also`, `same`, `edit`, `join`): see
`entity_boundary.short_variants`.

Human conversation context includes the exact source/dataset parent, its
metadata, roster and distinct observed senders. A protected participant or
contact-linked sender withholds outgoing messages in that conversation too.
AI-chat parent titles/metadata and bounded, exact same-thread reply ancestors
are checked. Parent record protections and observed parent mentions veto.

Unrelated neighboring messages' **content is not a dependency**. A protected
mention elsewhere in an ordinary thread does not alone withhold an independently
supported message. Known membership, protected parents and declared reply
dependencies still veto it. Graph adjacency does not propagate protection into
every neighboring entity or fact.

Missing schemas, malformed JSON, duplicate parents, unsupported valued binary
surfaces and exceeded bounds withhold. Entity intelligence exclusions retain
their distinct global floor. Owner preview remains available through verified
owner authority; recipients receive no protected names or reason details.

## Current-state and ranking checks

A boundary is cached only for its SQLite read transaction. Index membership and
live release use the same `_qualified_bundle` veto. The private index basis binds
the observed entity/contact universe, and each sealed member binds the rows and
required context of its entire support closure, including other messages backing
the same fact. Changed aliases, mentions, contacts, participants, titles or reply
ancestry invalidate stale ranking material. Builds recheck before publishing and
retry a concurrent change. Search checks before ranking, within final canonical
requalification, and immediately before the transport sends a checkpointed
result. Old indexes without the new fields refuse. The pre-send check releases
the ledger before it can acquire a node gate; no gate spans the network write.

Stale indexes must be rebuilt before search resumes. This temporary refusal is
necessary to keep newly protected data out of corpus statistics; it is separate
from the permanent blanket withholding being removed.

Signed authority, recipient/app binding, replay protection, consent, budget and
protection clocks stay in force. This change does not remove Off-limits from
signed clock state. Existing flag lifecycle writes can therefore still require
grant synchronization and stale explicit classifications under existing clock
semantics. The transport's checkpoint/send ordering is unchanged; bytes already
dispatched cannot be recalled.

## Limits and rollout

Indirect references without a recorded identity, recognizable name, declared
reply or known contact association can evade this boundary. A nameless message
involving a number absent from a protected contact card is an example. Unknown
nicknames, misspellings and confusables outside the pinned map are not certified
absent. Do not claim this proves arbitrary prose contains no information about
a protected person.

The same observed identity/contact veto now covers legacy UMA message reads and
raw scoped canonical rows before redaction, including native graph/contact ids.
Message parents are recovered from canonical storage rather than trusting the
public row to include sender/context fields. A file-backed reader uses one fresh
read-only snapshot, and request-cached identity terms cannot authorize egress.
Unsupported/missing context withholds. Protected and nonexistent entity anchors
produce the same unresolved window response.

Legacy materialized summary/inference modes have incomplete input provenance.
While any entity/record protection is active, those modes remain withheld before
loading data, including attention, availability and complexity add-ons. This is
an unsupported-family floor; it does not withhold unrelated v2 messages or stop
Clark's model from synthesizing an answer from released messages. The owner keeps
access under verified owner authority. No new evidence family is introduced.

The broader owner-inspection/relay gaps reported in the handoff still require
their coordinated CP/engine review before claiming a universal non-owner
guarantee. Legacy reads do not acquire the signed v2 checkpoint/send guarantees.

Existing indexes can be rebuilt through the owner's 0600 Unix socket at
`POST /v1/permissions-beta/v2/message-search/rebuild`. No bearer or browser-token
extraction is needed. The response contains only grant/ready counts. TCP and
other principals refuse; a signed owner relay must still match the bound owner.
Rebuilding changes derived indexes, never grants or filters; qualification and
embedding run outside the node writer gate, with the index's own publication
checks retained.

Synthetic acceptance requires nonempty unrelated release, protected canaries
absent from outputs and ranking bags, normalized-name and context canaries,
missing-state refusals, alias/merge persistence, and concurrent-change refusals.
The signed-search fixture sets maximum k to ten and has thirteen permitted
members beside protected, forwarded, correspondent and owner-deselected controls.

Before installation: complete boundary/privacy regression tests and diff review.
After installation: rebuild the current grant index, query through Clark's
accepted grant, privately record released opaque ids/counts, check for leaks and
valid citations, then judge usefulness. Synthetic success establishes neither
live yield nor absence of live leaks. Later iterations should separately measure
owner filtering, missing provenance, unsupported families and retrieval recall.
