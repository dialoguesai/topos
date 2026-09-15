# Owner-attested entity coverage design, v1

Status: design only. No coverage attestation, storage, API, capability or
recipient path described below is implemented. The current global entity
Off-limits withholding in `evidence.py` remains in force. This proposal is a
future owner-attested semantic boundary, not a machine certificate that arbitrary
prose contains no protected information.

## Why the existing observations cannot authorize absence

`entity_mentions` records positive resolver observations. Its legacy schema
permits missing source/table identity, lacks dataset/resource and content-revision
binding, and has no completeness marker. Its uniqueness key does not distinguish
source/table. An empty join therefore means no recorded match, not no mention.
Confidence, an empty NER result, or a substring miss cannot repair that gap.

`OwnerEvidenceReview.classifications.subject_entity_ids` describes aboutness. It
does not inventory every person, organization, place, alias, quoted speaker,
pronoun or indirect reference in the record. Existing self-only classifications
must never migrate into completed entity coverage automatically.

Protection is also name-based: an owner may protect a name before an entity ID
exists, and the restriction must survive merges, deletions and reminting. Merely
checking current entity IDs would lose that intent. The existing broad veto is
therefore necessary for this uncertified family.

## Minimum future boundary

The initial useful candidate is a short owner-authored atomic preference with a
small, fully inspectable evidence closure. Arbitrary paragraphs, hidden context,
incomplete references, mixed content and unsupported surfaces remain withheld.
Both raw source output and the proposed scalar require their own registered
surface contract; scalar projection does not sanitize its supporting evidence.

For a candidate closure C and exact output O, the necessary condition is:

```text
every artifact, terminal source, required context and output has current coverage
AND every coverage inventory is complete under its registered surface contract
AND no unresolved reference or unknown lineage remains
AND no covered/dependent item touches any current protected selector
AND no record/ancestor is Off-limits
AND the ordinary correlated evidence-use and output-release rules permit it
```

This is a conjunction. An output review, policy grant, natural-language filter,
processing locality or model decision cannot override an Off-limits veto.
Known positive matches remain vetoes even if a negative attestation contradicts
them. An ambiguous protected term is withheld; disambiguation must not become a
hidden exception that weakens the owner's selector.

## Proposed private attestation shape

Use a distinct closed `topos-owner-entity-coverage/v1` schema in a future change:

| Field | Binding |
| --- | --- |
| `owner_id`, `review_id`, `review_generation`, `status`, `reviewed_at` | Authenticated owner mutation and durable current review |
| `binding`, `canonical_file_revision` | Exact environment, node, resource, owner and canonical-file incarnation |
| `closure_revision` | Every artifact, leaf, required context identity/revision and dependency edge |
| `surfaces` | Full evidence identity/revision, registered surface-schema version and exact surface hash for each reviewed item |
| `references` | Complete inventory of resolved entity identities and relevant preemptive name-selector identities |
| `completeness` | Explicit `owner_attested`; never inferred from a model or a missing database row |
| `unresolved_references` | Must be empty for eligibility; unknown context is not an empty inventory |
| `identity_universe_revision` | Entity resolution, aliases, merge/remint history and normalizer/schema version |
| `protection_revision` | Current record/entity selectors and monotonic protection generation |
| `projection_version`, `output_hash` | Exact proposed output when the attestation covers a projection |

These are design fields, not additions to the signed P2a grammar. The eventual
parser must reject unknown fields, duplicate identities, incomplete pairs,
unbounded inventories and unsupported surface versions. A constructed JSON object
or matching hash establishes consistency, never authenticated owner provenance.
The runtime must load the current attestation itself from a private owner store.

The owner preview must display all declared surfaces and relevant context without
truncation, including titles, quote/forward metadata and contextual references
where the registered adapter depends on them. Hashing an undisplayed parent does
not make its semantic contents reviewed. A row that needs an unresolved pronoun's
antecedent cannot be declared independent of that context. Unsupported or
oversized closures cannot receive a completed attestation through a partial view.

