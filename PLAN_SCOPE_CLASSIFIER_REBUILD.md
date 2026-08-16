# Plan — Scope Classifier: build it to the spec it was designed against

**Status:** PROPOSED
**Date:** 2026-08-15
**Supersedes:** Track Q of [`PLAN_SCOPE_CLASSIFIER_PROMOTION.md`](PLAN_SCOPE_CLASSIFIER_PROMOTION.md) §2
**Parent:** [`PLAN_SCOPE_CLASSIFIER.md`](PLAN_SCOPE_CLASSIFIER.md) · **Generator:** [`DESIGN_SCHEMA_GROUNDED_GENERATOR.md`](DESIGN_SCHEMA_GROUNDED_GENERATOR.md)

The promotion plan asked "how do we get this head past the gate." Q3 showed that was the
wrong question: the head is not a weak version of the intended model, it is a **different
model from the one specified**. It was built multi-label and trained single-label. This
plan closes the gap against the spec, not against a metric.

---

## 0. What "intended" means

From the parent plan and `scope_head`'s own contract. Stated flatly so each one can be
measured:

1. **Multi-label.** Predicts the *set* of scopes a question needs. `scope_head.py` argues
   this explicitly: *"am I free Friday according to my calendar is two scopes, and a
   softmax would force a choice."*
2. **Covers all 14 live scopes.** Not "averages well across them."
3. **Calibrated.** A confidence means the same thing on real language as on a template.
4. **Abstains rather than guesses.** At a permission boundary, silence beats a wrong scope.
5. **Local, and trained on no user data.** Non-negotiable, and already held.
6. **Fits the query pipeline's contract.**

## 1. The measured gap, per intent

| # | intent | measured (saved artifact, 2026-08-15) | verdict |
|---|---|---|---|
| 1 | multi-label | **0 of 8,309** train rows multi-label; recall 0.165 on multi-gold vs 0.356 single-gold | **never trained for it** |
| 2 | covers 14 scopes | **54.6%** of failures are *dead* — all 14 sigmoids < 0.30; `public_bio` 71% dead, `work_context` 69%, `activity` 63%, `messages` 63% | **~4 scopes unlearned** |
| 3 | calibrated | synthetic validation picks tau 0.70; at 0.70 the head answers **0%** of real questions (real median confidence 0.346) | **does not transfer** |
| 4 | abstains | 0.941 of negatives abstained | **MET** |
| 5 | local, no user data | corpus is synthetic + CC BY; loader refuses share-alike | **MET** |
| 6 | fits the pipeline | `QueryPipeline.execute(scope_id: str)` — **singular** | **contract undefined** |

Two of six are met. The multi-label failure is the one that makes the model a different
thing from the design, so it leads.

---

## B0. The contract — **DECIDED 2026-08-15 (Jonny): escalate on uncertainty, never on cardinality**

An earlier draft recommended escalating any multi-scope prediction to the LLM. Jonny
rejected that, correctly: **cardinality and uncertainty are orthogonal.** A confident
`{availability, schedule}` is the model doing exactly the job it was designed for;
escalating it means building a multi-label model and then refusing to believe it. The rule:

```
high `none`                        → confident abstain (no scope needed)
any label in [tau_low, tau_high)   → ESCALATE  (ambiguity)
nothing ≥ tau_high and `none` low  → ESCALATE  (ignorance — see B0a; impossible today)
labels ≥ tau_high                  → ACT on that set — 1 or N scopes
```

The LLM is the fallback for *uncertainty*, not for *sets*. Consequences:

* **The pipeline must accept a scope set.** `QueryPipeline.execute(scope_id: str)` is
  singular; under this contract a confident 2-scope prediction is *used*, so the seam is
  real work: either `execute()` takes `scope_ids` and unions envelopes, or the router
  fans out one call per scope and merges. Each per-scope call stays separately
  permission-gated either way — acting on a set never widens any single grant.
