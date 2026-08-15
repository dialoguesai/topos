# Audit — Role Competence Catalog v3

**Date:** 2026-08-13
**Scope:** `role_classify_3.json` (711 cases), `role_tool_3.json` (100), `role_primary_3.json` (62),
the factory `scripts/generate_role_competence_v3_catalogs.py`, and the oracle
`src/topos_eval/role_competence/classify.py`.
**Question asked:** is v3 good enough to rank LLM families against TOPOS node benchmarks?

**Verdict:** the engineering is clean, but the catalog cannot currently separate a competent
model from a lexical one. Most of the headline score is recoverable from surface tokens, and
the two lanes that claim to measure retrieval do not measure retrieval. Recommendations for v4
are in §6, ordered by how much measurement validity each one buys.

Note: v3 has never been run against a model. All 21 rows in
`eval_reports/role_competence_public/history.jsonl` reference v2 catalogs. Everything below is
static analysis plus reruns of the factory's own oracle.

---

## 1. What v3 got right

These are real improvements over v2 and should be preserved.

- **No prefix padding.** 0/711 classify, 0/100 tool, 0/62 primary start with `please `,
  `can you `, `quickly:`, `for my records,`, `from my data,`, `summarize:`. v2's
  7-prefixes × 70-stems inflation is gone.
- **Unique texts.** 711/711 classify texts unique, 100/100 tool. Only one duplicate in primary
  (`what are the latest commits on grow_web?`).
- **Frozen evidence discipline.** `HIT_PACKETS` paraphrase the *question* and never the packet
  text. This is the right invariant and it is correctly implemented.
- **Broader hold coverage in the tool lane.** 23 `denied` / 40 `insufficient_evidence` /
  37 `completed`, 63 abstain cases. v2 had 48 cases total.
- **No personal markers.** `test_catalogs_have_no_personal_markers` passes on all nine JSONs.
- **Exclusion order.** Gates before accuracy (`score_competence_lane`) is sound design — a model
  cannot greenwash a hold-gate failure with a high accuracy number.

---

## 2. Root cause — gold is defined by a substring matcher fitted to the data

`build_classify_3()` admits a case only when the rule-based `fake_classify_label` already agrees
with the intended label:

```python
pred = sorted(_pred_set(user))
gold = sorted(labels)
if pred != gold:
    misses.append(...)   # -> raise SystemExit
```

So the catalog is, by construction, exactly as hard as `_FAKE_RULES` and no harder. Anything
requiring semantics is deleted at generation time.

`_FAKE_RULES` is not a general classifier. It is a lookup table of the answers:

| measurement | value |
|---|---|
| total rule keys | 231 |
| keys ≥ 3 words (memorized phrases) | 66 |
| **keys matching exactly one case in the whole catalog** | **170 / 231 = 74%** |

Examples of keys that exist only to make one seed pass: `squeeze in a haircut`,
`rolodex listing`, `headshot caption`, `cortisol reading`, `retro on my calendar`,
`github repos did i click`.

### 2.1 The oracle has no concept model

Rewording a catalog question in the way a real user would collapses the oracle to `none`:

| paraphrase | gold | oracle says |
|---|---|---|
| how many steps did I walk yesterday? | `health:read` | `none` |
| what's booked for me on Thursday? | `schedule:read` | `none` |
| what websites was I on last night? | `activity:read` | `none` |
| how much did I spend at the grocery store? | `resources:read` | `none` |
| where was I on Saturday afternoon? | `places:read` | `none` |
| what's Priya's phone? | `contacts:resolve` | `none` |
| what team am I on? | `work_context:read` | `none` |
| how overloaded am I right now? | `complexity:read` | `places:read` |

**14 of 16 natural paraphrases (88%) are missed.** Since this function defines gold, the catalog
contains no paraphrase the matcher can't already spot.

### 2.2 Near-miss negatives are structurally inadmissible

