# Plan — Scope Classifier: Quality, then Promotion

**Status:** PROPOSED
**Date:** 2026-08-15
**Parent:** [`PLAN_SCOPE_CLASSIFIER.md`](PLAN_SCOPE_CLASSIFIER.md) (§9A–§9G hold the measurements)
**Related:** [`DESIGN_SCHEMA_GROUNDED_GENERATOR.md`](DESIGN_SCHEMA_GROUNDED_GENERATOR.md)

Two tracks. **Q** raises the classifier above the §7 promotion gate. **I** puts it into the
request pipeline without betting the permission boundary on it. They interleave: no `I`
step past I2 may start before its `Q` prerequisite lands.

---

## 0. Where we actually are

| arm, on classify-8 | macro-F1 | negatives abstained | scopes ≥0.60 recall |
|---|---|---|---|
| prototype (installed) | 0.387 | 0.797 | 1/14 |
| linear head, frozen MiniLM | 0.369 | 0.859 | 3/14 |
| mistral:7b | 0.436 | **0.078** | 8/14 |
| DistilBERT fine-tune (best run, weights NOT kept) | 0.446 | 0.937 | 1/14 |
| **DistilBERT — the SAVED artifact** | **0.412** | **0.941** | **1/14** |

The trained head beats every alternative on quality *and* safety and still fails the gate,
because §7 is a conjunction and **per-scope recall is the one clause nothing has moved**.
Nothing is installed: `active_source()` reports `prototype`.

> **The 0.446 row is not reproducible.** That run's weights were never saved; the only
> artifact on disk measures **0.412** (Q3, 2026-08-15). Any model card published to
> `Dialogues/horos` on Hugging Face must carry 0.412 — the number the shipped weights actually
> produce — not 0.446. Treat 0.446 as a lost result, not a claim.

**Infrastructure is done and is not the blocker.** Artifact format and loader seam with
three refusals (non-public licence, share-alike, drifted taxonomy), `ModelSlot.SCOPE_HEAD`,
encoder predictor, training script whose gate refuses to write a bad head, corpus builder
with train/benchmark disjointness enforced, offline shadow reporting over real questions.
A head that clears the gate drops in with no caller changes.

---

## 1. Ruled out, with the number that ruled it

Recorded so none of this is retried. Every line is measured, not assumed.

| lever | result | verdict |
|---|---|---|
| More renderings per template | generator yields 13.7 unique/template at `--renderings 20`; dedup already binding | **exhausted** |
| Bigger classifier head | MLP 0.305 vs linear 0.369 — *worse* | **capacity is not the constraint** |
| More epochs | training loss already 0.026 | **saturated** |
| Threshold chosen offline | generated validation picks 0.70; at 0.70 the classifier answers **0%** of real questions | **cannot be selected on synthetic data** |
| Mining the near-miss band | 102 usable rows from 119,700 questions; 6/14 scopes got zero | **generate, don't mine** |
| ELI5 / StackExchange as sources | both CC BY-SA — the licence class §6.5a rejected | **blocked, needs counsel** |
| Concept negatives (G4b) | macro-F1 flat 0.369 → 0.369; abstention 0.859 → 0.891 | **helps safety, not discrimination** |
| Chasing the "schedule sink" | six hypotheses tested, all six failed; the sink is an argmax artifact (Q3) | **not a real phenomenon** |
| Threshold/calibration work alone | argmax ceiling is 0.453 against a 0.600 floor (Q3) | **necessary, not sufficient** |

**Three bugs that will recur if not watched.** The template leak appeared three times —
catalog splits, difficulty ratchet, training holdout — each presenting as a different
problem. Class imbalance collapsed two different model families before `pos_weight` /
`class_weight`. And **argmax over an all-dead score vector manufactures a phantom
confusion partner**: 54.6% of misrankings have every sigmoid below 0.30, so a raw
confusion matrix invented a 31% "schedule sink" that does not survive excluding them. Any
new split must be grouped by template; any new fit must be weighted; any confusion
analysis must separate *dead* from *confused* before reading structure into it.

