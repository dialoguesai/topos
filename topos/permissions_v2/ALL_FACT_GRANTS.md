# All fact categories in grants

The original `selective-facts/v1` contract admitted only four work predicates.
The other 27 catalog entries were unavailable, and the two Moments packs were
absent. Enabling a pack for derivation did not give its facts a grant projection.

`selective-facts/v2` offers all 30 bundled categories and their 237 pack
predicates, plus an explicit mapping for the 12 legacy FactStore predicates.
The node and control plane carry identical frozen catalogs. Consent pins the
catalog digest, each category's revision and predicate set, selected assertion
kinds, and individual fact exclusions. A pack upgrade cannot silently grow an
existing grant. The v1 parser, digest, work predicates and output remain intact.
Moving a draft from v1 to v2 requires review even if it still selects only work.

## Release behavior

- The owner chooses categories and whether to include supported owner statements,
  extracted/inferred facts, or both. Nothing selects all categories automatically.
- A claim matching the existing exact owner-statement grammar can be labelled
  `owner_stated`. Other stored extractions are labelled `inferred`, with visible
  wording saying “Extracted or inferred”; extraction altitude alone never proves
  an owner statement. This is permission to disclose a stored claim, not a new
  truth-verification mechanism.
- Structured values use only fields frozen in the selected predicate schema.
  Internal payload metadata and raw source text do not become fact citations.
  Known entity references render their names, with those entities' protections
  and revisions checked too. Other-person facts retain their named subject.
- Pack writers use `disclosure=owner_only` for every extraction by default. New
  v2 consent can authorize those stored values. Independent `owner_only_records`,
  exclusions, review opt-outs, Off-limits, source provenance and source reviews
  still veto release. v1 retains its original `scoped` requirement.
- Every dependency in a derived fact chain must also be in the grant's selected
  categories, current, enabled and unprotected. Native leaves must all qualify
  under one common permit clause, including the existing owner-authored native
  source requirement. Incoming third-party messages do not gain source permission
  merely because their derived fact category was selected. Cycles and
  excessive/incomplete chains refuse.
- A fresh pack fact whose validity contains only a day can qualify on that day
  only when its permitted native evidence supplies an exact timestamp on that
  same day. Future or unproven validity still refuses.
- Sensitive fact labels are combined conservatively with source labels. Selecting
  a category never overrides the grant's topics, sensitivity levels or exclusions.
- Index revision checks bind the fact dependency closure and named entities.
  Disabling any selected category is rechecked at release before a rebuilt index
  is available. The final node and control-plane checks enforce signed assertion
  kinds and category/predicate membership and reject raw-text fact citations.

## Compatibility and rollout

Deploy the node, control-plane and web changes together. Catalog negotiation
refuses mismatched catalogs rather than guessing compatibility with an older
node. Existing signed v1 grants still parse and retain their scope; existing
indexes rebuild for the new implementation revision. No migration changes
stored facts, enables a pack, edits a live grant or turns off protections.

A category being available does not promise every stored row can be shared.
Missing native provenance, missing reviews, unsupported source tables, unknown
subjects, malformed values, closed facts and denied dependencies still withhold.
The owner preview reports these restrictions. An extraction/inference is never
promoted to a verified statement just to make it shareable.

## Verification

`test_all_fact_grants.py` exercises all 30 categories through signed search,
all 237 registered value projections, old consent, closed v2 grammar, sensitive
consent, individual exclusions, global marks, category switches, dependency
chains/cycles, other-person facts and the control-plane output boundary. The
web suite checks catalog compatibility and the explicit fact-type selection.
The paired response-quality reports and change log record broader verification
and any limitations of the separately run real-model companion gate.
