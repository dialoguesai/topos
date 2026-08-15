# Design — Schema-Grounded Query Generator

**Status:** PROPOSED
**Date:** 2026-08-13
**Parent:** [`PLAN_SCOPE_CLASSIFIER.md`](PLAN_SCOPE_CLASSIFIER.md) §6.5c
**Related:** [`AUDIT_ROLE_COMPETENCE_CATALOG_V3.md`](AUDIT_ROLE_COMPETENCE_CATALOG_V3.md)

Generates labelled scope-classification cases from the node's own schema. Covers the **nine scopes
no public corpus reaches** — `health`, `activity`, `ai_conversations`, `attention`, `complexity`,
`work_context`, `public_bio`, `relationship_context`, `resources` — plus their near-miss negatives.

Gold is an **input**, never an inference. This is the P1 discipline applied to generation.

---

## 1. Grounding inputs — what the schema actually gives us

Five real sources, in descending order of value.

**(a) `topos/topos/features/signal/definitions/*.json` — the askable attributes.** The richest
input by far. Each typed dimension declares `core_question`, entity types with **`fields`**, and
`signal_objects`:

| dimension | core_question | entity types → fields |
|---|---|---|
| `relationships` | "Who do they know and how warm or active are those ties?" | `RelationshipEdge`{tier, warmth_band, cadence_band, last_interaction_at}, `NetworkCluster`{cluster_label, member_count, dominant_context} |
| `profile` | "Who is this person professionally and what can they credibly claim?" | `SkillNode`{label, proficiency_band, evidence_count}, `ExperienceNode`{title_band, organization_band, seniority_band, started_at, ended_at}, `Credential`{credential_kind, label, issuer_band} |
| `work` | "What are they building and how much capacity do they have?" | `ProjectLoad`{project_label, domain_tags, load_band, status}, `DomainFocus`{domain_tags, focus_strength}, `CapacityBand`{capacity_band, confidence, computed_at} |
| `intentions` | "What do they want next and what are they actively seeking?" | `Goal`{goal_text, horizon, status, confidence}, `SeekingSignal`{seeking_kind, domain_tags, urgency_band}, `Constraint`{constraint_kind, description_band} |
| `time` | "When are they busy, free, and predictably available — and which busy time is negotiable?" | `AvailabilityWindow`{start, end, availability_kind, hard_or_soft, movability_band, attendance_priority}, `Commitment`{kind, recurrence, load_weight}, `RoutinePattern`{day_of_week, time_band, frequency} |

**(b) `scope_registry.json`** — `primary_dimensions` (scope → dimension), `signal_objects`,
`summary_objects`, `inference_objects`, `raw_tables`, `default_source_ids`, `default_mode_ceiling`.

**(c) `dimension_registry.py`** — `DIMENSION_CANONICAL_TABLES` (e.g. `wellbeing` →
`journal_entries, sleep_session, mood_entry, activity_log`), `DIMENSION_SIGNAL_OBJECTS`.

**(d) `shared/schema_registry.py`** — 9 raw/connector tables with `CATEGORY_INFORMATIONAL`
(askable) vs `CATEGORY_ORGANIZATIONAL` (plumbing) columns. Only the informational ones generate.

**(e) `sources/registry.py`** — `allowed_scope_ids` per connector. **This is the multi-label
oracle** (§4).

### 1.1 The blocking gap, and it is worth knowing now

Only **5 of 10** dimensions have typed definitions with `fields`. Missing: **`interests`,
`memory`, `wellbeing`, `resources`, `places`.**

Cross-referenced against the nine target scopes:

| scope | dimension | typed definition? |
|---|---|---|
| `public_bio:read` | profile | ✅ |
| `relationship_context:read` | relationships | ✅ |
| `work_context:read` | work | ✅ |
| `ai_conversations:read` | memory + work | ◐ work only |
| `activity:read` | profile + interests | ◐ profile only |
| `health:read` | wellbeing | ✗ |
| `resources:read` | resources | ✗ |
| `attention:read` | interests | ✗ |
| `complexity:read` | interests | ✗ |

**Four of the nine are ungeneratable at quality today.** Writing the five missing dimension
definitions is the real prerequisite — and it is work worth doing regardless of the classifier,
because the engine's own signal derivation consumes the same artifact. That is the concrete form
of "schema design quality determines model effectiveness."

`DIMENSION_CANONICAL_TABLES` gives a weaker fallback for the missing five (table names, no fields).
Use it to bootstrap, but mark those cases `grounding: weak` in provenance so their contribution is
measurable and capped.

---

## 2. The generative form

A case is a composition of typed axes. Gold is known because it is an input.

```
(scope, askable, lens, timeframe, surface, register) ──▶ utterance,  gold = {scope}
```