The decision boundary this lane exists to measure is *"is this a request for the owner's records,
or a general question about the same topic?"* That band cannot be represented, because the oracle
labels every one of these with a scope, so `add()` refuses them and the factory exits:

| would-be case (human gold: `none`) | oracle assigns |
|---|---|
| How does sleep affect memory consolidation? | `health:read` |
| What is a good resting heart rate for adults? | `health:read` |
| How do shared calendar invitations work in Outlook? | `schedule:read` |
| Why do people browse social media before bed? | `activity:read` |
| What is the average salary for a staff engineer? | `resources:read,public_bio:read` |
| How does ChatGPT handle long context windows? | `ai_conversations:read` |
| What is complexity theory in computer science? | `complexity:read` |

The existing 20 `HARD_NEGATIVES` look topically adjacent but were hand-written to dodge the
matcher — "How do fitness trackers estimate oxygen uptake in lab studies?" avoids `vo2 max`,
"What is contact tracing in epidemiology?" avoids `contact record`. They are lexically trivial.

`PLAN_ROLE_COMPETENCE_EVAL.md` §3.3 schedules "hard negatives (near-miss scopes)" for v8. The
current factory makes them impossible to add at any version.

---

## 3. The headline metric is carried by two trivial segments

`label_accuracy` is one number over 711 cases. Its composition:

| segment | n | share |
|---|---|---|
| `none` | 255 | 35.9% |
| scope | 220 | 30.9% |
| emo_27 | 216 | 30.4% |
| multi-label | 20 | 2.8% |

66% of the score comes from `none` + emo, and both are lexically transparent.

### 3.1 Measured triviality

TF-IDF + logistic regression, 5-fold stratified CV, no semantics, no LLM:

| segment | lexical CV accuracy |
|---|---|
| `none` | **0.996** |
| emo_27 | **0.944** |
| scope | 0.555 |
| **overall** | **0.839** |

`LABEL_ACCURACY_FLOOR = 0.80`. **The admit floor sits below the bag-of-words ceiling.** The floor
cannot discriminate: the catalog's information content is mostly surface lexical.

A five-line zero-shot rule — *if the text contains an emo frame, return the emotion word found in
it; otherwise return `none`* — scores **0.644**. "Always `none`" scores **0.359**.

### 3.2 The emo lane is a word-lookup task, not emotion recognition

- **89.8% of the 216 emo texts contain their gold label as a literal substring**
  (`"this note is disappointed the concert was cancelled"` → `disappointment`). The remaining
  10% use a one-to-one synonym: `livid`→anger, `terrified`→fear, `longs for`→desire,
  `hilarious cracked up`→amusement.
- All 216 carry one of five frames (`this note `, `this message `, `the writer `,
  `emotion in this text`, `label the emotion`), and **0 of the 495 non-emo cases contain any
  frame**. The frame is a 100%-precision giveaway for "this is the emotion taxonomy."
- GoEmotions is multi-label natural Reddit text with a `neutral` class. This is single-label
  synthetic template text with no `neutral`, so every emotional-looking utterance is guaranteed
  to have a non-neutral answer — itself a giveaway.

### 3.3 The `none` class is register-separable and exhausted

225 of 255 `none` cases are AskReddit poll titles ("What's your unpopular opinions?",
"Celebrities with the biggest ego?"). They differ from scope questions in *register*, not in the
property being tested. A model can separate them without understanding the scope taxonomy.

The pool is also spent: `load_reddit_none(limit=250)` took 225 of the 229 accepted
`classify_none` rows that exist in `reddit_expand.jsonl`. There is no headroom here for v4.
Meanwhile 6,531 `tool_deny`, 1,070 `classify_label`, 227 `tool_empty`, 186 `primary_hit` and
135 `tool_hit` harvested rows sit unused.

### 3.4 Multi-label is 2.8% of the catalog and scored without partial credit

20 multi-label cases, zero with three or more labels, scored by exact set match. Composite
routing — the thing that actually distinguishes a careful model — is barely measured, while a
model that emits one extra plausible label is penalized in full.

---

