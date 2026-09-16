# Separate temporal fields (step 5)

Status: built on `beta/permissions-v2`, 2026-09-16, after a design review whose
confirmed corrections are folded in below. Prerequisite for most new fact
families, per `audits/2026-09-14-permissions/BETA_NEXT_STEPS.md` step 5.

## The problem, measured

Three different times are written into one column, and which one a row holds
depends on which producer wrote it:

| meaning | what it should say | where it ends up today |
|---|---|---|
| **assertion revision** | when this node recorded or changed a belief | `signal_objects.valid_from` from FactStore (extraction wall clock), verdict edits, truth seeds, the LLM pass, most rule facts |
| **real-world applicability** | when the stated thing is true in the world | `valid_from` from the profile extractor as `YYYY-01-01T00:00:00+00:00` (an invented midnight UTC that exact-instant contracts accept as exact); `period_start` as a bare year, unvalidated model text, or a truncated occurrence date |
| **contributor event time** | when the source record happened | `valid_from` from the one dated rule pattern (raw `event_at`) and from DerivationWriter (`[:10]` of the source date, else `now`); `conversation_messages.event_at`, which legacy writers fill with ingestion time when the native time is missing |

Consequences reproduced on synthetic databases:

- A year is an exact instant to every reader that parses `valid_from`.
- Re-extracting an old message makes the old statement current again, and can
  close the newer belief with a `valid_to` earlier than its own `valid_from`.
- A missing native event time is indistinguishable from a real one.
- `valid_to` is written in three formats and compared as a string.

Readers disagree about what `valid_from` means: FactStore's renderer refuses to
show it as "since", `facts_direct` shows it as "since", the timeline counts it as
belief time, the person graph and commitments read it as the time the fact is
about, and the fact materializer turns it into graph activity. There are 85
`valid_to IS NULL` filters across 40 files. The signed permission contracts
`exact_instant_v1`, `stated_day_v1` and `canonical_event_time_v1` read these
exact columns.

## Decision: add explicit fields, redefine nothing

`valid_from`, `valid_to`, `period_start`, `period_end` and `event_at` keep their
current values, formats and meanings at every writer. Changing them would move
every reader above at once, break pinned tests on both sides of the existing
disagreement, and change the meaning of grants already signed.

Two new nullable, versioned columns carry the separated meanings. Producers
that know them write them forward; nothing is backfilled:

- `signal_objects.temporal_json` holds `topos-fact-temporal/v1`;
- `conversation_messages.event_time_json` holds `topos-event-time/v1`.

### Migration

Spec 75 (`storage/db/migrations/temporal_fields_v1.py`) is `always_run`: a
PRAGMA-guarded `ALTER TABLE ... ADD COLUMN <name> TEXT` with no `DEFAULT`, no
`CHECK` and no backfill, so there is no table scan and every existing row stays
NULL. `conversation_messages` is created at runtime by
`ConversationsTablesManager`, not by migrations, so the manager adds its column
too, without ever rolling back the caller's pending writes. Writers check each
column explicitly and write exactly as before where it is absent; FactStore's
pre-B2.1 fallback insert is not widened.

Registering the spec stamps the database's schema version, and an engine that
knows fewer migrations refuses a newer stamp. Like specs 63 and 69, it lands at
a release cut: an engine built from this branch must not run against a node
whose installed engine predates it. The lab database will be stamped 75.
Hosted Postgres rows are out of scope and read as unknown. Any copied-corpus
manifest taken after the migration must be re-reviewed.

### Review stability

Evidence review hashes every non-NULL column of a leaf and a fact, except the
operational ones. A NULL column is not on that surface, so adding the columns
stales no existing review. Both records are written only when a row is inserted,
never by a refresh or corroboration, so a reviewed row does not change beneath
its review.

## The time point

Every temporal value is a **point** with an explicit precision, timezone basis
and provenance. Nothing is rounded, truncated or defaulted into a more precise
form (`features/temporal/points.py`).