| axis | source | example values |
|---|---|---|
| **scope** | scope_registry | `relationship_context:read` |
| **askable** | dimension `fields` / signal_objects / informational columns | `warmth_band` |
| **lens** | authored (6) | lookup, trend, comparison, aggregate, existence, ranking |
| **timeframe** | authored (7) | ∅, today, this week, last month, since <event>, a date, "lately" |
| **surface** | authored (5) | interrogative, imperative, elliptical, narrative, fragment |
| **register** | **mined from MASSIVE/CLINC150** | phrasing templates transferred from `calendar_query`, `email_query` surface forms |

The register axis is where the public corpora earn their keep for the nine uncovered scopes: they
supply *how people phrase assistant requests*, which transfers across topic even though their
*intents* do not.

### 2.1 The naturalization rule — the single most important constraint

**A field name is a coverage index, never a token in the output.** Users say "how close am I with
Priya", not `warmth_band`.

Every askable carries a small hand-authored **realization lexicon**:

```python
"warmth_band": [
    "how close am I with {person}",
    "am I drifting from {person}",
    "how warm is things with {person}",
    "are {person} and I still tight",
    "where do {person} and I stand",
]
```

This lexicon is the human-authored core; everything else is combinatorial. It is also the exact
thing v3 got wrong: there, the distinctive token *was* the answer key, so 74% of oracle rule keys
matched one case. Here the lexicon is deliberately written to **share vocabulary across scopes**,
and §5 enforces it.

**Refined during authoring (2026-08-13).** The leak gate bans `askable` and `entity_type` only —
the names that exist nowhere but the schema. It does **not** ban dimension or scope words:
"attention", "focus", "profile", "relationship" are ordinary English an owner really says, and
forbidding them buys triviality in the other direction, an unnatural catalog. Those are governed
by the single-scope-giveaway gate instead, which permits a natural word exactly when more than one
scope uses it — the property that actually matters, since a shared token cannot be an answer key.
Where a type name collides with ordinary English (`Credential` vs "credentials") the phrasing is
reworded to an equally natural synonym rather than the gate being weakened.

---

## 3. The negative twin — near-miss generation as a mechanical transform

Every positive emits a sibling. This is what makes the band the audit found structurally
inadmissible (§2.2) generatable *at the same rate as positives*, sharing vocabulary by
construction — because it is literally derived from the positive.

| transform | positive | near-miss sibling → gold `none` |
|---|---|---|
| **de-possess** | "how close am I with Priya?" | "how do people build closer friendships?" |
| **generalize** | "what did I spend on dining last month?" | "what's average household dining spend?" |
| **third-party** | "what's my capacity for new work?" | "what's Priya's capacity for new work?" |
| **mechanism** | "how focused have I been this month?" | "how is focus concentration measured?" |
| **advice** | "what am I working on right now?" | "how should I decide what to work on?" |
| **definition** | "what's my warmth score with Sam?" | "what is a relationship warmth score?" |
| **hypothetical** | "what did I browse about kayaks?" | "what would browsing history reveal about someone?" |

Gold for the sibling is defined by the transform, not discovered. Seven transforms × every
positive is more near-miss volume than the band can absorb — sample it down to the §5 ratio.

**The third-party transform is doing double duty.** "What's on Priya's calendar?" is not the
owner's records, and getting that wrong is a *privacy-relevant* failure, not just an accuracy one.
Over-weight it.

---

## 4. Multi-label from declared co-occurrence, never from guesswork

`sources/registry.py` already declares which scopes legitimately travel together:

```
["schedule:read", "availability:read"]          ["ai_conversations:read", "work_context:read"]
["contacts:resolve", "relationship_context:read"]   ["public_bio:read", "work_context:read"]
["health:read", "work_context:read"]
```

Generate composites **only** from declared pairs. The schema declares the truth, so gold stays
authored. Do not invent plausible-looking pairs — that reintroduces inference into gold.

Where a pair is declared *and* the utterance names only the shared askable, the gold is the set.
Those cases feed the taxonomy-seam question in `PLAN_SCOPE_CLASSIFIER.md` §6A.3 directly: if the
model cannot separate them, that is evidence about the taxonomy, not the model.

---

## 5. Anti-triviality controls — wired to the existing ratchet

The generator must not be able to rebuild v3's failure at larger n. Each control is a build gate.

1. **Lexical ceiling gate.** Run `python -m topos_eval.catalog.difficulty` on output; fail if the
   5-fold CV ceiling exceeds the declared target (0.650 today). The ratchet already exists; wire
   the generator into it rather than inventing a second check.
2. **Label-leak gate.** No utterance may contain its `scope_id`, its dimension label, a signal
   object name, or a raw field identifier. Fail the build, don't filter silently.
