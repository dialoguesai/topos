# Selective fact sharing — 1.5.5 target

Implementation is reviewable; activation is **not ready**. Node and control-plane switches default off. On 2026-10-11 the owner explicitly directed that the full release gate remain pending after two attempts detected concurrent changes in the read-only metadata fingerprint of the owner's node. No guard was weakened. This is not a passing privacy-rig result.

## Signed consent and compatibility

The strict `permissions-beta/p2c-v3` search contract gains optional `selective_facts` with `version: selective-facts/v1`, `vocabulary: fact-ontology-vocabulary/v1`, catalog digest `a09987c6fbfd1442e92ec485c9ca8c0e6bcfab32a222cf482f875af3191136ba`, exact ontology semantic revisions and predicate sets, and grant-specific `excluded_fact_ids`. Null, unknown keys, duplicates, stale digests, unknown ontologies and altered predicates refuse. The absence of the field preserves legacy signed bytes. An older strict parser rejects a new selective declaration; explicit catalog negotiation prevents offering it to incompatible nodes.

No ontology is selected by default. Legacy fact-bearing policies remain parseable for custody and revocation but release no facts without explicit new consent. Other authorized output kinds continue. Changes to ontology meaning or predicates require a new revision and owner review; never widen this frozen vocabulary in place. Excluding an item narrows one grant; removing an exclusion requires owner review.

## Admission inventory

Ontology identity is not evidence of ownership, native grounding or permission. The initial admission table covers all 28 bundled root ontologies. Unavailable entries have no eligible counts or private existence signals in the catalog.

| Ontology | Owner-facing name | Initial disposition |
| --- | --- | --- |
| work.career | Work and career | admitted, owner-stated only |
| admin.legal | Legal and administration | special_category |
| aspirations.goals | Goals and aspirations | unsupported_grounding |
| behavior.habits | Habits | inferred_or_owner_only |
| behavior.preferences | Preferences | inferred_or_owner_only |
| beliefs.civic | Civic beliefs | special_category |
| beliefs.spiritual | Spiritual beliefs | special_category |
| communication.style | Communication style | inferred_or_owner_only |
| community.membership | Community membership | special_category |
| craft.tooling | Tools and craft | unsupported_grounding |
| expertise.knowledge | Knowledge and expertise | unsupported_grounding |
| finances.economics | Finances | special_category |
| health.lifestyle | Health and lifestyle | special_category |
| health.mental | Mental health | special_category |
| health.physical | Physical health | special_category |
| household.pets | Household and pets | unsupported_grounding |
| identity.core | Identity | special_category |
| interests.taste | Interests and taste | unsupported_grounding |
| life.events | Life events | unsupported_grounding |
| narrative.self | Personal narrative | inferred_or_owner_only |
| net.capability | Capabilities | inferred_or_owner_only |
| net.character | Character | inferred_or_owner_only |
| obligations.commitments | Commitments | unsupported_grounding |
| personality.traits | Personality | inferred_or_owner_only |
| places.mobility | Places and travel | unsupported_grounding |
| possessions.assets | Possessions | special_category |
| relationships.social | Social relationships | third_party_subject |
| values.motivation | Values and motivations | special_category |

Work and career freezes `works_at`, `worked_at`, `works_on`, and `work.project` under `work-career-sharing/v1`. Supported producers are exact pack `work.career` version `0.2.3` with stated altitude and matching pack metadata, and the explicit `fact_store_v1` legacy mapping `fact-store-work-mapping/v1` for the first three predicates. `work.role`, other pack versions, predicted/inferred facts, fact dependencies and unsupported structured values are unavailable.

The production positive test exercises the signed native snapshot owner lane, real scoped extractor, owner-source enrollment, machine assessment, ordinary Facts-only search and later source revocation. It does not rewrite an owner-only fact to scoped disclosure. Eligibility still requires an authored owner assertion and a complete atomic native source sentence. Unknown or conflicting producer metadata refuses. Private owner entailment inspection may still collect qualified candidates, but neither a model verdict nor owner confirmation can substitute for native stated grounding in selective sharing. Ordinary search installs no such collector.

## The owner's ontology switches