## 4. The retrieval lanes do not measure retrieval

### 4.1 There is nothing to retrieve

21 of 25 primary scenarios return **exactly one document**, and that document contains the whole
answer:

```
rp_v3_sara_marcus: 1 doc -> 'Sara asked Jordan about Marcus on Tuesday in the afternoon thread'
rp_v3_northwind:   1 doc -> 'Jordan works at Northwind Labs as a staff engineer on personal AI systems'
```

No distractors, no ranking, no multi-hop, no conflicting or superseded evidence. The task is
"read the single document you were handed."

### 4.2 `evidence_backed` never inspects the model's answer

`evidence_backed_rate` scores needles against `_full_trace_blob(trace)`, which reads
`trace.evidence.summary_items` — and `loop.py:377` sets that to `shown_items`, *what the tools
returned and fed the prompt*. The model's `final.text` is never checked.

Because each packet is one document containing every needle, **any completed turn that calls
`query_scope` scores 1.0**. `evidence_backed` is the primary lane's headline accuracy metric
(floor 0.70) and it is measuring "did you call the tool and finish", not "did you answer
correctly."

### 4.3 Latent brittleness once 4.2 is fixed

- **107 of 107 needle groups have exactly one alternative.** Zero paraphrase tolerance. The
  moment needles are checked against answer text, a correct answer saying "Tue" instead of
  "tuesday" fails.
- **Question/needle mismatch.** RP asks *"What did Sara ask Jordan about Marcus?"* but the
  needles require `sara`, `marcus` **and** `tuesday`. The question doesn't ask for the date.
  Same shape in `v3_journal_investor` (needles demand `5 of 7`) and `v3_cal_density`.

### 4.4 Tool-lane hit cases pass on one echoed word

`expect = [re.escape(packet["needles"][0][0])]` — only the first alternative of the first group:

```
RT87: expect=['sara']       q='What did Sara ask Jordan about Marcus?'
RT97: expect=['docker']     q='What did I discuss about Docker and nginx deployment?'
RT100: expect=['sam']       q='Who are my collaborators on coding work?'
```

### 4.5 Tool arguments are untested by construction

`FakeMcpServer.call(self, name, arguments)` ignores `arguments` entirely — responses are
consumed in call order regardless of what the model asks for. Scope arguments, query
formulation, date ranges and pagination are unmeasured. The report is honest about this
(`"arg_correctness": None`), but it is the largest untested surface in the tool lane.

---

## 5. Smaller findings

- **29 identical `user_text` strings appear in both `role_primary_3` and `role_tool_3`** (all
  `EMPTY_QQ` items plus the `HIT_PACKETS` first questions). The lanes are correlated and coverage
  is overstated by roughly that many cases.
- **v3 tool/primary are mostly v2.** 48 of 100 tool cases and 24 of 62 primary cases are
  inherited verbatim via `json.loads(role_tool_2.json)`. The "richer" claim applies mainly to
  classify.
- **Unstratified sampling.** `run_classify_lane` does `rng.shuffle(cases)` then `cases[:limit]`.
  With 42 label classes and 8 cases per emotion, a `--limit 100` run yields ~1 case per emo class.
  The headline number is noisy and not comparable across seeds.
- **Zero-tolerance gates flip verdicts on one case.** `GateResult.clean` is `not violations`, so a
  single hold or scope violation out of 63 abstain cases moves a model from `admit` to `exclude`.
  On a lane meant to *rank families*, that is a coin flip rather than a gate.
- **Test is weaker than the factory.** The factory asserts zero prefix-padded cases;
  `test_v3_classify_is_unique_not_prefix_padded` only asserts `< 5%`. Match them.
- **`abstained = message == "none"`** in the classify trace (classify.py:675). In this lane `none`
  is a *correct answer*, not an abstention. Harmless today because classify sets `gates_ok = True`,
  but it will misreport if the hold gate is ever enabled for classify.

---

## 6. Recommendations for v4

Ordered by measurement validity gained per unit of work.