```text
point := {
  text        the exact lexical form, or null when unknown
  precision   instant | day | month | year | unknown
  basis       utc | fixed_offset | unrecorded | unknown
  offset      minutes east of UTC, only for fixed_offset
  provenance  native_source_clock | stated_in_content | producer_clock | owner_edit
              | ingestion_clock_substitute | unverified_producer | unknown
}
```

The accepted lexical forms are exactly: an instant with `Z`, `+00:00`, `-00:00`
or a civil numeric offset (UTC−12:00 to UTC+14:00); a naive instant (basis
`unrecorded`); `YYYY-MM-DD`; `YYYY-MM`; `YYYY`; with years 1 to 9999 and 1 to 6
fraction digits. Everything else is `unknown`, including epoch numbers,
space-separated datetimes, leap seconds, calendar-invalid dates and padded text.
Parsing never raises on input. The parser is integer arithmetic over its own
grammar, not `datetime.fromisoformat`, whose accepted fraction lengths differ
between Python versions.

`-00:00` is parsed as `utc`. RFC 3339 uses it to say the UTC instant is known and
the local offset is not, so the instant is exact and there is nothing to widen.
The signed `exact_instant_v1` parser accepts only `Z` and `+00:00` and reads
`-00:00` as unknown, so a capability that evaluates it needs a new contract id
(see the parity scope below).

### Conservative interval rule

A point denotes the set of UTC microseconds it could refer to. Instants are
closed intervals; day, month and year spans are half-open. With an unrecorded
basis the set is widened across the civil offsets in use since standard time,
UTC−12 to UTC+14:

| point | UTC interval |
|---|---|
| instant, `utc` or `fixed_offset` | `[t, t]` |
| instant, `unrecorded` (naive text `L`) | `[L − 14h, L + 12h]` |
| day `D` (always `unrecorded`: the grammar cannot state an offset) | `[D−1 10:00Z, D+1 12:00Z)` |
| month, year | the calendar span, widened the same way |
| unknown | no interval |

Local mean time before standard time could fall outside that range. A contract
that needs certainty for early dates must treat unrecorded points before 1900 as
unknown under its own contract id.

Every comparison is three-valued (`True`, `False`, or `None` for "cannot tell"),
and `None` is never promoted:

- **order(a, b)** is `before` only when `a`'s last possible instant is strictly
  earlier than `b`'s first: `a.hi < b.lo` for a closed `a`, `a.hi <= b.lo` for a
  half-open `a`. `after` is the mirror. Identical or touching instants are
  `overlapping`, never `before`. `None` if either point is unknown.
- **occurred_by(p, anchor)** is `hi <= anchor`, with `hi` the instant itself or
  the exclusive end of a span. For an instant that is `t <= anchor`
  (`exact_instant_v1`). For a day it is `anchor >= D+1 12:00Z` (`stated_day_v1`'s
  `current_from`).
- **event_window(p, anchor, max_age)** mirrors `canonical_event_time_v1` in all
  three values: `None` if `p` is unknown or could lie after the anchor, `False`
  only when all of it ends before `anchor − max_age`, `True` only when all of it
  lies inside `[anchor − max_age, anchor]`, otherwise `None`. For an explicit-UTC
  instant it is exactly the signed `None if event > anchor else lower <= event`.
  A future event must stay unknown, not unmatched: a deny clause ORs its time
  match, so `False` there would let a matching deny silently not match.
- **within(p, lower, upper)** is plain interval membership and makes no claim to
  match any contract. A capability's permit or deny time match uses
  `event_window`, never `within`.

### Contract parity, and where it stops

`tests/permissions_v2/test_temporal_contract_parity.py` pins, over generated
values:

- `occurred_by` agrees with `exact_instant_v1` for `Z` and `+00:00` instants
  whose fraction length the running interpreter's `fromisoformat` accepts;
- `occurred_by` agrees with `stated_day_v1` for bare `YYYY-MM-DD`, and both
  reject the same malformed days;
- `event_window` agrees with the signed `canonical_event_time_v1` expression in
  all three values, including a deny clause through `fact_policy._or/_and` with
  a future event.