Signed consent names an ontology; it does not turn one on. A fact releases only while its ontology is also switched on at this node, which is the derivation registry's `enabled` column (`pack_registry`, the switch the Lenses page sets). `selective_facts.enabled_ontologies` reads it on the snapshot that decides. A registry that is missing or cannot be read names no ontology, so nothing is shared on the strength of an unknown state. A legacy `fact_store_v1` work fact maps to `work.career` and follows that ontology's switch.

The check runs in `fact_projection` after `authorize`, so it holds at the three places a fact is decided: the index build, the owner's count and preview, and the release's own re-decision of each candidate. The refusal is `fact_ontology_off`; the owner's preview shows it per item and the count files it under held back, as the owner's own choice. Because the release re-decides every candidate, and a written answer's fetch walks its items through the same release checks, switching an ontology off stops its facts at the next request, cached answers included.

A switch refuses no index and no share stops serving for it. No index basis binds the switches. The refresh loop's fact digest (`refresh_loop.fact_digest`) reads the admitted ontologies that are on, so switching one queues a build of the indexes that could hold its facts (cause `facts_changed`) while the index that is there keeps serving: after switching off it still lists facts the release refuses until that build; after switching on, the facts arrive with that build. Without the refresh loop, switching on takes effect at the next build by the owner. An earlier draft bound the switches in the basis instead; that made the whole share refuse until its index was rebuilt, tens of minutes on a busy node, and was dropped. Switching an ontology outside this release moves nothing, since it shares nothing either way. Switching one back on restores what the share signed; no new consent is taken, because the signed selection never changed. The digest carries the switch state only while an admitted ontology is off or the registry is unreadable, so on a node with them all on (the default) it is the digest kept before, and the upgrade queues nothing.

Between a switch off and its build, the index still ranks that ontology's facts. A candidate the release refuses because its ontology is off is walked past without using up the candidate window (`search_release.candidate_limits`, up to `OFF_ONTOLOGY_PASSES`, 500, per query), so the share's other records are released as they will be once the build has left the facts out. `search_lanes.rank` hands the walk those spare candidates after the ones it always read, in the same order, reaching below a lane's depth of 200 for them; the candidates before them are unchanged, so a share whose ontologies are on is answered exactly as before. Any other refusal counts as before, and a share without fact consent ranks no spare candidate. The build itself is a refresh, not a restore: it waits on the refresh's own rate limit and never takes the slot a dropped index needs. It shares that slot with `review_added` builds, so either can wait up to the refresh interval behind the other. A fact switched back on within the refresh interval of the previous build returns when that interval ends (300 seconds on a default node), not after the 30 second debounce.