* **Measured on the saved artifact** (per-label ladder, sweep over 7 threshold pairs):
  acts on 45–91% of turns depending on the pair, exact-set 24–41%. The shippability number
  is none of those — it is the **disjoint-set rate**: acting on a set sharing *nothing*
  with gold, i.e. the permission-boundary error. 109 cases (10.2% of turns) at (0.18/0.30).
  B4's gate is on this number.
* The 11.67% multi-label rate in the benchmark is a **generator parameter**, not a measured
  property of real questions; only I2/B7 can supply the real rate. Under this contract
  that mostly prices retrieval fan-out, not correctness.

**Exit:** rule written into `scope_router.py` as the stated behaviour with all four
branches tested; the pipeline seam (set-accepting `execute()` vs router fan-out) decided
and implemented.

## B0a. An explicit `none` class — **new, and it leads: the rule above is unimplementable without it**

**77.6% of training rows (6,451 of 8,309) are negatives encoded as an all-zero target
vector. Ignorance is *also* an all-zero vector.** "Confidently nothing" and "no idea" are
representationally identical, so the ladder's ignorance branch cannot exist: today the
model *acts* (by abstaining) on questions it has never seen, and Q3 measured that as 54.6%
of all failures. A model that abstains by having no opinion is not abstaining by decision.

Add `none` as a 15th output:

* **Corpus:** negative rows get `labels: ["none"]` instead of `[]`. Zero new data — the
  6,451 rows already exist and already carry a `none_kind` tag (12 kinds: `world`,
  `third_party`, `assistant_task`, `device`, …). The structure is currently thrown away.
* **Training:** 15-dim target; `pos_weight` recomputed (`none` is the *majority* label, so
  its weight lands below 1 — that is correct, not a bug).
* **Head format:** `none` is not a registry scope, and the loader's label check refuses
  labels absent from the live registry. `none` must be declared as a reserved sentinel in
  the artifact format, exempt from the registry check but required present — an old head
  without it is a worse artifact, not a compatible one.
* **Router:** `none` never reaches the pipeline as a scope; it exists only to separate the
  abstain branch from the ignorance branch.
* **Expected side effect, worth measuring:** positives are 1,858 mostly-synthetic rows vs
  6,451 mostly-real negatives (3,705 MASSIVE + 1,500 CLINC). The model is currently trained
  to see *real language itself* as "not a scope" — the likely root of the 0.346 real-median
  confidence. Naming `none` gives real-language register somewhere to go that is not
  "suppress every scope."

**Exit:** four-branch ladder implemented and tested; ignorance (low-everything) measurably
separated from confident-none on the negative slice.

---

## 2. The build

> **Execution status 2026-08-15:** B0a BUILT+TESTED (`none` sentinel in `scope_head.py`
> exempt from the registry check with a collision refusal; train script emits 15-dim
> targets; four-branch ladder in `scope_classifier._classify_with_head` with a `reason`
> field — `ambiguity` / `ignorance` / `confident-none` — pre-B0a artifacts keep the
> legacy two-threshold path; 8 new tests, 323/323 query suite green). There is no
> separate `scope_router.py` — the ladder IS `classify()`; earlier references to that
> file were stale. B1 BUILT (`make_compounds` in the corpus builder; `cmp::a::b` dual-
> parent grouping; train script drops compounds with held-out parents). B2 first pass
> DONE via the schema-coupling gate itself: the dead zone was partly an ENGINE SCHEMA
> gap — added `WorkItem` (work), `BrowseTrail` (interests), `ProfileSurface` (profile)
> entity types + 13 field-true askables; messages needed no schema at all (its fields
> existed; the lexicon had never realized them). 11/11 lexicon gates, 56/56 definition
> tests. Corpus rebuilt: 13,464 rows, multi 13.2%, synthetic 0.361, per-scope min 283
> (was 41), 0 collisions. **B4 also fixed a fourth split leak found in passing: the
> train script selected its "held-out" threshold slice AFTER training on all rows** —
> thresholds were chosen on memorized data, which is likely the real root of §9G's
> "picks 0.70, answers 0%". Holdout now selected before training. Retraining as of this
> note; the B0 pipeline seam (set-accepting `execute()`) is deliberately held until the
> 1.3.16 release freeze lifts.