The new grammar is wider than the signed parsers: offset instants, `-00:00`, and
1, 2, 4 or 5 fraction digits. Any capability that evaluates these rules on those
forms must be a new contract id. `exact_instant_v1` itself has a known defect: on
Python 3.10, `fromisoformat` accepts only 0, 3 or 6 fraction digits although the
contract text says 1 to 6, so the same signed grant can decide differently on
two interpreters. The test pins that divergence per interpreter. A fix belongs to
a versioned contract, or to pinning the interpreter for signed evaluation.

## The records

```text
topos-fact-temporal/v1 := {
  version:   "topos-fact-temporal/v1"
  asserted:  point   always a known explicit-UTC instant; producer_clock or owner_edit
  applies:   { start: point, end: point }   stated real-world applicability, else unknown
  evidence:  point   event time of the source record the assertion came from, else unknown
}

topos-event-time/v1 := {
  version:   "topos-event-time/v1"
  event:     point
}
```

Both are stored as canonical JSON (sorted keys, no whitespace) and parsed
strictly. A present but malformed record is an error, never a silently absent
one (`features/temporal/records.py`).

## Writers

| writer | `asserted` | `applies` | `evidence` |
|---|---|---|---|
| FactStore.assert_fact, any caller that passes nothing (location aggregate, truth seed) | producer clock | unknown | unknown |
| rule message patterns (`extract_facts_from_batch`) | producer clock | unknown | the source row's `event_at`, as `unverified_producer`, or as `ingestion_clock_substitute` when the row's own record says so and still describes that time |
| profile extractor | producer clock | the stated years, at year precision, `stated_in_content`, on every fact from the row (the payload keeps them only on the employment fact) | unknown |
| journal extractor | producer clock | unknown | `entry_at`, as `unverified_producer` |
| LLM pass | producer clock | the model's `period_start`/`period_end` at their own precision, as `unverified_producer` (a model's reading is not a verified statement) | as for rule patterns |
| owner snapshot lane (`permissions_v2/ingest_snapshot_facts.py`) | producer clock | unknown | the linked row's `event_at` as `native_source_clock` |
| verdict edit | owner edit | carried from the corrected fact | carried from the corrected fact |
| DerivationWriter | not written (NULL, read as unknown) | | |

The shared extractors never record `native_source_clock`, even when a row's
record carries that label: nothing on their path validates it, so it is
recorded as `unverified_producer`. They borrow a stored record only for a row
that names the same dataset and source; a row that names neither cannot show
the record for its message id is its own, and records `unverified_producer`.

For `event_time_json`:

- The legacy canonical writer writes `ingestion_clock_substitute` when it filled
  a missing time, or when staging declared that it did (`local_sync` marks the
  iMessage and Signal staging fills). Otherwise it writes `unverified_producer`.
  It never writes `native_source_clock`, whatever the incoming record says.
  Through the real iMessage reader an undated message never reaches staging (the
  schema validator drops it), so that fill is defensive.
- The owner snapshot lane writes nothing here. Its trusted writer's column
  tuple and `_record_identity` are unchanged, so every existing snapshot link
  still validates. Its native clock is established when it is needed, from the
  validated provenance link, never from this column.

## Readers