Known limit: facts count against a share's record cap. A share built at its cap while an ontology was off goes over the cap when the ontology is switched on, and an over-cap share refuses until the owner acts. The production cap is 5,000 records. A request in flight at the moment any build publishes is refused once; that is the index door's existing behaviour, not specific to a switch. The owner's preview lists a fact whose source is excluded, Off-limits or owner-only with its reason, while the count's reply has no number for it (the count's class "never" is not carried in the reply); that difference is older than this work and unchanged.

Older nodes. A node from 1.5.5 to 1.5.8 has the facts switch and no check of the owner's ontology switches: with its switch set by hand it shares the facts of an ontology its owner switched off (`test_ft168_old_nodes.py`). A node with the rule therefore says so: `permissions_v2_fact_ontology_switches`, the integer `selective_facts.ONTOLOGY_SWITCHES_VERSION`, in its registration and every heartbeat beside the answer version. The control plane offers Facts, accepts a new share with fact consent and forwards a fact only for a node that said it since the control plane last started; a node it has not heard from is treated as not having said it. The word is about what the code does, so it does not follow the facts switch, and it is never in the catalog reply.

The catalog reply stays data-independent and byte-identical to the control plane's mirror: it does not say which ontologies are on, so the control plane learns nothing from it. The app lists only the ontologies that are on by reading the owner's own registry through the existing owner route (`GET /derivation/packs`). That filter is display; the node's check is the enforcement. A share whose signed selection names an ontology that is off is still accepted and installed (the signed bytes are valid) and releases none of that ontology's facts while it stays off.

Tests: `tests/permissions_v2/test_selective_facts.py` (off, never listed and missing registry; facts stopped at once while the share serves on, and restored after a build; an ontology outside the release; the owner's preview; a cached answer in both modes) and `tests/permissions_v2/test_inferred_facts_refresh.py` (the digest moves with an admitted switch only; a switch queues one build and drops no index; the digest of a node with every admitted ontology on is the one kept before); `tests/permissions_v2/test_ft168_switch_loops.py` (the independent test lane's run with the node's own sweep and refresh loop threads; two strict expected failures remain for the two known limits above); `test_ft168_exposure.py` (87 cases: a fact whose source the share would withhold never leaves, and what the owner is told) and `test_ft168_fix_attacks.py` (the two fixes from outside, byte for byte against the commit before them, and the reach past 200 switched-off facts).

## Evidence and dispatch

Permitted source evidence and delivered output kinds are independent. A Facts-only share may qualify a journal source without authorizing delivery of the journal entry itself. Every underlying source still passes native provenance, owner subject/authorship, source consent, time window, whole-source sensitivity and NSFW restrictions, Off-limits, copy restrictions, machine review and owner overrides, and complete terminal lineage. A mixed restricted source cannot be laundered through a narrow work fact.

Fact citations contain only the qualified fact statement, not the raw supporting paragraph. Independently authorized message, journal, goal, relationship and interest outputs retain their own controls. Ontology selection does not promise to redact facts from separately authorized raw outputs.

Index basis and send tokens bind the vocabulary and activation switch. Current permissions are rechecked at retrieval, answer context, cached answer fetch and final node/CP dispatch. The final task checks its own switch immediately before sending, including answers-only bodies with no fact records. Owner previews reuse the same frozen qualifier and show per-item refusal reasons; source excerpts appear only for qualified facts. Preview pages hold up to 100 candidates and signed exclusions up to 500 IDs.

## Activation and release order

1. Review and deploy compatible node and control-plane contracts with both switches off.
2. Deploy app controls, which require current compatible node metadata and control-plane activation before Facts is offered.
3. Complete and adjudicate the full privacy rig and evaluation below.
4. Enable `TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED` on the node and `PERMISSIONS_BETA_V2_SELECTIVE_FACTS_ENABLED` on the control plane only after acceptance. Existing master/source/search/answer switches and consent remain required.

Disabling either selective switch blocks fact delivery and answered selective responses, including cached answers. It preserves unrelated non-fact sharing. This branch adds an Unreleased entry for the 1.5.5 target; it does not publish or bump a release.

## Evidence and pending gates

The journal privacy regressions now sign explicit selective consent and create scoped fixtures rather than assume implicit owner-only delivery. The old IF-6 public inferred-release expectations are replaced by legacy-consent and explicit-selection refusals; the private inference guard cases remain tested. Relevant node contract, search, counts, answer, transport and switch regressions ran: 402 tests passed in the broad run, with its one inventory expectation subsequently corrected and verified in the focused run. The final focused suite passed 61 checks including new selective tests, counts, legacy search, inventory and the report benchmark. The migrated fact/journal/inference/entailment/send-token suite passed 489 checks. Companion CP and app PRs record their validation.

`tests/permissions_v2/test_fact_sharing_report.py` writes a reproducible JSON sample when `FACT_SHARING_REPORT` is set. It has one distinct native owner-stated `works_on` fact and 12 requests per arm, the same deterministic writer and non-fact outputs. The facts-off arm excludes that candidate in signed consent while activation stays on. It demonstrates qualified delivery and reports search/answer p50/p95, but usefulness delta is zero. Zero observed leaks in this tiny sample is not a privacy or usefulness activation verdict.

Still pending: all six directed owner pairs in the isolated three-owner privacy rig; equivalent values across ontology classes; mixed/forged lineage and stale semantics; catalog negotiation and widening races; representative per-ontology/per-predicate denominators and independent adjudication; matched production-writer answers; and declared workload p50/p95 budgets. These must be completed before activation. The metadata fingerprint must be stable during the full rig; unrelated running owner work may invalidate a run even with zero observed owner contact.

Run focused checks from the node root:

```sh
python -m pytest tests/permissions_v2/test_selective_facts.py tests/permissions_v2/test_n4_share_counts.py tests/permissions_v2/test_knowledge_search.py -q
FACT_SHARING_REPORT=/tmp/fact-sharing-evaluation.json python -m pytest tests/permissions_v2/test_fact_sharing_report.py -q
```