### P1 — Break the oracle/gold circularity *(unblocks everything else)* — ✅ LANDED 2026-08-13

Gold must be known a priori, not discovered by regex.

1. ✅ Gold now comes from the authoring structure and is never re-derived from the matcher.
   `scripts/generate_role_competence_v4_catalogs.py` writes `role_classify_4.json` (607 cases);
   `fake_classify_label` is a CI smoke brain that is allowed to be wrong, checked against a band.
   Oracle agreement fell from **1.000 (by construction) to 0.928**.
2. ✅ `test_v4_gold_is_authored_not_derived_from_the_oracle` asserts the band (0.30 < agreement
   < 0.98). `test_fake_classify_matches_v3_gold` is kept for v3 but re-documented as recording
   the defect, not a quality signal.
3. ✅ `topos_eval/catalog/difficulty.py` measures `majority_baseline`, `lexical_ceiling`
   (TF-IDF + LogReg, 5-fold CV) and `label_leak_rate`; `tests/catalog/test_catalog_difficulty.py`
   ratchets every shipped version against `DECLARED`. Run
   `python -m topos_eval.catalog.difficulty`.

Also landed, because P1 made them possible or safe:

4. ✅ **The near-miss band exists.** 41 world-knowledge questions phrased in each scope's own
   vocabulary, gold `none` — the exact cases the v3 factory refused. Oracle agreement on the band
   is **0.000**, and `test_v4_near_miss_band_is_what_v3_could_not_represent` fails if someone
   re-fits `_FAKE_RULES` to them.
5. ✅ **AskReddit selection inverted.** v3 kept a title only if the matcher already said `none`,
   discarding the interesting ones. v4 takes matcher-misread titles *first* — the matcher is now
   a difficulty heuristic, never a gold source.
6. ✅ **`none` rebalanced 35.9% → 24.9%** (P2 item 6 pulled forward; a sampling decision, not new
   content). Majority baseline 0.396 → 0.249.

**What P1 did not fix, and could not.** The lexical ceiling moved only 0.839 → 0.818. It is
dominated by the emo segment, which measures **0.986 on its own** and leaks its gold label into
the prompt 89.8% of the time. That is P3. The 0.65 target is recorded as
`target_lexical_ceiling` and the gap prints on every run. Until it closes, v4 is opt-in and
`LABEL_ACCURACY_FLOOR = 0.80` still sits below the trivial ceiling — a fake keyword brain scores
0.933 on v4 and is still returned as `verdict=admit`.

### P2 — Add the near-miss negative band — ✅ LANDED 2026-08-13

Shipped as `role-classify-6` (598 cases), which supersedes v5 for ranking.

4. ✅ Near-miss taken from 41 to **125**, 8–12 per scope.
5. ✅ Two new bands. **third-party** (28) points the scope's own vocabulary at somebody else's
   records — "how many steps did Priya walk this week?" — testing *whose data*, which nothing
   else in the catalog covered. **advice** (14) is generation needing no lookup.
6. ✅ Multi-label **20.9%** with **35 three-label cases** (v3–v5 had none), composed from the
   authored scope seeds so gold is the union by construction. Partial credit landed with
   macro-F1 in P3.