1. **FactStore supersession** (`features/facts/evidence_time.py`). The order
   of checks is unchanged: owner exclusion, then a same-value refresh, then the
   0.10 confidence margin, which still queues a `fact_conflicts` row. Only then
   does the new check run, and a challenger that fails it is withheld when its
   evidence is **definitely before** the incumbent's latest supporting evidence.
   It is deliberately narrow, because every wrong refusal keeps a stale belief:

   - It runs only in a store built with an `EvidenceTrust`, which only a caller
     that can prove its rows supplies (today, the owner snapshot lane). Every
     other store refuses nothing, which is today's behaviour, resurrection
     included.
   - Evidence is read when the decision is made, from the rows named in each
     fact's *current* `source_refs`, never from `temporal_json` frozen at insert.
     A refresh never writes `temporal_json`, so the frozen value goes stale as
     refs accumulate. A ref counts only while its row exists, is not deleted or
     excluded, and the ref names that row's source **and** dataset. Every shared
     extractor writes refs without a dataset, and `imessage:<ROWID>` ids repeat
     across databases, so such a ref cannot say which row it means and never
     counts.
   - Only owner statements are ordered, and a ref counts only when its row is
     the owner's own (`is_from_self` 1). A refresh merges refs from any speaker
     into a fact whose label stays `owner`, so the label alone cannot show whose
     message supports it.
   - The only point ordered is `native_source_clock` with an explicit `utc` or
     `fixed_offset` basis, as vouched for by the trust, whose text equals the
     row's `event_at`. `unverified_producer`, `ingestion_clock_substitute`,
     `producer_clock`, `stated_in_content`, `unknown` and any unrecorded basis
     all mean "cannot tell". A substituted time is later than the true one, so it
     would wrongly refuse a genuinely newer challenger.
   - A time is "cannot tell" when it could end after the moment its row is known
     to have existed: the trust's own ceiling when it gives one (the snapshot
     lane gives the moment the owner attested the snapshot's exact bytes, which
     is earlier than the job that ingested them), else the row's explicit
     `ingested_at` (an aware instant, or SQLite's UTC `datetime('now')` form).
     With neither, including a naive or unparseable `ingested_at`, it is "cannot
     tell". That covers a fabricated future time and a device clock running
     ahead. A clock running behind cannot be detected this way: it makes a newer
     statement look older and can wrongly keep a stale belief. The native reader's
     own checks are the only defence against that.
   - Every challenger ref must be trusted, and its latest possible instant is what
     is compared. The incumbent needs one trusted ref: the latest trusted ref is a
     lower bound on its latest support, so "definitely before" stays sound while
     unknown refs are ignored.
   - Multi-valued predicates are never ordered. Their key holds only a
     48-character prefix of the value, so two different values can share a key
     without contradicting each other.

   A withheld challenger is not dropped and is not a contradiction. In the same
   gated commit it is inserted as a **closed historical revision**, with its own
   record and `valid_from = valid_to =` the insertion clock. The incumbent is not
   touched: no close and no `updated_at` change, so its review does not go stale.
   No `as_of` query returns the historical row. `history()` and
   `search(include_closed=True)` do, and past-tense retrieval renders it as
   superseded on its insertion date, which fits `valid_to`'s belief-time meaning.
   Older restatements of a value that already has a closed revision (with the
   same attribution) fold into it: its `source_refs` gain the new refs, and
   nothing else changes (not the record, the interval or `updated_at`; a closed
   row is never a review target). Replaying a message it already holds adds
   nothing at all. Without this, a backfill of older messages filled history and
   past-tense retrieval with copies and pushed the current value out. No `fact_conflicts`
   row is written, because that table has no uniqueness constraint and every
   re-derive would add one. The caller receives the incumbent and counts it as
   written; `FactStore.outcomes` records which case applied
(`older_evidence_kept_as_history`, `older_evidence_corroborated`,
`older_evidence_already_recorded`).

   The resurrection fix therefore applies only when both facts' evidence comes
   from rows an attested native lane proved. It never fires on facts derived from
   legacy sync. The legacy columns still invert whenever a challenger's legacy
   `valid_from` (a raw `event_at` or an invented year) is earlier than the
   incumbent's wall-clock `valid_from`. That is documented behaviour, not
   something this step changes; a correct belief interval can come only from
   the records. DerivationWriter's supersession is out of scope too:
   resurrection remains possible in `derivation_job` facts.

2. **Recorded evidence** (`recorded_evidence`). The shared extractors read the
   source row's `event_time_json`, from the row or by message id with a matching
   dataset and source, so an ingestion-clock substitute stays marked in the fact.
3. **Owner corrections.** `verdicts.edit_fact` reads the corrected fact's record
   and carries its applicability and evidence into the new row, with the
   assertion time as an owner edit. A correction keeps the corrected fact's
   attribution unless the owner changes it. When the new value already has an
   active row (a multi-valued predicate, or a value asserted earlier), the
   correction refreshes that row instead: the row keeps its own record, and takes
   the corrected fact's attribution (or the one the owner names), so the owner's
   statement is never credited to whoever asserted that row.
4. **Permission helpers.** `occurred_by` and `event_window` are the pure functions
   a future capability will evaluate, pinned to the signed contracts' behaviour
   by the parity properties. No signed contract changes in this step.

Owner fact read APIs do not return the records yet. Adding a key changes every
owner-facing fact payload, and there is no consumer for it yet.

## Grantee visibility

Step 5 adds no grantee capability that uses these fields, so neither column is
disclosed to any non-owner, whatever filters the grant carries.
`disclosure/content_policy._strip_internal_privacy_columns` always removes
`event_time_json` and `temporal_json` (`TEMPORAL_RECORD_COLUMNS`), in the one
pass both `SELECT *` grantee readers share: the engine HTTP messages route and
`handle_uma_get_rows`. `timestamp_to_date` and an `event_at` column blocklist are
not relied on to reach into the JSON, and a grant with neither would still
receive nothing new. `uma_get_messages` and the query pipeline keep fixed column
lists and must not add these columns. A future grantee reader that wants them
needs a precision-aware projection under its own signed contract.

## Regression set

- **Precision:** day, month and year stay their own precision through parse,
  record, store and compare; a resume year stays a year while the legacy
  `valid_from` keeps its invented January 1st.
- **Unknown:** missing, empty, malformed, epoch-number, space-separated and
  ambiguous forms are unknown, never compare as known, and never raise in a writer.
- **Timezone boundaries:** a day at the UTC−12 and UTC+14 edges; a naive instant;
  an explicit offset crossing a UTC date; `-00:00`; leap days; invalid calendar
  dates; days two apart overlap, days three apart do not.
- **Endpoints:** equal instants, and the same instant written as `Z` and `+09:00`,
  are `overlapping`; `occurred_by` at `hi − 1` and `hi`.
- **Future-dated assertions:** a future stated applicability has not occurred; a
  future event leaves a deny clause unknown; a fabricated future evidence time
  cannot lock a fact.
- **Re-extraction of old evidence:** with trusted evidence, an older statement
  after a newer one leaves the newer belief current and keeps the older one as
  history, a newest-first reprocess of three statements keeps the newest and all
  three in history, a corroborated incumbent is judged by its latest support,
  twelve older restatements of a superseded value fold into one history row, and
  replaying adds no rows and no conflicts. Through the real snapshot lane, a
  second enrollment holding an older statement is kept as history, a revoked
  newer enrollment no longer holds it back, and a message dated after its own
  snapshot was attested is never ordered. Without trust, today's resurrection is
  pinned as documented behaviour. An oldest-first run is unchanged.
- **What the guard refuses to order:** anything the trust does not vouch for as
  this row's own native clock; a ref missing its source or dataset (including a
  dataset-less ref a shared extractor merged in); another speaker's message;
  non-owner statements; multi-valued predicates; a time after the row's ceiling,
  or any row with no usable ceiling.
- **Corrections that keep history:** an owner edit closes the old row, carries
  applicability and evidence forward, records the edit time separately, and keeps
  the corrected fact's attribution, including when it lands on another speaker's
  existing row.
- **Provenance:** the legacy writer's substitute and unverified marks; a native
  label claimed by a record, or unvalidated in a row, is never repeated.
- **Heal:** a re-ingest never rewrites a body across a dataset or source, or over
  a row with a provenance link.
- **Grantee visibility:** neither record reaches a grantee through either reader,
  with no filters, with `timestamp_to_date`, or with `event_at` blocked.
- **Contract parity:** as above.
- **Review stability:** the migration adds both columns with no default and leaves
  existing rows NULL, idempotently; an owner review recorded before it still
  qualifies after it; a refresh never writes them.

## Not in this step

- `ai_chat_messages` event-time provenance.
- Any historical repair or backfill.
- DerivationWriter records and supersession.
- Owner fact read APIs returning the records.
- A new signed fact-validity or event-time contract. That belongs to the first
  capability that needs one, and the parity properties exist so it can be built
  on this rule.
- Rewriting the readers that disagree about `valid_from`. They are recorded
  above, and moving them is a product decision per surface.
