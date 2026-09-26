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

- Its existing payload disclosure is `scoped` or `owner_only`. Missing, unknown and
  any other disclosure are withheld. Until 26 September only `scoped` qualified,
  which withheld every fact a real node holds: `facts/llm_extract.py` writes
  `owner_only` for every owner-asserted fact and `scoped` for facts asserted by
  others, so the rule admitted exactly the facts the next bullet refuses and
  refused exactly the ones it admits. The disclosure a fact carries describes
  how far the node's own surfaces show it; it is not the owner's word on sharing
  their own statements under a policy the owner authored, and the owner's
  deselection (below) is. An owner review never changes disclosure.
- The fact is about the uniquely identified owner and asserted by the owner.
  Existing actor-role and altitude representations must agree with that claim.
  A payload saying `stated` cannot override a row saying `inferred`, or vice versa.
- Every recursively referenced fact meets the same conditions. The recursive
  graph resolves without cycles, missing/deleted rows, duplicate references,
  ambiguous identities or exceeded bounds (128 records, depth 16).
- Every terminal source is an owner-authored canonical message. Conversation
  messages require native `is_from_self=1` and matching `owner_user_id`. AI messages
  count only through the owner-attested ChatGPT lane: `sender_type` `human` or
  `user`, the lane's source id on the row and on its one conversation parent,
  that parent belonging to the owner, and a live provenance link whose content
  revision matches the row. No other AI-chat row is owner-authored, because
  `app_ingest` and other unguarded doors can write `human` rows under the
  owner's conversation. The parent row's revision is bound into the evidence revision.
- A review covers every fact and terminal source at its current complete row
  revision: the owner's explicit review when one exists, otherwise the implicit
  review described in the next section, unless the owner deselected the fact.
  Every classification states owner authorship, direct self-statement, only
  owner subjects, known sensitivity and nonempty domains. Mixed subjects,
  quotations, unknowns and known independent copies withhold the entire
  candidate. Which entities count as the owner is fixed by the signed
  capability, not by this resolver: see the subject contract below.
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

Every row revision hashes the row's reviewed surface: every valued SQLite
column except a closed per-table list of operational columns that routine
syncs, derived scrubs and fact refreshes rewrite without changing what the
owner reviewed (`REVIEW_SURFACE_EXCLUSIONS`: batch ids, ingest and row
timestamps, derived content hashes and scrub outputs, extractor confidence and
writer identity). A column missing from that list is consent-relevant as soon
as it holds a value; a NULL column is absent from the surface, so a migration
that adds a column stales nothing until a value appears, and clearing a
reviewed value is a change. Legacy JSON text is pinned exactly, except that a
fact payload is compared as sorted JSON without its confidence. Tagged finite
SQLite floats are encoded as exact hex. The snapshot additionally binds the
complete graph and the durable canonical identity (resource binding,
protection clock identity and exact database path). Any changed candidate,
source, parent or recursive edge invalidates the review.

The snapshot's `protection_revision` binds the protection history of its own
closure, not of the whole node (`closure_protection_revision`): the current
Off-limits and record-tombstone state of every closure record, every fact
tombstone key its facts could match, and the latest clock-v3 event that
touched any of them. Protecting, excluding or lifting anything else on the
node leaves every unaffected review current, while any event touching the
closure, including a protect-then-lift with no intervening read, changes it.
That memory lives in the clock v3 event log, which has no delete or update guard
yet, so deleting its rows can make such a review current again (open issue
F07). Entity Off-limits and entity exclusions remain node-wide inputs until
entity coverage exists. Signed authority still binds the node-wide
revision of the read that serves it, checked by the release adapters.

## Implicit review

The owner asked for every qualifying fact to be available without a review of
each one -- a node holds tens of thousands of signal objects and hundreds of
facts, and ingestion adds more every hour -- with the owner able to deselect any
fact, and the least confident facts surfaced first for a look. So a fact the
owner has neither reviewed nor deselected is *implicitly reviewed*:

- Its review is synthesised at qualification time from the snapshot just taken
  (`EvidenceResolver._implicit_review`), so it can never be stale, and it is
  never stored: `review_id` is `implicit:<fact_id>`, `reviewed_at` is 0.