**Item 6's share targets turned out to be obsolete, and I replaced them.** They were written
against exact-set accuracy, where a large majority class is a free score. Once P3 moved the
lanes to macro-F1 that stopped being true — v6 is 42% `none` and the always-`none` policy
scores **0.040 macro-F1**, because it earns a decent F1 on one of fifteen labels and zero on
the rest. The metric-consistent guards are now `majority_policy_macro_f1` ≤ 0.10 and a
**minimum of 25 positive cases per label** (v6's thinnest is 30), which is what macro-F1
stability actually needs. A composite counts as a positive for both its scopes, so multi-label
growth helps here rather than competing.

**The important finding is a weakness in my own P2 recommendation.** Per-band accuracy under a
*trained* bag-of-words model:

| band | lexical accuracy | reading |
|---|---|---|
| advice | 1.000 | separable by form |
| `none` (AskReddit etc.) | 0.986 | separable by form |
| **near-miss** | **0.984** | **separable by form** |
| norm (first-person) | 0.938 | discriminating |
| third-party | 0.714 | discriminating |
| scope | 0.355 | hard |
| multi-label | 0.000 | hard |

The near-miss band defeats the *keyword oracle* (0.096 agreement) but a model trained on the
catalog picks it out at 0.984 — "How does X work?" separates from "did my X drop this month?"
by **question register**, with no understanding of whose data is being asked for. That is the
same class of weakness v3's hand-written `HARD_NEGATIVES` had, one level up. Defeating the
oracle is not the same as being hard.

Two responses, both shipped. A **norm band** (16 cases) puts the negatives in first-person
register — "how many steps should I be walking a day?" — so form no longer gives the answer
away; it measures 0.938. And per-band accuracy is now part of `difficulty_report`, printed with
a `<-- not discriminating` flag, with `test_some_negative_band_resists_a_trained_lexical_model`
failing the build if every negative band becomes form-separable. The weakness is now measured
rather than discovered by hand.

The norm band's gold is the most arguable in the catalog and is deliberately its own band so it
can be revisited alone. A personal assistant might reasonably *want* the owner's step count to
personalise "how many steps should I walk?"; the claim is only that it does not *need* it,
which is the minimum-necessary reading the prompt already states. For the same reason
"how should I plan my week?" is **not** in the advice band — a calendar read would genuinely
improve that answer, so `none` would be contestable gold.

**Composites buy volume, not difficulty.** Measured at 31% of the catalog they pushed the
lexical macro-F1 ceiling *up* from 0.596 to 0.666, because concatenating two topically distinct
questions is easy for bag-of-words — both keywords are present. They were trimmed back to the
15% target (landing at 20.9%). They still test something real: that a model emits the right
*number* of labels. Harder composites need clauses that imply a scope without naming it, which
is the plan's v5 paraphrase work.

**Net difficulty:** lexical macro-F1 ceiling 0.621 (v5) → **0.623** (v6), against a 0.70 floor.
Essentially flat, while the negative band tripled and multi-label went from 5.9% to 20.9%. The
honest reading is that P2 bought *coverage* — whose-data, norm and 3-label failure modes that
were previously untested — rather than raw difficulty. Closing the remaining gap to the 0.50
target is paraphrase work, not more bands.

### P3 — Make the emo lane an emotion task — ✅ LANDED 2026-08-13

7. ✅ All five frames gone — 0 of 735 emo cases carry one.
8. ✅ Label leak fell **0.898 → 0.095**, and what remains is genuine self-report
   ("I'm so sorry" → `remorse`) rather than a template naming its own answer. Ratcheted in CI.
9. ✅ Real GoEmotions text (Demszky et al. 2020, Google Research, CC BY 4.0), native
   multi-label gold (28.6% of cases carry 2+ labels), `neutral` restored as the 28th label.
10. ✅ `label_macro_f1` is now the headline for both label lanes; `label_accuracy` stays as a
    drill-down. Macro so the 28-way tail counts as much as the majority class, and partial
    credit by construction so multi-label cases no longer score all-or-nothing.

**The split was forced by item 7.** The frames were doing real work: they told the model which
of the two taxonomies applied. Remove them from a mixed scope+emotion closed set and a bare
comment like *"Kings fan here, good luck to you guys!"* is genuinely ambiguous between
`excitement` and `none` ("not the owner's personal data") — both defensible, so the mixed set
cannot score it fairly. P3 therefore ships two catalogs:

| catalog | what it is | lexical ceiling (set-acc / macro-F1) | label leak |
|---|---|---|---|
| `role-classify-3` | audited baseline | 0.839 / 0.838 | 0.572 |
| `role-classify-4` | P1, gold authored | 0.818 / 0.853 | 0.572 |
| **`role-classify-5`** | scopes + `none` + near-miss, emo removed | **0.679 / 0.621** | **0.279** |
| **`role-emo-1`** | real GoEmotions, 735 cases | **0.290 / 0.287** | **0.095** |

`role-emo-1` draws from the **test and dev splits only** — `train` is deliberately reserved,
because the node already ships `SamLowe/roberta-base-go_emotions` and may fine-tune a local
classifier; a benchmark drawn from its training split would measure memorisation. Each case
records its upstream `comment_id`. Slurs, sexual and self-harm content are filtered; mild
profanity is kept, because filtering it biases anger and annoyance toward implausibly polite
anger.

**The admit floor is now derived rather than guessed.** `LABEL_MACRO_F1_FLOOR = 0.70` sits
above the measured lexical macro-F1 of every catalog cleared for ranking (0.621 and 0.287), and
`test_admit_floor_sits_above_the_lexical_ceiling` enforces that against live measurements — so
lowering the floor, or shipping an easier rank-ready catalog, fails the build. This closes the
§3.1 defect where a 0.80 floor sat below a 0.839 ceiling. Catalogs carry an explicit
`rank_ready` flag; v2/v3/v4 are `False` and exempt.

**Verified end to end.** The keyword smoke brain scores **0.210 macro-F1 on `role-emo-1` and is
excluded** (`marginal-accuracy`) — the healthy outcome, since an emotion benchmark a lexicon can
solve is not measuring emotion recognition. It still scores 0.904 on `role-classify-5`, but that
number is *contaminated, not a baseline*: `_FAKE_RULES` was hand-written against those exact
scope strings. The honest trivial baseline is the CV ceiling (0.621), which must generalise
across folds. Driving the fake brain's classify score down is P2's job.

**Still open from P3's neighbourhood:** `LABEL_MACRO_F1_FLOOR` is calibrated against the floor
of what is *meaningless*, not against observed model performance. P6 tightens it once real
per-family runs exist.

### P4 — Put retrieval back in the retrieval lane — ✅ LANDED 2026-08-13

Shipped as `role-primary-4` (52 cases, 28 scenarios; 40 retrieval + 12 abstain) via
`scripts/generate_role_primary_4_catalog.py`. Gold documents are copied **verbatim from the
shipped v2/v3 JSON at build time** — the factory cannot retype prior evidence, only paraphrase
questions and add distractors around it.

11. ✅ Every hit scenario carries 4–8 documents: gold plus same-entity-wrong-date,
    same-date-wrong-entity, superseded-with-recency-cue, and topical distractors. Two new
    families beyond the plan: **contradiction** (two documents disagree with *no* recency cue;
    both values are expect groups, so silently picking one fails — a deterministic proxy for
    "flag the conflict") and **near-empty** (the packet holds only Priya's documents; the
    correct move is abstaining, and the neighbour's values are disclosure `forbid_patterns`).
12. ✅ **The metric is split.** `answer_correctness` (new, in `metrics/agent.py`) grades
    `final.text`: every `expect_patterns` group present, no `avoid_patterns` distractor value
    cited; non-completed turns are misses. `evidence_backed` stays as the
    packet-reached-prompt diagnostic. Both are reported on every lane; primary's headline is
    `answer_correctness` when the catalog carries oracles, falling back to `evidence_backed`
    on v2/v3. Floor `ANSWER_CORRECTNESS_FLOOR = 0.70`, provisional like the rest until P6.
13. ✅ Alternatives via regex alternation — `(?i)(august 20|aug 20)`, `(?i)(twelve thousand|12,?000|\$12k)`.
14. ✅ Question/needle alignment: "What did Sara ask about Marcus?" no longer demands
    `tuesday`; the journal case's rate stays only where "consistent?" actually asks for it.
15. ✅/◻ Landed with a design correction: distractor values are a **new `avoid_patterns`
    field**, not `forbid_patterns`. Forbid feeds the scope-discipline *hard gate*; citing a
    superseded standup time is a correctness failure that should cost accuracy, not a
    disclosure violation that excludes the lane. `forbid_patterns` stays reserved for real
    leaks (the busy-afternoon packet's meeting title, near-empty's neighbour data). The
    tool lane's single-token `expect` cases (RT87–100) are deferred to P5's tool-4 bump.

**Anti-triviality is structural, and enforced twice.** The factory guards (G1–G7) and
`tests/catalog/test_role_primary_4.py` both assert: ≥4 docs per hit scenario, gold satisfies
every expect group, **no distractor document passes alone**, every avoid marker matches a
distractor and never gold, non-contradiction cases always carry avoids (so whole-packet
parroting fails by construction), and zero texts shared with `role_tool_3` (P6.20 for this
catalog). Three of these guards caught defects in my own first draft — a wrong-date distractor
that passed the asker question, a changelog doc that passed the reading-habits question, and
16 question texts still colliding with the tool lane.

**Verified end to end.** The fake parrot brain (`"Grounded answer: " + results[:600]`) on the
same trace set: `evidence_backed 0.975`, `answer_correctness 0.05` — §4.2's conflation is now
two separable numbers — and it is **excluded** via the hold gate for answering the near-empty
world from Priya's documents. On v3 this same brain admitted with evidence_backed ≈ 1.0.
`role-primary-4` is declared `rank_ready` (structural note in `difficulty.DECLARED`: the
lexical-ceiling CV grades label cases and does not apply; the structural tests are the
ratchet). The atlas can now rank primary lanes; **pack posture still needs P5**, since tool
remains gates-only while FakeMcp ignores arguments.

### P5 — Test tool arguments — ✅ LANDED 2026-08-13

Shipped as `role-tool-4` (100 cases: v3's deny/auth/validation/empty behavior bands verbatim +
14 hit cases rebuilt) via `scripts/generate_role_tool_4_catalog.py`.

16. ✅ `ToolScriptEntry` gained `match_args` + `mismatch`; `FakeMcpServer.call` returns empty
    (default) or an error category when the call's arguments miss the oracle. Because the last
    script entry repeats, a self-corrected retry still reaches the payload — the first wrong
    ask is what the new `arg_correctness` metric records. It reads `args_structural` only
    (match keys are parse-restricted to the structural vocabulary, with a consistency test
    against `loop.STRUCTURAL_KEYS`), so it grades identically on trimmed public traces.
    `arg_correctness` stops being null and gains a floor (`ARG_CORRECTNESS_FLOOR = 0.70`,
    provisional like the rest; exclusion reason `marginal-arg-correctness`).

**One deliberate deviation from the item as written: aim, not vocabulary.** The frozen
baseline prompt never enumerates scope ids, so demanding `scope_id == "health:read"` would
grade models on knowledge they were never given. `args_match` uses substring alternatives —
`{"scope_id": ["health", "journal", "mood"]}` accepts whatever reasonable scope string a model
invents for a health question and rejects a hardcoded wrong one. A factory guard plus
`test_a_hardcoded_scope_cannot_clear_the_arg_floor` pin the mix: a brain that always sends the
same scope must mismatch on most oracles (measured 3/14), so it cannot clear the floor by luck.

**Also landed here — audit item 15's tool-lane half, deferred from P4.** RT87–100's
one-echoed-token hits are gone: v4 hit cases are retrieval-tier with the packet's needle
groups, full expect groups with alternation, one distractor document each, and
`avoid_patterns` from it — the tool lane's hits now grade under the same discipline as
primary-4. Two **least-privilege probes** additionally match `access_mode ["summary"]` (the
profile's own example arg), so requesting full access where summary suffices is a measured
over-ask. Gold packets are copied verbatim from `role_tool_3.json`; the v3 hit questions keep
their original texts, and since primary-4 already paraphrased its copies, **the two
recommended catalogs share zero user texts** (P6.20 closed in both directions).

**Verified end to end.** The fake brain hardcodes `scope_id=work_context:read` on every call.
On v3 that was invisible. On v4: `tool_choice_accuracy 0.973` (right tool) vs
`arg_correctness 0.214` (right aim on 3/14) → **excluded as `marginal-arg-correctness`** —
and `evidence_backed` fell to 0.214 because a mismatched ask genuinely never receives the
packet. "Right tool, wrong ask" is now two different numbers. `role-tool-4` is declared
`rank_ready` (structural note in `DECLARED`), which completes the pack-posture prerequisites:
the atlas prints "Suggested pack posture" once real models produce rankable admits on tool
**and** primary — pinned by `test_atlas_publishes_pack_posture_once_tool_and_primary_are_rankable`.

### P6 — Sampling and reporting hygiene — items 17–19 LANDED EARLY (2026-08-13)

Pulled forward on the product-alignment review: the atlas publishes **pack recommendations per
machine and use case** (survivors ranked by safe-accuracy per p95), so the reporting layer is
part of the recs product, not polish. Three "required adjustments before merge" from that
review landed together:

17. ✅ **Stratified `--limit`** (`stratified_sample` in classify.py): deterministic
    proportional draw by gold label set — same `(seed, limit)` draws the same cases on every
    fingerprint, every label keeps its catalog share, so an M3 Pro volunteer's 120-case run is
    comparable to a full 735-case run. `lane_meta.sampling` discloses which mode ran.
18. ✅ **Per-band drill-down** (`label_by_band` in every classify/emo lane report and history
    row): per-band exact-match against the same band families the difficulty report measures,
    so "this 9B routes scopes but answers through third-party requests" is readable directly,
    and readable against what bag-of-words gets on that band.
19. ✅ **Violation budget** (`GATE_VIOLATION_BUDGET_PER = 40`): hold/scope gates now tolerate
    1 violation per 40 graded hold turns (⌊63/40⌋ = 1 for the v3 tool lane) before flipping
    the verdict. One stochastic slip no longer erases a family; every violation is still
    reported verbatim, and the 0.10 hallucination-rate ceiling still excludes models that
    answer through holds often. Verified: 1-of-63 admits, 2-of-63 excludes.
    Wilson CIs on headline metrics remain open.
20. Deduplicate across lanes: assign each of the 29 shared texts to one lane. *(open)*
21. **Recalibrate the floors last**, against observed family runs. *(open — the macro-F1 floor
    is derived from the lexical ceiling, which bounds meaninglessness, not competence)*

**Also landed from the review — the publication gate itself.** `rank_ready` now travels
mechanically from `difficulty.DECLARED` into every lane score, history row, and rendered card:

- `score_competence_lane` stamps `rank_ready` + `catalog_version` on every lane; catalogs with
  no declaration (all tool/primary today) read as `False`.
- The BENCHMARK_CARD gains a `rankable` column; the atlas ranks **only** rankable admits, lists
  the rest as "gates-only admit (not rankable)", and **"Suggested pack posture" is suppressed**
  until tool *and* primary lanes are rank-ready — a posture composed from lanes whose quality
  axis is invalid (P4/P5 open) would be a false recommendation. Pre-review history rows lack
  the flag and correctly read as not-rankable.
- The emo competence is now rendered in the atlas (it was invisible to the report layer).

### P7 — Use the harvested pool

22. `reddit_expand.jsonl` holds 6,531 `tool_deny`, 1,070 `classify_label`, 227 `tool_empty`,
    186 `primary_hit` and 135 `tool_hit` rows that v3 ignores. That is where the plan's v5
    paraphrase volume can come from without hand-authoring — but only after P1, or the same
    matcher will filter it down to whatever it already recognizes.

---

## 7. Suggested order of work

P1 first and alone — it is the gate. Adding cases before gold is decoupled from the matcher
just grows a catalog that measures substring matching at larger n. Then P2 and P3 in parallel
(different files, no overlap), then P4+P5 as one retrieval-lane pass, then P6 recalibration
using the new baselines, then P7 for volume.