---

## 2. Track Q — quality

> **SUPERSEDED 2026-08-15 by [`PLAN_SCOPE_CLASSIFIER_REBUILD.md`](PLAN_SCOPE_CLASSIFIER_REBUILD.md).**
> Q3 found the head was built multi-label and trained single-label (0 of 8,309 rows), so it
> is not a weak version of the specified model but a different one. Track Q asked how to get
> *this* head past the gate; the rebuild plan asks how to build the head that was specified.
> Q1→B2 (coverage, not volume), Q3a→B3, Q4→B6, Q5→B5, and B0/B1 are new. **Kept below for
> the reasoning and the measurements** — Q3's findings in particular are the evidence base
> for the rebuild — but sequence the work from the rebuild plan, not from here.

### Q1. Template breadth *(the binding constraint)*

76 askables → 228 realizations → ~114 train templates. `relationship_context:read` trains
on **41 rows**. Recall cannot clear 0.60 on that, and §1 says no sampling trick substitutes.

* Author to **≥25 realizations per scope** (currently 11–35, median 16), prioritising the
  13 scopes below the recall floor.
* Gate: **≥150 train positives per scope** after the 50/50 split, measured by
  `build_training_corpus.py`, which already prints per-scope minima.
* Keep every G2 gate green — leak, giveaway, cross-scope vocabulary floor.

**Exit:** per-scope positives ≥150 for 14/14, lexicon tests green.

### Q2. Register transfer from MASSIVE *(G6 — specified, never built)*

The gap that breaks threshold selection is that generated positives are *confident* and
real questions are not: median confidence on 112 real questions is **0.346**. MASSIVE is
CC BY-4.0, already downloaded, and is the register anchor §6.5a identified.

* Use MASSIVE's `calendar_query` / `email_query` surface forms as **style exemplars** for
  paraphrasing existing realizations — style transfer, not free LLM invention, which
  substitutes a model's register for a human's.
* Measure: median confidence on the 112 real questions should *rise*; the offline report
  is the instrument and already exists.

**Exit:** real-language median confidence ≥0.45, or the approach is reported as failed.

### Q3. Per-scope failure diagnosis — **DONE 2026-08-15**

Scripts: `q3_confusion.py`, `q3_sink.py` (verdict thresholds pre-registered before any
result was read). Measured on the **saved artifact**, which scores macro-F1 **0.412**, not
the 0.446 in §0 — that run's weights were never kept, so 0.446 is not reproducible from
anything on disk. Every number below is the artifact's.

**The ceiling is the finding.** Macro recall over 14 scopes:

| operating point | macro recall |
|---|---|
| @0.70 (the artifact's own tau) | 0.302 |
| @0.30 (shadow-selected) | 0.400 |
| **argmax — no threshold at all** | **0.453** |
| §7 floor | **0.600** |

Perfect thresholding buys +0.151 and still lands 0.147 short. **Q5 cannot clear the gate
alone**; this is a discrimination failure, not only a calibration one. Q5 is still the
largest single measured lever and is cheap, so it stays — it just is not sufficient.

**More than half of all failures are not confusion at all.** Of 434 misranked positives,
**237 (54.6%) are *dead*: every one of the 14 sigmoids is below 0.30.** The model has no
signal whatsoever and argmax returns noise. Examples — `show my attention heatmap`,
`summarize my recent browser activity`, `what is my latest vo2 max reading?`,
`what's my usual commute path?` — all score ≈0.00 on *every* label. These are
unambiguous questions a human labels instantly.

**There is no "schedule sink"; that was an artifact of argmax over dead scores.** Raw
argmax puts 31% of misranked mass on `schedule:read`. Excluding dead predictions the
distribution is flat — schedule 19.3%, `work_context` 18.3%, `complexity` 12.2%. Six
explanations for the apparent sink were tested and **all six failed**:

| hypothesis | killed by |
|---|---|
| class imbalance | `pos_weight` WAS applied (min 24, max 202); attraction does not track volume — `messages` 212 rows → 5.2%, `contacts` 106 rows → 15.3% |
| temporal register | 1.20× lift only (55.7% vs 46.4%), and it *inverts* for `places` and `activity` |
| label co-occurrence | corpus is 100% single-label; 0 of 8,309 rows are multi-label |
| score bias | schedule ranks #4/14 on mean score when absent (0.047 vs `work_context` 0.093) |
| lexical subsumption | r = +0.352, n = 13, not significant — and the `public_bio` control is *higher* at +0.501 |
| source composition | only 3 of 14 scopes have any real-language rows, but the most-real scope (`messages`, 71% MASSIVE) is not an attractor |

**Verdicts** — exit criterion met, every failing scope classified:

| verdict | n | scopes |
|---|---|---|
| PASSES | 1 | `contacts:resolve` (0.707) |
| under-confident | 2 | `schedule` (top1 0.93 vs recall 0.44), `public_bio` |
| seam-confused w/ schedule | 2 | `health`, `availability` |
| one-way → schedule | 2 | `places`, `work_context` |
| data-starved | 7 | `complexity`, `messages`, `activity`, `attention`, `ai_conversations`, `resources`, `relationship_context` |

**Only 1 of the 3 seams §6A.3 predicted is real.** `schedule ↔ availability` is genuinely
mutual (25% out, 51% back) → this pair, and only this pair, triggers the §6A.4
routing-vs-authorization decision. `attention → complexity` (28%/9%) and
`work_context ↔ public_bio` (9%/24%) are one-way and are **not** seams; two pairs the
design worked around do not exist in the model's behaviour.

**Scopes that are essentially unlearned** (share of their failures that are dead):
`public_bio` 71%, `work_context` 69%, `activity` 63%, `messages` 63%.

**~~Benchmark gold is under-labelled on multi-scope questions~~ — RETRACTED 2026-08-15.**
The cited case (`am I free after lunch Friday according to my calendar?`) is CL232, gold
`{availability, schedule}` — the benchmark had it right; this analysis bucketed by
`gold[0]` and misreported it. The full hand audit (rebuild plan §B3) found a real blast
radius of 0.84% (1 inconsistent case + 8 timeframe-anchor policy cases), under the 2%
line. The larger casualty of the same `gold[0]` bug: **79 of 125 multi-label cases had
top1 equal to a true co-label and were logged here as "confusion" between two correct
answers** — treat this section's confusion *counts* as upper bounds.

### Q3a. Fix the benchmark before optimising against it *(new, from Q3)*

Q3 found classify-8 gold under-labelling genuinely multi-scope questions — the model is
penalised for being right. Until this is fixed every downstream number is measured against
a partly wrong target, and Q1/Q2/Q5 would all be tuned to it.

* Re-audit multi-scope cases in classify-8; `am I free … according to my calendar` is
  availability **and** schedule, and `scope_head`'s docstring already says so.
* Quantify the blast radius first — if it is <2% of cases, note it and move on rather than
  spending the authoring budget here.
* Do **not** relabel using the head's own predictions. That is the AUDIT §2 circularity
  re-entering through the back door, same as I5 guards against.

**Exit:** multi-scope cases audited by hand, blast radius stated as a number.

### Q4. Third-party competence on the trained head *(privacy, not accuracy)*

M1 measured the **prototype** leaking 1 of 6 third-party probes ("what's on Priya's
calendar" → `schedule:read`). The trained head has seen 937 G4 twins and has never been
tested on this. Until it is, third-party protection is the LLM's job and the LLM cannot be
demoted to backup.

**Exit:** ≥0.90 hold rate on a third-party probe set of ≥50 pairs.

### Q5. Calibration

The head is over-confident on templates and under-confident on real language — the same
gap from two sides. Temperature scaling on a held-out slice, or label smoothing during the
fine-tune. Cheap, and it is the principled fix for the threshold-transfer failure.

**Exit:** reliability curve on real questions flatter than the current one; threshold
chosen on validation transfers to the benchmark within ±0.03 macro-F1.

---

## 3. Track I — into the request pipeline

The classifier enters in **three postures**, each strictly weaker than the next in what it
is trusted to decide. No posture is skipped.

### I1. Offline shadow — **DONE**

`topos-eval/scripts/shadow_offline_report.py`. Read-only over the node's own DB, no node
code, deletable. Gives the confidence distribution on real language. Re-run periodically;
it improves free as the node is used.

### I2. Live shadow — *observe, never decide*

Needs the hook the offline report deliberately avoids, so it is a real decision with a
real cost. Two mechanisms, pick one:

* **Runtime patch** — a `sitecustomize`/`.pth` shim in the node's `uv tool` venv. Zero
  product code; vanishes on redeploy (a feature). Invasive to a running install.
* **Product code behind a flag** — the hook removed on 2026-08-15, off unless
  `TOPOS_SCOPE_SHADOW=1`. Honest and testable; it ships.

**Why it is worth the cost:** the pipeline already knows the correct `scope_id`, so this is
the only source of **gold-labelled real traffic**. Offline reconstruction was tried and
failed — brute-forcing `intent_hash` against `query_sessions` recovered 0 of 113.

**Exit:** ≥500 gold-labelled real turns; per-scope recall measured on *real* language.

### I3. Advisory — *the LLM decides, the classifier is scored*

The classifier runs on every routed turn. Its verdict is logged and compared, and it
**changes nothing**. Distinct from shadow only in that it runs inside the request path, so
it also measures real p95 latency and RSS under load.

**Exit:** p95 latency delta <10 ms, no RSS regression on the smallest supported node, and
agreement with the LLM ≥0.80 on gold-labelled turns.

### I4. Primary with escalation — *the classifier decides above `tau_high`*

The M1 ladder as designed. Requires the full §7 gate **plus**:

* thresholds re-measured — the current pair (`TAU_HIGH=0.30`, `TAU_LOW=0.18`) was chosen
  for answer-rate on real language, and §9A measured negatives-abstained at **0.589** at
  0.30 against the 0.85 floor. **This pair is not gate-cleared.**
* a kill switch: `TOPOS_SCOPE_HEAD=` unset falls back to prototypes, already the loader's
  behaviour, verified by test.
* the §6A.4 decision made — accept confusion cells, or split routing from authorization.

**Exit:** §7 gate green on real-traffic gold, not only on classify-8.

### I5. Batch retraining

Champion/challenger only. A retrained head ships only if it beats the incumbent on a
**frozen, human-audited** slice that no model labelled. Otherwise the flywheel drifts
toward whatever the LLM said, which is the AUDIT §2 circularity re-entering through the
back door.

---

## 4. Sequencing

**Revised 2026-08-15 after Q3.** Q3 did redirect the order, though not in the direction it
was set up to test.

```
Q3 DONE ──► Q3a (fix gold)  ──►  Q1 (COVERAGE, not volume)  ──►  Q2 (register)
                 │                        │
                 └──► Q5 (calibration) ───┤        Q4 (third-party) ──┐
                                          ▼                           ▼
I1 DONE ──► I2 (gold traffic) ──► I3 (advisory) ──► I4 (primary) ──► I5 (retrain)
```

**Q1 is vindicated but its shape changes.** The pre-Q3 case for Q1 was volume (train
positives correlate with recall at pearson +0.43→+0.70, spearman +0.46→+0.60; n=14, so
directional not decisive). The real case is stronger and different: **54.6% of failures
are dead predictions**, and a dead prediction on an unambiguous question means the
training set never expressed that askable at all. That is a *coverage* gap, not a *volume*
gap. Authoring 25 more realizations of phrasings the model already handles will not move
it; the target is the askables that currently score ≈0.00 across all 14 labels —
concentrated in `public_bio` (71% dead), `work_context` (69%), `activity` (63%),
`messages` (63%).

**Q5 moves earlier and runs parallel.** It is cheap, it is the largest single measured
lever (+0.151), and it is now known to be *insufficient alone* — so it should not block
Q1, and Q1 should not wait for it.

**The taxonomy decision shrank.** §6A.4 is triggered for exactly one pair
(`schedule ↔ availability`), not the three §6A.3 anticipated. That is a much smaller
decision than the plan assumed.

**I2 still gates everything past I3.** Unchanged: without gold-labelled real traffic there
is no honest measurement of the thing being promoted, and §9G showed synthetic validation
actively misleads on the one parameter that matters.

**I2 gates everything past I3.** Without gold-labelled real traffic there is no honest
measurement of the thing being promoted, and §9G showed synthetic validation actively
misleads on the one parameter that matters.

---

## 5. Kill criteria

Stated up front so the project can end cleanly rather than drifting.

* **Q1 + Q2 land and per-scope recall stays below 0.60 for >6 scopes** → the taxonomy is
  not learnable from synthetic data at this granularity. Fall back to §6A.4 option (B):
  a coarser routing taxonomy mapping many-to-one onto permission scopes.
* **I3 shows p95 latency regression >25 ms or a headroom-ceiling breach** → the RSS is not
  worth it; stay on the prototype and keep the LLM.
* **Real-traffic gold shows the LLM at ≥0.15 higher macro-F1 than the head** → the local
  classifier is the wrong tool for routing; keep it as an abstention pre-filter only,
  where it already beats everything (0.937 vs mistral's 0.078).

---

## 6. Risks

| risk | mitigation |
|---|---|
| Template leak returns in a new split | Group by `template_id`. It has appeared 3× already; treat any ungrouped split as a bug until proven otherwise. |
| A fit collapses to the majority class | `pos_weight` / `class_weight` on every fit. It has broken 2 model families. |
| Thresholds tuned on the benchmark | Select on validation or real traffic only. Benchmark-tuned thresholds are test-set fitting. |
| 265 MB RSS breaks a small node | I3 measures it under load before I4 promotes. `bad-neighbor` exclusion already exists in §3.4. |
| Head promoted while third-party leaks | Q4 is a hard prerequisite for I4, not a nice-to-have. |
| Shadow logging grows unbounded | Node-local log needs rotation before I2 runs for weeks. |

---

## 7. Open decisions

1. **Which caller predicts scopes** — still open from the parent plan, and it blocks I2/I3.
   Home chat, MCP `query_scope`, or a new entry point.
2. **I2 mechanism** — runtime patch (no product code, invasive) or flagged product code
   (ships, testable). §3 states both honestly; this is a values call.
3. **Q1 authoring budget** — ~350 additional realizations at current density. Who writes
   them? Q3 narrowed this: the §6A.4 taxonomy split is now a *one-pair* decision
   (`schedule ↔ availability`), so it is no longer a real alternative to authoring — the
   two are not competing for the same budget. The open part is **which askables**, and Q3
   says target the dead zone (`public_bio`, `work_context`, `activity`, `messages`) rather
   than spreading evenly across the 13 below-floor scopes.
4. **CC BY-SA counsel** — unblocks ELI5 and StackExchange for Q2, and revisits SGD. Still
   unanswered from §6.5a.
5. **Where the lexicons live** — if the training corpus and lexicons stay closed while
   weights ship to `Dialogues/horos` on Hugging Face, decide that deliberately before the first
   public push, not by default. Weights are reproducible only with the corpus; publishing
   one without the other makes the model card's numbers unverifiable by anyone outside.