- Its labels are the node's own, from `IMPLICIT_LABELS`, keyed by the fact's
  predicate and, failing that, its signal dimension, in the
  `owner-review-vocabulary/v1` a policy is written in. The table errs towards
  the more protective sensitivity (work predicates are `work`/`none`;
  `prefers` is `hobbies`/`personal`; `lives_in` is `home`/`personal`;
  `practices` and `training_for` are `health`/`special`; anything unkeyed is
  `relationships`/`special`). A policy releases a fact only when its domains
  intersect the fact's and its sensitivities include the fact's, so a wrong
  label can hide a fact from a policy but cannot hand a health or home claim to
  a work-only one.
- Its authorship, speech and copy claims are exactly what `_eligible` then
  verifies against the rows -- owner-authored terminal sources, no quote
  metadata, no independent copies -- so the synthesised review asserts nothing
  the integrity checks do not prove.
- An explicit owner review, where one is current, takes precedence over the
  implicit labels. The owner's deselection -- an opt-out row in the private
  review store -- takes precedence over both: an opted-out fact is withheld with
  `owner_opted_out`, and a message that also backs an opted-out fact is withheld
  by the sibling floor for raw release. Absence of any row is availability, so a
  fact ingested a moment ago is available at once, and the store never needs a
  row per fact.
- The owner's review queue (`EvidenceReviewService.queue`, owner-only) lists
  every current fact least confident first -- `signal_objects.confidence` is
  the extractor's own doubt -- with the labels it carries, its standing
  (`explicit`, `implicit`, `opted_out`), its qualification and its terminal
  source count; `totals` gives "N of M facts shareable" per source; `opt_out`
  and `opt_in` are the deselection and its undo. The queue shows a fact's own
  predicate and object, never a terminal message's text.
- The p2c search index (`search_index.py`) is built over every current fact
  that is not deselected, qualified exactly as above; it holds the node write
  gate only to freeze the owner's decisions and to publish, and builds on an
  ungated read snapshot in between.

Evidence reviews are enabled unless `TOPOS_PERMISSIONS_V2_EVIDENCE_REVIEWS_ENABLED`
is set to something other than `true`, and the store defaults to
`evidence-reviews.db` in the durable `permissions-v2` directory beside the
canonical database when the node config names no path; the node's own process
enrols it at startup. Nothing here changes the canonical database's schema.

## Owner review storage and trust

Open issues F01, F02 and F07 cited in this document come from the 15 September
audit. They are reproduced on synthetic databases and listed, with their
mitigations, under "Known open issues" in the control plane's
`docs/testing/PERMISSIONS_BETA_RELEASE_CHECKPOINT_2026-09-14.md`.

`EvidenceReviewStore` is a separately configured private SQLite service dependency;
`qualify` never accepts a request-supplied review or a `reviewed=true` flag. Creating
or revoking a review requires a verified `OWNER_APP` principal over `uds` or signed
`cp_relay` with `acting_user` equal to the pinned node owner. Owner class alone,
the same actor under a third-party class, and TCP headers are insufficient.

