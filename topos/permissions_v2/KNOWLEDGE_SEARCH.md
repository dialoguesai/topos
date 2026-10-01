# Filtered knowledge search candidate

`permissions-beta/p2c-v3` is a separately agreed capability. Existing p2c-v1 and
p2c-v2 grants retain their original meaning. The owner-facing `knowledge_search`
profile keeps source IDs, categories, sensitivity, expiry and rolling evidence
window, but changes eligible result families and classification semantics.

## Release boundary

- Only the signed grant's message, fact, goal and relationship projections can
  reach `canonical.knowledge_search.v1`; at most the grant's signed `max_k` results
  per request. A grant may sign up to 20 (`KNOWLEDGE_MAX_K`; 10 before 30 Sep 2026).
  A grant signed at 10 keeps 10 until the owner re-consents, and a `k` above the
  grant's `max_k` is the uniform refusal.
- Messages need native source provenance and a current whole-message assessment.
  Machine records have a separate namespace and cannot impersonate owner reviews.
  An explicit owner correction takes precedence. Unknown, quoted, protected and
  otherwise excluded content remains withheld.
- Automatic assessment runs against the pinned local loopback model, with bounded
  neighboring context and the owner's protected aliases. Neither that context nor
  the protected list is an output field or sent to a hosted model. Source, context,
  model, rubric and owner-correction revisions bind each assessment. For an AI-chat
  prompt the context is the owner's own adjacent turns, never the assistant's
  replies (OD-54).
- Facts and goals need complete, independently permitted message support. Legacy
  source references are completed only when exactly one canonical source identity
  matches, then that source still needs native provenance. No source authority is
  manufactured by reference resolution.
- With the journal family on (IF-5), the support may also be the owner's journal
  entry. The entry is qualified exactly as a journal member is (its owner proof,
  the NSFW hard withhold, owner-only, exclusions, Off-limits over every column, its
  own assessment), is inside the window only by every instant its stated day can
  denote, and is cited as a record: its own opaque ID and whole text, dated at most
  by its day. Such an item releases only under a grant that signs `journal_entry`;
  otherwise it is withheld (`journal_citation_needs_record_option`), even beside a
  message. A citation of a same-source twin resolves to the member, and the twin's
  own vetoes still apply.
- With `TOPOS_PERMISSIONS_V2_JOURNAL_GOAL_FIELD` also on (Lane H1, default off), a
  goal citing a journal entry is grounded when it is the entry's structured goal
  field verbatim: the time-log `goal` rendered as the first paragraph ("Goal: ...")
  and stored as `metadata_json.goal`, the two equal. The field must clear
  `journal_goal_field.refusal` (Off-limits, special categories, speech acts, third
  parties, a closed vocabulary, an intention's shape) besides every check above.
  The lane's model-free step stores one such goal per qualifying entry
  (`permitted-derivation`, operation `journal_goal_field`).
- With `TOPOS_PERMISSIONS_V2_DERIVED_FACTS` also on (IF-6 v1, owner decision OD-63,
  default off, inert without the journal family), a fact the node's extractor drew
  from exactly one journal entry releases even though the entry does not state it,
  marked `assertion: "inferred"` (the existing `FactResult` value; v1 adds no wire
  field, because the CP relay and the recipient app refuse an unknown key). The
  grant needs nothing beyond the "Journal entries" option, whose recipient consent
  text already names "the facts and goals drawn from them". Every check a stated
  fact runs still runs first, the fact's implicit labels still meet the grant's
  decision, and only the stated floor and OD-38 are dropped. The value must then
  clear `inferred_facts.refusal`, in this order: the entry's own labels
  (owner-authored original wording, nothing protected, sensitivity none or
  personal), one plain Latin label, Off-limits over the value and the item's wire
  content (a bare part of an Off-limits name included, as for a journal entry),
  special categories, questions and quotes, URLs, templates and
  placeholders, and any person (the node's people and contacts, the entry's people
  column, relation and role words, honorifics, possessives, and capitalised words
  where the predicate expects no proper noun). A fact citing more than one source
  is out of scope (`inferred_fact_scope`); a fact citing more than 20 records is
  refused before any grounding rule, as today. The guards' version is part of the
  index basis while the flag is on, and the refresh loop queues a rebuild when the
  node's facts move (cause `facts_changed`). Known residual: a person the node does
  not know, named only as the proper-noun value of an employer, school, city,
  membership or project fact, is not caught.
- The first structured adapters support conservative owner-stated fact predicates,
  stated intentions, and owner-to-goal relationships. They do not yet cover every
  fact predicate or graph edge. A visit cannot become a residence assertion, and
  an intention cannot become a completed goal.
- Each structured result includes permitted supporting messages with grant-specific
  opaque IDs. Canonical IDs, hidden graph endpoints, arbitrary fields and private
  withholding reasons are not recipient output.
- Index publication and release both recheck evidence and projection freshness.
  Existing signed authority, live consent, revocation, expiry, request binding and
  read budgets remain in force.

## Classification and owner corrections

An owner can start a bounded background pass over already stored recent messages.
It does not ingest source history or activate a grant. Completed assessments are
reused when the same window is resumed. The owner can inspect machine labels,
correct them, or exclude messages. The initial worker is owner-started; continuous
classification of newly ingested data is not yet scheduled automatically.

## Verification and known limits

The tests include real signed node releases for all four result types, cross-type
exclusion propagation, stale claims, incomplete second sources, changed model
revision, owner corrections, denied recipient management calls, closed schema
validation and CP-to-node schema parity. The small independent synthetic corpus
also runs through the actual local model. It is a useful regression check, not a
proof that semantic classification never makes an error.

A successful build is not live acceptance. Activation and several useful cited
answers in the recipient UI, checked against an owner-side release audit, are still
required. Native provenance coverage for older ChatGPT imports remains separate
from source ownership confirmation. Lexical retrieval covers the permitted index;
local vector generation currently has an owner-maintenance budget per build.