### B1. Compound positives in G3 *(depends: B0a)*

The generator already builds compound questions — the benchmark is full of them
(`"Busy afternoon on the 13th… Also, summarize my attention profile."`,
`"Two things: Am I available Friday evening? show my interest profile."`). The **training**
generator never emits them. That is a corpus gap, not a design flaw.

* Emit compound positives by composing two askables from *different* scopes, reusing the
  benchmark's existing conjunction surfaces (`Also,` / `Two things:` / `Separately,`).
* **Under the B0 contract the model must learn *confident* co-activation** — a compound
  question should put both scopes above `tau_high`, not hedge both into the escalation
  band. Compound rows are what make that possible; today co-activation is actively trained
  *against* (every non-gold label is a negative on every single-label row).
* Hold compound rows to the same G2 gates as every other row — leak, giveaway, cross-scope
  vocabulary floor.
* **Group by template in every split.** The leak has appeared three times; compound rows
  derive from *two* templates, so the grouping key must cover both or it re-opens.

**Exit:** ≥12% of train rows multi-label (matching the benchmark), multi-gold recall within
0.05 of single-gold recall.

> **B2's target sharpened 2026-08-16 by the real-language probe**
> (`topos-eval/scripts/scope_head_probe_real_language.py`, 53 hand-annotated
> phrasings). The split that matters is not per-scope: it is **artifact-concrete
> vs abstract**. On b4, swallowing is **26% on concrete artifacts and metrics**
> ("what's in my PR review queue", "what's my resting heart rate trend", "what's my
> bank balance") against **5% on band phrasings** ("how has my sleep been"). The
> lexicons realized bands and never realized artifacts — so author artifacts first,
> and measure with dead-rate on the concrete slice rather than macro-F1.
>
> Two findings the same run pinned: **`attention:read` is a seam, not a hole** (0/3,
> both failures confidently landing on `complexity` and `public_bio` — more data will
> not fix a boundary the model draws in the wrong place), and **the FE keyword router
> defaults to `ai_conversations:read`** when nothing matches, so shadow rows whose
> `true_scope` is `ai_conversations` may be a fallback rather than a routing decision.
> Any measurement over shadow gold must exclude or flag them.

### B2. Coverage sweep — kill the dead zone *(depends: none, run parallel to B1)*

The single largest failure mode: 54.6% of misses score ≈0.00 on **every** label. These are
unambiguous questions — `show my attention heatmap`, `what is my latest vo2 max reading?`,
`what's my usual commute path?` — that the training set simply never expressed.

This is a **coverage** problem, not a volume problem, and the distinction decides the work:
authoring 25 more phrasings of askables the model already handles moves nothing.

* Enumerate askables per scope from the dimension definitions, then diff against askables
  actually realized in the lexicons. The gap list is the work queue.
* Prioritise by dead-rate: `public_bio` (71%), `work_context` (69%), `activity` (63%),
  `messages` (63%).
* Re-measure dead-rate after each scope lands — it is a far better progress signal than
  macro-F1, which moves too slowly to steer by.

**Exit:** dead-prediction rate < 20% (from 54.6%), no scope above 30%.

### B3. Fix the benchmark before optimising against it — **DONE 2026-08-15: blast radius 0.84%, no re-version**

Hand audit of all 125 multi-label cases plus a structural sweep (36 single-label cases
whose text names a second scope's domain; compound-marker scan). Never used the head's
predictions.

* **The motivating example was my error, not the benchmark's.** `am I free after lunch
  Friday according to my calendar?` is CL232, gold `{availability, schedule}` — already
  correct. The Q3 analysis bucketed cases by `gold[0]` and misreported it. Retracted here
  and in the promotion plan.
* **What actually survives audit: 9 cases = 0.84%**, under the pre-registered 2% line →
  note and move on, no classify-9.
  * CL041 (`Busy afternoon on the 13th without revealing meeting names` = `availability`
    alone) is inconsistent with CL229/CL232, which label the same derived-from-calendar
    pattern as both scopes. 1 case, on the one genuinely mutual seam.
  * 8 cases in the `…since I started the launch brief` family: a timeframe anchor naming a
    work artifact. **Policy decided: timeframe anchors do not add scopes** — otherwise any
    anchored question becomes multi-scope and the label loses meaning. Recorded so the
    generator and future graders apply it consistently.
* The 5 single-label cases with compound surface markers are QA-stream `none` rows with
  rhetorical two-part questions — correctly `none`.

**Exit met:** audited by hand, blast radius stated (0.84%), anchor policy recorded.

### B4. Retrain honestly *(depends: B1, B2, B3)*

* Grouped splits by `template_id` — compound rows keyed on both parents.
* `pos_weight` on every fit; it has silently collapsed two model families already.
* Report macro-F1 **and** the two decompositions Q3 showed are load-bearing: dead-rate, and
  single-gold vs multi-gold recall. A single headline number hid both defects for a week.
* **Keep the weights.** The current plan's best number (0.446) is unreproducible because
  that run's artifact was discarded; only 0.412 exists on disk.

**Exit:** artifact saved, all three metrics reported together, training gate refuses to
write a head that regresses any of them.

> **B4 run 1 (2026-08-15), gate REFUSED — recorded so the lessons survive.** macro-F1
> 0.345 < 0.412, **disjoint 0.301**: the threshold sweep optimised macro-F1 under the
> abstention floor alone, picked 0.10, and spent the one gate it could not see. Run 1's
> apparent "dead rate 0.007" was a **threshold artifact, caught by a fixed-threshold
> check**: at the same 0.30 reference the new training made scope sigmoids MORE
> suppressed (dead 0.544 vs old 0.367). Three lessons: **(1) a selection rule blind to
> a gate will spend that gate** — the sweep now enforces every §5 clause on validation;
> **(2) select, then refit on everything** — the holdout carve-out cost the artifact
> 1,220 compounds and 1,040 positives it need not pay once the threshold is frozen;
> **(3) never compare a threshold-dependent metric across models at different
> thresholds.**
>
> **Ladder simulation on b3 (real `classify()` path, 2026-08-15).** Over 1,071 turns:
> model decides 78.2%, LLM hand-off 21.8%. Negatives are excellent (243/253 confident-
> none, 6 false fires). Acting on positives: 61% exact set, 21.8% disjoint (diffuse).
> **The dominant remaining defect is new and has a name: confident-none SWALLOWS 171
> positives (21%) — silently, with no LLM backstop.** Precision of the confident-none
> branch is only 59%. Swallowed scopes: resources 24, work_context 20, health 19,
> ai_conversations 17, relationship_context 17. Root cause hypothesis: the §6.4
> synthetic cap forces ≥60% public rows, ALL of which are none — with a trained none
> class the cap now structurally teaches "unfamiliar phrasing = none". The cap
> predates the none class; revisit it. Principled fix is Q2 register transfer for
> POSITIVES (real-language paraphrases) + corpus rebalance, then remeasure.
>
> **B4 run 2 (2026-08-15), gate still refuses promotion — the run itself is the B4
> exit.** Artifact `scope_head_b3` (KEPT), threshold 0.70 chosen by min-disjoint
> fallback (no candidate met disjoint ≤0.03 on validation). **macro-F1 0.490** — beats
> the 0.412 incumbent and the lost 0.446; best measured to date. exact-set 0.475,
> abstention 0.972, **multi_gap −0.014 (PASS — from +0.192; compounds delivered
> parity)**. Still failing: per-scope floor 12/14, dead 0.246 (vs 0.20 — but the
> four-branch ladder now ESCALATES these to the LLM instead of silently abstaining:
> the rate is not fixed, the failure mode is), **disjoint 0.203 vs 0.03 — the blocking
> number**. Next lever: a Q3-style confusion pass on b3 to find which pairs confidently
> confuse, then Q5 calibration; per-scope floor continues through B2 iterations.

### B5. Calibration *(depends: B4)*

Known cheap and known insufficient: perfect thresholding takes macro recall 0.302 → 0.453
against a 0.600 floor. Worth doing, cannot be the plan.

* Temperature scaling on a held-out slice, or label smoothing during the fine-tune.
* **Select the threshold on real language, never on the benchmark** — synthetic validation
  chose 0.70, which answers 0% of real questions.

**Exit:** threshold chosen on validation transfers to the benchmark within ±0.03 macro-F1.

### B6. Third-party competence *(carried from promotion Q4, unchanged)*

M1 measured the *prototype* leaking 1 of 6 third-party probes. The trained head has seen
937 negative twins and has **never been tested on this**. Until it is, third-party
protection is the LLM's job and the LLM cannot be demoted.

**Exit:** ≥0.90 hold rate on ≥50 third-party probe pairs. **Hard prerequisite for any
promotion**, not a nice-to-have.

### B7. Real-traffic gold *(carried from promotion I2, unchanged)*

Still the only source of gold-labelled real language, and now also the only way to size
B0's hand-off rate. Offline reconstruction was tried and recovered 0 of 113.

---

## 2A. Hybrid vs LLM-only — measured, full benchmark, per-case composition (2026-08-15)

Both LLMs run over all 1,071 cases with the bake-off's exact prompt/parser; hybrid = the
shipped four-branch ladder verbatim, with the LLM's actual per-case answer substituted on
escalated turns (escalated cases are selected-for-hard, so averages would flatter the
LLM). These full-run LLM numbers supersede the 204-case bake-off subset.

| arm | macro-F1 | exact | neg-abstain | silent-drop | wrong-scope/all | LLM share |
|---|---|---|---|---|---|---|
| LLM-only mistral:7b (4.4 GB) | 0.495 | 0.243 | 0.126 | 0.028 | **0.237** | 100% |
| **hybrid b4 + mistral** | **0.550** | **0.524** | **0.972** | 0.276 | **0.143** | 16.4% |
| hybrid b3 + mistral | 0.523 | 0.516 | 0.964 | 0.221 | 0.177 | 21.8% |
| LLM-only llama3.2 (2 GB) | 0.376 | 0.261 | 0.510 | 0.243 | 0.204 | 100% |
| **hybrid b4 + llama3.2** | **0.500** | **0.508** | **0.976** | 0.309 | 0.146 | 16.4% |

* **The hybrid beats LLM-only on every axis except silent drops.** Macro +0.055, exact
  2.2×, negatives-abstained 0.126 → 0.972, wrong-scope-reaching-pipeline nearly halved
  (0.237 → 0.143) — while sending the LLM only ~1 turn in 6.
* **The low-RAM result is the headline: a 2 GB machine with the head (0.500) outperforms
  a 4.4 GB machine without it (0.495).** The machine-class quality gap compresses from
  0.119 to 0.050, because the hardware-dependent part only touches ~16% of traffic.
* **The one regression is silent drops** (0.028 → 0.276 vs mistral-only), concentrated
  in the confident-none swallowing defect; note the low-RAM LLM-only baseline already
  silently drops 0.243, so on small machines the hybrid matches the failure users
  already have while improving everything else. Fix is Q2 register diversity; every
  swallowing improvement flows straight to this cell.
* Trade inside the hybrid: b4 is better everywhere except silent drops (0.276 vs
  b3's 0.221). Which head fronts the hybrid rides on how silent drops are weighed
  against wrong-scope — a product call, not a modeling one.

## 2B. The classifier is a pack role — DECIDED + BUILT 2026-08-15 (Jonny)

Open decision #1 ("which caller predicts scopes") is answered by the pack system: scope
routing is a **bound role** in the model pack, like every other per-step model choice.

* **New role `scope`, NOT a binding on `classify`** — that role's consumers are
  free-form extraction (facts, conversation context, emotion) an encoder head cannot
  serve; one binding must never mean two competences.
* **Policy: `scope` is a USER-CONTEXT role.** It sees the raw question before any
  permission gate — an open provider there leaks every question asked. The `scope-head`
  provider is on-device by construction, hence protected and local.
* **Optional, engine-defaulted.** Every stored pack predates the role; requiring it
  would reject them all on deploy (`REQUIRED_ROLES` vs `ROLES`). Absent → engine
  default = installed head + LLM escalation. Unlike a back-filled LLM role, that
  default is measured, versioned, and identical on every node.
* **Default in all four builtin packs** (`{"provider":"scope-head","model":"default"}`),
  identical at every tier — the identity is the point: a 2 GB machine gets the same
  router as the strongest machine (§2A). Seed migration extended for fresh installs +
  follow-up `20260815200000_builtin_scope_role.sql` for live environments, per the
  balanced-pack precedent. `SCOPE_HEAD_ON_LLM_ROLE` is a write-time validation error.
* Engine: `ROLES` mirror + `scope_classifier.resolve_scope_binding(conn)` (pack binding
  → else engine default; never raises). CP 208/208 pack tests, engine 46/46.
* **Still open:** react-app pack-settings UI row for the role; the decide-flip stays
  gated on I2 real-traffic numbers (the binding ships the identity, not the authority);
  node pack-cache sync `sync_model_packs_from_control_plane` still has no production
  caller, so nodes reach the engine default path today regardless — which is the same
  behaviour, by design.

## 2C. Packs are query-time only — DECIDED + BUILT 2026-08-15 (Jonny)

Found while exposing the scope role: the pack's `classify` role selected **ingest**
models. Tracing every subtype proved none is reachable from the query path — facts,
conversation-context, emotion, topic and goal extraction all run in enrichment when
data arrives, and all already had their own selectors under Settings → Models → Node
functions. One dropdown steering two pipelines meant neither was honestly controlled
(emotion never even consulted its binding: it runs `roberta-base-go_emotions` in its
own slot).

Jonny's ruling: **model packs answer "when a user's request needs a model, which one?"
and nothing else.** Executed across all three repos:

* ROLES = `primary / reasoning / tool / scope`. `classify` keys in stored packs and
  stale client writes are stripped, never rejected (a rejection would brick editing
  every existing pack). Migration removes the key from live rows.
* Ingest resolvers (`facts_llm`, `conversation_context_llm`) are device-override →
  settings default, **no pack rung** — and the J-B10 guard now asserts they make no
  pack-resolver call at all, so re-adding one is a decision, not a drive-by.
* **Enrichment spend is role-less** (`ENRICHMENT_SUBTYPES`): the old chain fell
  through to labelling `fact_llm_extract` as `primary` pack spend — a billing
  misattribution fixed in passing.
* Follow-on candidate stands (see conversation, 2026-08-15): `conversation_context`
  is the best second encoder target — binary, off the request path, currently a 9B
  LLM asked for one word — but nothing starts before Horos clears I2.

Tests: engine 551, CP 243+11, FE 159 — all green post-surgery.

## 3. Sequencing

```
B0 (contract)  ─── blocks ──►  B1 (compound)  ──┐
                                                 ├──►  B4 (retrain)  ──►  B5 (calibrate)
B3 (fix gold)  ──────────────►  B2 (coverage)  ─┘              │
                                                                ├──►  B6 (third-party)
                                                                ▼
                        B7 / I2 (real gold)  ──►  promotion track I3 → I4
```

**B3 first in wall-clock** — it is independent, and everything after it is measured against
its output. **B0 first in decision order** — it is a five-minute call that determines
whether B1 exists.

B2 is the long pole and the highest-yield: it addresses 54.6% of failures where every other
lever addresses a slice.

---

## 4. Not doing, with the number that ruled it

Carried forward so none of it is retried. Every line was measured.

| lever | why not |
|---|---|
| More renderings per template | 13.7 unique/template at `--renderings 20`; dedup already binding |
| Bigger head | MLP 0.305 vs linear 0.369 — *worse*; capacity is not the constraint |
| More epochs | training loss already 0.026 |
| Threshold chosen on synthetic data | picks 0.70; answers 0% of real questions |
| Mining a near-miss band | 102 usable rows from 119,700 questions; 6/14 scopes got zero |
| ELI5 / StackExchange | CC BY-SA; §6.5a licence class refused, unblocked only by counsel |
| Concept negatives (G4b) | macro-F1 flat 0.369 → 0.369; helps safety, not discrimination |
| Chasing the "schedule sink" | six hypotheses tested, all six failed; it is an argmax artifact |
| Calibration alone | argmax ceiling 0.453 vs 0.600 floor |
| A taxonomy fix for b3's disjoint 0.203 | confusion is DIFFUSE: top pair 7.6%, twelve pairs to reach half — no seam to split |
| A scope-score guard on confident-none | rescues 8 of 171 swallowed positives; the swallowed cases sit at none ≥0.7 with every scope <0.30 — it is in the weights, not the ladder |
| Fixing swallowing by corpus rebalance | cap 0.40→0.55 + none share cut (b4 run): macro-F1 rose to 0.512 and dead PASSES (0.171), but swallowing WORSENED 171→222 — the mechanism is template familiarity, not none-share; the fix is Q2 register diversity for positives |

---

## 5. Gates

The §7 conjunction stands, plus one clause the old gate could not have expressed because
nothing was multi-label:

* macro-F1 ≥ incumbent (0.412 on the saved artifact — **not** 0.446)
* per-scope recall ≥ 0.60 on **all 14**
* negatives abstained ≥ 0.85
* **NEW — multi-gold recall within 0.05 of single-gold recall.** Without this the model can
  clear every other clause while remaining single-label in everything but configuration,
  which is exactly today's failure.
* **NEW — dead-prediction rate < 20%**, measured as low-everything *including* `none`. A
  model that abstains by having no opinion is not the same as a model that abstains by
  deciding to, and only this clause tells them apart.
* **NEW — disjoint-set rate ≤ 3% of acted-on turns.** Acting on a scope set sharing nothing
  with gold is the permission-boundary error — the one error class a router at this seam
  must not make. Baseline today: 10.2% at (0.18/0.30). Over-broad (superset) and
  incomplete (subset) sets are quality bugs; disjoint sets are safety bugs, and the gate
  treats them differently on purpose.

---

## 6. Kill criteria

* **B1 + B2 land and per-scope recall stays below 0.60 for >6 scopes** → the taxonomy is
  not learnable from synthetic data at this granularity. Fall back to a coarser routing
  taxonomy mapping many-to-one onto permission scopes (§6A.4 option B).
* **Dead-rate will not go below 30% after B2** → the schema's askable space is larger than
  a template generator can cover; the answer is real-language transfer (B7/I2 first), not
  more authoring.
* **Real-traffic gold shows the LLM ≥0.15 higher macro-F1** → keep the head as an
  abstention pre-filter only, where it already beats everything (0.941 vs mistral's 0.078).

---

## 7. Open decisions

1. ~~B0 contract~~ **DECIDED**: escalate on uncertainty, never on cardinality; act on
   confident sets. Remaining sub-decision: set-accepting `execute()` vs router fan-out
   (B0 exit criterion).
2. **Which caller predicts scopes** — home chat, MCP `query_scope`, or a new entry point.
   Still open from the parent plan; blocks B7/I2.
3. **I2 mechanism** — runtime patch (no product code, invasive) or flagged product code
   (ships, testable). A values call.
4. **B2 authoring budget** — who writes the coverage sweep, and is it worth it against the
   §6A.4 taxonomy option?
5. **CC BY-SA counsel** — unblocks ELI5/StackExchange and revisits SGD.
6. **Where the lexicons live** — weights are going to `Dialogues/horos` on Hugging Face. If the
   corpus and lexicons stay closed, the model card's numbers are unverifiable by anyone
   outside. Decide deliberately before the first public push.