The store requires an absolute path, a private file owned by the process user,
and no symlink anywhere in its parent path. It persists a random store identity,
the durable canonical identity and the resource identity inside the file and
checks them on every open; the enrolled runtime additionally pins that store
identity and an authority digest of every review row in its external marker. That
digest is `digest` of every row -- `review_id`, `fact_id`, the stored body and the
active flag -- ordered by `review_id`, retired rows included, and it is computed by
streaming those exact bytes into SHA-256 rather than building them as one canonical
value. The owner's deselections (`fact_opt_outs`) are authority too: a store rolled
back to before an opt-out would widen release, so once any exist the digest is
taken over `{"opt_outs": [...], "reviews": [...]}`; with none it is exactly the
value every earlier engine computed, so a marker such an engine wrote still opens,
the table is created on reopen before the schema pin, and no migration exists to
run. What also changed earlier is that `canonical_bytes`' 1 MiB
whole-value refusal no longer applies to it, which had stopped one store at 309
owner reviews of the beta campaign's shape and failed every read, revoke and
release on it with `json_size`. Cost is now bounded by reading the rows, not by
the size of one Python object, but it is still linear. Measured on this machine,
one digest takes about 3 ms at 386 rows, 7 ms at 772, 19 ms at 2,000 and 94 ms at
10,000, and a store write pays two of them plus two fsynced marker publications. Of
each of those, the table-count cross-check described below is about 0.2 ms, 0.4 ms,
1.1 ms and 5.9 ms -- roughly 7% from 2,000 rows up, and it decodes no row. At 386 rows
it is 0.02 to 0.2 ms depending on the run, which is inside the noise at that size: the
figure that resolves, and so the one to hold the design to, is the 7%.
That work is not merely *under* the node write gate: it is done while HOLDING the
process-wide write lock (`topos/storage/db/write_gate.py`, one `_WRITE_LOCK` for
every writer in the process), so at 10^4 rows it is node-wide write latency rather
than review latency -- every other writer waits for it. The stated goal of a small
per-write cost at 10^4 rows is therefore not met -- this change covers a few
thousand reviews per store, not ten thousand. The campaign this was built for fits with
room to spare: 386 reviews of its shape in one store are about 1.07x the old cap -- so the
old digest could not have held them at all -- and one further owner write there takes
about 8 ms end to end, one owner read about 11 ms. Those two end-to-end figures were
measured before the cross-check; each digest they contain now costs about 0.2 ms more
at that size, and the digest counts per call are pinned by test (two per owner read,
five per owner record).
Nothing bounds lifetime growth, so a single digest over 100 ms is logged as a
warning -- a duration and a row count, never a review, and pinned by test -- as the
signal that a bounded or incremental scheme is due. The warning is measured around
the cross-check as well as the encoding, so it stays a statement about what one
digest costs rather than about one part of it; 100 ms is about 10,600 rows on
the machine measured above. The write gate's own slow-section warning
(`_SLOW_HOLD_WARN_S`, 5 s) is the second tripwire, and it is the one that reports
what the rest of the node paid.
A different store at the enrolled path is refused, and so is an older store file
restored alone whose review rows differ from the enrolled digest (for example a
copy from before a revocation). An older file with the same review rows is not
refused: the marker does not carry the store's protection-generation floor, so
restoring the canonical database and the store together from before a
restriction, with the marker left in place, is not detected (open issue F02).
The marker lives beside the store, so restoring both together (for example the
whole `permissions-v2` directory from an earlier backup) is not detected either.
One case changed with the size cap, and it is the owner's call rather than an
implementation detail: a store larger than 1 MiB of digest input, paired with a
marker written outside the engine to match its rows, now opens, where before every
path on it failed `json_size`. The engine cannot produce such a pair -- a write
that crossed the cap rolled back before its commit, and the lab's recovery only
activates a digest the engine computed -- so it is the same trusted-file boundary
as a marker forged to match tampered rows, which is already accepted, and which was
already accepted at any size below 1 MiB. What is lost is that the >1 MiB case used
to fail closed by accident. Store-only tampering is refused at both sizes, which is
what the detection cases in `tests/permissions_v2/test_review_store_floor.py` run
twice. The alternative, if this is not acceptable, is to keep a cap on the digest
INPUT size and refuse above it, at the cost of the capacity this change exists to
buy.
The store file must hold exactly the objects the store creates and nothing else:
`review_identity`, `fact_reviews`, that table's primary-key index, the
`(fact_id, active)` index below, and `projection_contract` in the output store.
Anything else is refused as `review_database_binding` at every open, including the
reopen that writes the observed clock high-water. The digest covers rows rather
than schema, so a planted trigger firing inside a legitimate owner write would
otherwise be published into the marker as the owner's own work. The check compares
the kind lower-cased and never reads the spelling as authority: SQLite decides an
object's kind from its `sql` text and accepts any case variant in `type`, so a row
written with `type='TRIGGER'` installs a trigger that fires while a
`type IN ('trigger','view')` test sees nothing (SQLite 3.47.1). It denies by kind
rather than listing the two kinds that execute, so a kind this engine does not know
about fails closed. The `sql` text is deliberately not compared: rewriting it cannot
smuggle in an executing object, and any reinterpretation of the stored cells moves
the row digest. An operator who has added anything to the file -- an extra index, or
an `ANALYZE`'s `sqlite_stat1` -- must drop it; the refusal is closed, not lossy.
The current-review lookup uses an index on `(fact_id, active)`, which the store
creates and the pin therefore requires. The row it points at is never trusted: the
predicate is answered from the index's keys alone, so `_current_row` re-reads the
row by rowid, out of the table b-tree, and re-asserts `fact_id` and `active` from
what it finds. A stale or planted b-tree under that name would otherwise decide
which review is current while the row digest still matched the marker byte for
byte -- neither the index nor `sqlite_master` is digested. With the re-read, serving
a revoked review needs a real row in the table b-tree.
The digest is what refuses such a row, and it does not take its own reach on trust
either. What it enumerates is not the table: `ORDER BY review_id` is planned as a walk
of `sqlite_autoindex_fact_reviews_1`, the primary key's own index, so moving the serving
lookup onto the table left the digest resting on a different index -- and the two
could disagree in an attacker's favour. A row written into the table b-tree and not
into that autoindex is invisible to the digest, so the marker still matches byte for
byte, the object set is still exactly the pinned one, and the table's `sql` is still
byte-identical, while `_current_row`'s rowid re-read finds that row and serves it as
the owner's current review. Every digest -- entry and exit, evidence store and output
store, since all four are one function -- therefore compares the number of rows it
streamed with `SELECT count(*) FROM fact_reviews NOT INDEXED`, which is the table
b-tree's own answer with every index forbidden to the planner, and refuses a
disagreement as `review_database_binding`, in either direction: a stream shorter than
the table is the hidden row, and a longer one is an index entry the table cannot answer
for, which the exit digest -- where a changed digest is published rather than refused --
would otherwise write into the marker as the owner's own. Both reads run in one
transaction under `BEGIN IMMEDIATE`, so a disagreement is never a race: it is a store to
quarantine, not one to retry, which is why it is not the transient
`review_storage_unavailable`.
At the entry digest the count and the value are together exhaustive about the reach. A
stream that misses a table row and still counts right has to have streamed something in
its place -- some other row a second time, or an entry with no row behind it, which comes
through as a row of NULLs -- and both of those are in the value, which the marker pins.
At the exit digest only the count refuses, because a changed value there is the owner's
own write being published; that is the same boundary the rest of the exit digest has,
where a file rewritten underneath an open transaction is published rather than refused,
and the enrolled marker is what the next open judges the file against.
The cells it hashes are read out of the table too, which took a second correction.
Written as the plain ordered walk -- `SELECT review_id,fact_id,review_json,active FROM
fact_reviews ORDER BY review_id` -- the plan took `review_id` from the index KEY and only
the other three columns from the row, so one cell of every row was pinned to the index
rather than to the table: a table cell edited away from its key digested as the key, and
the marker still matched byte for byte while `PRAGMA integrity_check` reported the row
missing from the index. Nothing reads that cell back today -- every `review_id` a caller
sees comes from the parsed body, and `record_review` and `revoke_review` match on the
key -- so it disclosed nothing, but that was a property of the current call sites rather
than a checked one, which is the accident this cross-check exists to remove. The digest
now walks the index and joins each entry to the row it points at (`LEFT JOIN fact_reviews
AS t NOT INDEXED ON t.rowid=i.rowid`), so all four cells come from the table b-tree and
the autoindex decides only the order of the stream and which rowids it reaches -- the
first pinned by the digest value itself, since the same rows in another order are another
value, the second by the count. It costs nothing measurable -- 3.1 vs 3.2 ms, 17.7 vs 17.4
and 87.8 vs 87.3 at 386, 2,000 and 10,000 M1-shaped rows, walk against join, medians of 21
interleaved repetitions, a difference that changes sign between sizes -- because it is the
same b-tree seek the walk already deferred, with one more cell read from the page it lands
on. Enumerating the table instead (`... FROM fact_reviews NOT INDEXED ORDER BY review_id`)
reads all four cells from the table too and needs no count, but sorts: 114 ms at 10,000
rows against 81 ms, with every owner review body through a temp b-tree. `LEFT JOIN`
rather than an inner one, so that the stream stays one row per index entry: an entry
pointing at a rowid the table does not hold now streams NULLs and is caught by the count,
where the plain walk ended the read with SQLite's "database disk image is malformed" and
an inner join would have dropped it silently.
`fact_reviews_current` is deliberately not counted the same way. A row hidden from it
is still in the autoindex, so it is still digested and the marker still has to match
it; and what that index decides -- which row answers `fact_id=? AND active=1` -- is
exactly what `_current_row` refuses to trust. `fact_reviews` is also the only table in
either store with an autoindex to hide a row from: `review_identity` and
`projection_contract` key on `INTEGER PRIMARY KEY`, which is the rowid itself, so
their reads are table reads already, and the pinned object set names no autoindex for
them. A future table with a non-rowid primary key needs the same cross-check, and a
test fails if one appears without it.
`PRAGMA integrity_check` is the operator-side check on the indexes themselves; no
request path runs it. Not because it is slow: measured on this store's shape it is
about a quarter of one digest (0.6 ms, 4.3 ms and 25 ms at 386, 2,000 and 10,000
rows), because it walks pages in C while the digest encodes rows in Python. It is off
the request path because its cost is bounded by the whole file rather than by this one
table, so it grows with anything the store ever holds; because its verdict is a list
of English sentences rather than a value, so reading "anything but ok" as a refusal
makes a SQLite message-text change either an outage or a silent pass; and because it
is still 3-4x the cross-check's own cost inside a section that holds the node-wide
write gate. It is what names the fault once an operator is looking: the hidden row
reports as `row N missing from index sqlite_autoindex_fact_reviews_1`.
Opening a store still writes to it before the floor has judged it: the reopen
creates the missing index and the clock high-water, in a transaction that predates
the floor (the high-water always did). A refusal rolls that transaction back, but an
operator who wants a suspect store's original bytes must copy the file before
starting the engine.
The exit digest is recomputed, and the marker republished, only when the
transaction compiled a statement that could change a review row. A SQLite
authorizer on the connection decides that, so trigger bodies and statements
cached before it are included, and an action code it does not recognize counts as
a change. Under `BEGIN IMMEDIATE` no other SQLite connection can commit a row
change, so such a transaction wrote no review row through SQLite. Skipping the exit
digest is then a deliberate refusal to look, not an equality: a release callback,
which holds this transaction open across its own work, can rewrite the store file
in place, and re-reading the rows at the end is exactly the step that would publish
the rewritten state into the marker as the owner's own. The marker cannot move, and
the next verifying open refuses the restored file as rollback
(`test_an_in_place_restore_during_a_release_callback_is_never_absorbed`).
As a second check, a transaction that compiled no row write at all and still sees
`total_changes` move is refused as `review_database_binding` -- tamper, not the
transient `review_storage_unavailable` this file uses for a storage fault. That is
the whole of the claim: `total_changes` is connection-wide, so a transaction that
legitimately writes any row, on any table, switches the check off. The one path
with nothing else to write is an owner read, and it now writes the clock high-water
only when the high-water actually moves, so the check is live there.
Device and inode numbers are compared only within one process: a bind mount
renumbers them across a container VM restart (observed 2026-09-15), which is not
a change of database. Replacing either database with a different one or
tampering with the stored identity withholds. The canonical identity does not
separate byte copies at the same path: where the canonical path is fixed, as in
containers (`/root/.topos/database.db`), a copy of the node state run under the
same resource binding is the same database to these checks. Distinct nodes are
distinguished by their binding, not by path. The store persists its observed
protection-clock ID and highest generation; a seen rollback is rejected during
use and across a normal service restart. The generation advances only when a
review is recorded, an owner read or preview or recipient qualification runs, or
a projection transaction observes the clock, so a canonical restore to before an
unobserved protect is not seen (open issue F01). A new clock is not silently
accepted. Review IDs are immutable and cannot be replayed after revocation; one
current review per fact is loaded authoritatively. First enrollment requires an
exclusively created new file. Missing schemas or identity/clock metadata in an
existing file are never silently recreated.

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
condition; this family is not a general declassification mechanism. The count is
keyed by two built-in expressions of the text, its character length and its first
64 characters, which migration 76 indexes on both message tables
(`permissions_read_path_indexes_v1`); the full-text equality behind them still
decides, and on a database without the index the same statement scans, as before,
and answers the same.

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
observed protection-clock rollback requires explicit recovery and is never
silently fixed; rollbacks the store never observed, and event-log deletion, are
open issues (F01, F02, F07). A marker left pending by a crash after a review
mutation is recovered only with the engine stopped, through the lab's
`recover_durable_identity.py --phase activate-pending`: it activates the marker
when the store's authority digest equals the pending digest and otherwise
archives both, so the next owner preview enrolls a fresh store. That tool
recomputes the same whole-history digest without a size cap, so it keeps working
unchanged on a store grown past 1 MiB; the marker version literals are unchanged
for the same reason, and nothing in the marker records that a store has outgrown an
older engine -- the marker model forbids unknown fields, so adding one would make
the older engine refuse the marker outright, which is worse than what it does now.
An engine older than this change, including a shadow host left on an earlier commit,
refuses a grown store with `json_size`, which the handler maps to HTTP 400 and the
control plane reports as a bad request rather than as a host that is too old. It
fails closed and keeps the marker, but the diagnosis is not in the error: deploy
the node and any shadow host from the same commit, and read a sudden `json_size` on
a store that used to work as a downgraded host. The marker and store together do not provide
protection against a privileged host deleting or rolling back all trusted
durable state at once.

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

