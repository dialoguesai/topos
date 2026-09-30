# Filtered knowledge search candidate

`permissions-beta/p2c-v3` is a separately agreed capability. Existing p2c-v1 and
p2c-v2 grants retain their original meaning. The owner-facing `knowledge_search`
profile keeps source IDs, categories, sensitivity, expiry and rolling evidence
window, but changes eligible result families and classification semantics.

## Release boundary

- Only the signed grant's message, fact, goal and relationship projections can
  reach `canonical.knowledge_search.v1`; at most ten results per request.
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