3. **Cross-scope vocabulary floor.** Each scope's realization lexicon must share ≥ N content tokens
   with at least one other scope's. If a scope's vocabulary is disjoint from every other, the task
   is a lookup and the gate fails. This is the direct antidote to v3's memorized-phrase problem.
4. **Held-out by axis, never at random.** Hold out **entire realization templates** and entire
   (lens × surface) combinations from training. A random split over a combinatorial generator
   leaks — the same template lands on both sides and the score measures memorization. Same lesson
   as P1, one level up.

   **This bit the measurement, not just the data (found at G3).** The difficulty ratchet used a
   plain `StratifiedKFold`, so it trained on one rendering of a phrasing and tested on its
   sibling. On `role-classify-7` that inflated the ceiling by **+0.153** — 0.665 random against
   0.511 grouped — enough to invert a ratchet verdict and make a *harder* catalog look easier.
   `difficulty_report` now groups by `provenance.template_id` automatically whenever cases carry
   one, and `AgentCase` carries provenance so it can. **Every DECLARED value for a generated
   catalog must be the grouped number.** Any future generator inherits this for free; any future
   *measurement* must keep it.
5. **Composition ratio, declared and enforced:** ~45% positive, ~35% near-miss, ~15% composite,
   ~5% ambiguous/abstain. Fail if drift exceeds a few points.
6. **Grounding cap.** Cases marked `grounding: weak` (the five missing dimension definitions,
   §1.1) may not exceed a declared share. Prevents the thin scopes from being padded into apparent
   coverage.

---

## 6. Provenance — every case carries the tuple that made it

```json
{
  "id": "SG0412",
  "gold_labels": ["relationship_context:read"],
  "turns": [{"user_text": "am I drifting from Priya lately?"}],
  "provenance": {
    "scope": "relationship_context:read",
    "dimension": "relationships",
    "askable": "warmth_band",
    "entity_type": "RelationshipEdge",
    "lens": "trend",
    "timeframe": "lately",
    "surface": "interrogative",
    "register_source": "massive:calendar_query",
    "template_id": "warmth_band.drift.3",
    "grounding": "typed",
    "polarity": "positive"
  }
}
```

This is what makes the curriculum loop in `PLAN_SCOPE_CLASSIFIER.md` §6.5g actually work. Per-axis
error analysis — "the head fails on elliptical surfaces", "trend lens is weak on `interests`" —
is a *generation instruction*, and it is the aggregate signal a node can report without sending
any text.

---

## 7. Milestones

| id | milestone | depends on | output |
|---|---|---|---|
| **G0** | Extract grounding: loaders over the five sources in §1; print askable inventory per scope | — | coverage report; confirms the §1.1 gap empirically |
| **G1** | Author the 5 missing dimension definitions (`interests`, `memory`, `wellbeing`, `resources`, `places`) | G0 | unblocks 4 of 9 scopes; **also a standalone engine win** |
| **G2** | ✅ **DONE 2026-08-13** — realization lexicons | G1 | `topos-eval/src/topos_eval/catalog/lexicons/` — 166 realizations, 9 scopes, 51 askables, 6 lenses; 10 gates in `tests/catalog/test_lexicons.py` |
| **G3** | ✅ **DONE 2026-08-13** — positive generator + provenance + ratchet wiring | G2 | `catalog/generate/schema_grounded.py`, `scripts/generate_role_classify_7_catalog.py` → `role_classify_7.json` (930 cases, 332 generated); 20 tests |
| **G4** | Negative-twin transforms (§3) | G3 | the near-miss band |
| **G5** | Composites from declared pairs (§4); ratio + leak + vocabulary gates (§5) | G4 | full generator |
| **G6** | Register transfer from MASSIVE/CLINC150 surface forms | G5, licence attribution in manifest | naturalness lift |

**G1 is the critical path and it is not classifier work.** It is schema work that the engine wants
anyway. Start there.

---

## 8. Open questions

1. **Who authors the realization lexicons?** They are the quality ceiling of the whole generator
   and they cannot be LLM-generated without reintroducing a model's register bias as gold. Budget
   real human time, or accept a measurable ceiling.
2. **Do the five missing dimension definitions get authored to the same `1.0.0` typed standard,
   or a lighter "generation-only" schema?** Same standard is more work but the engine gets it back.
   Recommend same standard.
3. **How much does register transfer (G6) actually buy?** Measure at G5 vs G6 before investing —
   if schema-grounded phrasing already clears the ceiling gate, G6 is optional polish.
4. **Should the ambiguous/abstain band be gold-as-set or gold-as-abstain?** §4 assumes set.
   Revisit once the §6A.4 routing-vs-authorization decision is made — they are the same question
   seen from two ends.