## Intelligence-exclusion boundary

Record and semantic fact tombstones veto every recursive contribution before
release, including the interval before lifecycle purge finishes. Any entity
exclusion withholds this family until complete entity coverage is certified;
missing observations after purge are not evidence of absence. Unknown exclusion
state fails closed. The versioned protection clock now tracks exclusion changes,
including add/remove cycles. Serving requires clock contract v4. Existing beta
nodes run the explicit, monotonic [clock v2 upgrade](EXCLUSION_CLOCK_UPGRADE.md)
from a v1 clock and then the
[clock v3 upgrade](EXCLUSION_CLOCK_UPGRADE.md#clock-v3-closure-scoped-review-binding);
a node that stops at v2 or v3 withholds with `protection_clock_unavailable`, as
does one whose engine identity tables appeared after the clock was installed,
until [the coverage resync](EXCLUSION_CLOCK_UPGRADE.md#coverage-is-recorded-not-assumed)
runs.
Neither clock upgrade changes a signed schema. The separate `permissions-beta/p2b-v2`
capability did: engine `122028c` regenerated eight signed protocol and fact
schema exports that embed the fact authority union, and added two schema exports
(`StatedDayFactPolicy`, `StatedDayFactDecision`) and the signed golden vector
`signed-golden-v2.json`.


## Whose facts these are

Producers write the owner's entity id as a fact subject, and a node legitimately
holds several `is_self` rows, so the first fact family could only release the
literal `"self"` subject that no production producer writes. Identity is now
attested rather than inferred, and which rule applies is fixed by the signed
capability. `QualifiedEvidence.subject_contract` records it, and the policy
preparation refuses a policy whose capability maps to a different one.

| capability | subject contract | permits |
|---|---|---|
| `permissions-beta/p2a-v1` | `legacy_single_self_v1` | the literal subject and the sole `is_self` row, or a refusal |
| `permissions-beta/p2a-v2` | `owner_attested_v1` | the literal subject unless shadowed, plus each active attestation |
| `permissions-beta/p2b-v1` | `legacy_single_self_v1` | the same |
| `permissions-beta/p2b-v2` | `legacy_single_self_v1` | the same |
| `permissions-beta/p2b-v3` | `owner_attested_v1` | the literal subject unless shadowed, plus each active attestation |
| `permissions-beta/p2b-v4` | `owner_attested_v1` | the same |

Two sets are always in play. The permit set above decides what may be released.
The restriction set decides what a tombstone, exclusion or copy check matches,
and holds every spelling of the owner this node has ever seen: current `is_self`
rows, ids ever attested or revoked, registry ids, and the merge-tombstone
fixpoint over those. The permit set is always a subset of the restriction set,
so widening whom the owner may release about can never narrow what an owner
restriction covers. The former `{"self"}` fallback in the tombstone prefix
builder is gone: it silently dropped every entity-keyed owner tombstone on
exactly the multi-self nodes that needed it most.

Under the attested contract a fact whose subject was rewritten in place is never
eligible. That is the merge overlay signature: another person's fact re-keyed
onto the owner's entity, carrying the owner's own messages as its evidence. The
owner can state the claim again, which writes a new fact with its own lineage.

Entity ids stay inside the node. Reviews still carry `["self"]`, the output
still says `subject: "self"`, and the permit set is derived beside the evidence
and passed by value rather than stored in a candidate, a review, a receipt or a
recipient error. The full design is in
[OWNER_IDENTITY_BINDING.md](OWNER_IDENTITY_BINDING.md).