A local model may propose an inventory for review. It cannot set completion,
approve its own interpretation, invent reference IDs or waive a positive match.
No model is required for the initial owner review path. Human attestation is also
fallible; the product must not claim universal absence was mathematically proved.
If owner-attested semantic completeness is not an acceptable trust boundary,
automatic relaxation for unstructured prose remains unsupported.

## Restricted lineage and current-state checks

Every full record and all recursive dependencies must be covered. The inventory
must include semantic references and influences, not only literal named spans.
Known independently copied or rephrased facts remain governed by the existing
copy/lineage restrictions. A graph/vector/summary with incomplete dependencies
cannot borrow a source record's certificate. A protected parent protects every
slice; substring redaction is not a proof of independent support.

Legacy mention matches are useful conservative veto signals. They are never a
permission proof: collisions across source/table/dataset require disambiguation
with full identity or withholding, not selection of a convenient match.

The current protection clock watches `owner_only_records` and `entity_blackholes`.
It does not watch entity aliases, resolution merges or `entity_mentions` edits.
A future implementation needs a separate monotonic identity/attribution clock or
an explicitly versioned clock extension. Its identity and high-water mark must
survive restart; missing triggers, reset, rollback and replacement fail closed.

New aliases, merges, reminted IDs, mention edits, normalizer changes, selector
changes, row/context/projection changes and review revocation invalidate coverage.
Initially a global identity-universe generation is conservative and simple.
Narrowing invalidation to a dependency read-set is a later optimization requiring
its own proof; unchanged text alone is insufficient.

The resolver must read this state from the same canonical snapshot and include
coverage revisions in the qualified evidence/candidate revision. Final dispatch
reloads current owner review and current restrictions under the existing
resolver/review/write gates. Long model work occurs outside those gates, followed
by complete requalification. Transport guarantees remain send-start ordering,
not recall of bytes already dispatched to a recipient.

Only after this validator enforces all obligations may a future version replace
both global entity checks in `_snapshot` and `_eligible` with the bounded
coverage decision. No skip flag, legacy fallback, recipient-supplied review or
silent migration is acceptable. Unregistered families keep the existing floor.

## Remaining profile and projection gaps

Coverage addresses an owner-wide restriction; it does not create data access.
The source-message release still requires raw permission and does not satisfy the
planned reading facts/summaries profile. The proposed exact `prefers` scalar is
unmounted and has no signed output authority. Finance, work and availability need
their own closed forms and evidence contracts.

The planned 180/365/30/14-day client profile windows also remain separate work.
Current P2a validity timestamps bound grant lifetime, not the event times of
contributing evidence. The next schema needs exact evidence tables separately
from output forms and an explicit source-event-time window. Missing or ambiguous
leaf timestamps, future evidence and unresolved fact validity withhold. Do not
substitute fact creation time or grant expiry for source event time.

See [the scalar projection design](FACT_PROJECTION_DESIGN.md) for the proposed
event-window semantics, output review and separate evidence/output rule changes.
None of these gaps is closed by approving entity coverage.

## Required acceptance tests before any enablement

- Positive control: an unrelated, fully owner-reviewed atomic record remains
  eligible while a separate protected entity exists.
- A protected entity by ID, alias, preemptive name, indirect reference, quote,
  object relation, parent context or contributing ancestor always withholds.
- Empty/missing mention tables, partial source identity, unknown references,
  truncated preview and false model negatives never issue coverage.
- Existing self-only reviews, arbitrary caller objects and model-created review
  IDs cannot migrate into or impersonate owner-attested coverage.
- Normalization drift, new aliases, merges/remints, cross-dataset ID collisions,
  context edits and protect/lift ABA invalidate old approvals and cached results.
- Owner approval cannot override a known protected match, protected parent,
  unknown copy lineage, non-authored role or restrictive source posture.
- Review revocation, identity changes and protection writes during model work or
  before final dispatch prevent release; restart and failed clock reads fail closed.
- Differential protected/absent canaries have the same recipient denial shape;
  reason codes, inventories, counts, ordering, snippets and errors expose no
  protected values. Owner diagnostics remain on the authenticated owner channel.
