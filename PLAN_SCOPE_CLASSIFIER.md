# Plan — Local Scope Classifier with LLM Escalation

**Status:** MEASURED THROUGH RUNG 3 — best arm found, NOT installed, blocked on one clause
**Date:** 2026-08-13 (status refreshed 2026-08-15)

**Where this actually stands** (detail in §9G; this block exists so a reader does not
have to reconstruct it from 900 lines of experiment log):

| arm | macro-F1 | negatives abstained | scopes >=0.60 |
|---|---|---|---|
| prototype, untrained | 0.387 | 0.797 | 1/14 |
| linear head on frozen MiniLM | 0.369 | 0.859 | 3/14 |
| mistral:7b (the status quo) | 0.436 | 0.078 | 8/14 |
| **DistilBERT, rung 3** | **0.446** | **0.937** | 1/14 |

Rung 3 beats every arm including the LLM, and abstains 12x better. It passes two of the
three §7 clauses and fails the third: 13 of 14 scopes sit below 0.60 recall. Precise and
insensitive.

**Nothing is installed.** The artifact lives in a scratchpad. Promotion needs the recall
clause cleared AND a 265 MB RSS decision.

**What is shipped, and it is not the classifier.** `scope_shadow.observe()` is called from
`query/pipeline.py`, OFF unless armed (`TOPOS_SCOPE_SHADOW=1` or the flag file, and
`TOPOS_SCOPE_SHADOW=0` disarms both), never raises, and decides
nothing — `classify()` has exactly one caller in the engine and it is that observer. Its
purpose is the one thing every measurement above still lacks: labelled REAL traffic. Every
number here comes from template-generated positives, and §9G names that distribution gap
as where the recall clause fails.

**So the open question is not "fine-tune or stop" — rung 3 is already the answer.** It is
whether to enable shadow mode long enough to get a benchmark-representative validation
slice, which §9G names as the first of the two remaining levers and which the promotion
decision is explicitly meant to be informed by.

**Before that flag is set anywhere:** the shadow log writes raw query text to
`~/.topos/scope_shadow.jsonl` (`TOPOS_SCOPE_SHADOW_LOG` redirects it). It now rotates at
8 MiB keeping one generation (`TOPOS_SCOPE_SHADOW_MAX_BYTES`, 0 to disable) — unbounded
query history on disk was the wrong default for a product whose security page promises
the data stays the owner's. See also §6.1: that promise is unqualified and constrains OUR
training, not just third parties'.

**Two switches, and the env one wins.** `TOPOS_SCOPE_SHADOW=1` or a
`~/.topos/scope_shadow.on` file arms observation; the file exists because the node under
the macOS app shell inherits no shell environment. `TOPOS_SCOPE_SHADOW=0` *disarms* it and
beats the file. That direction was missing until 2026-08-18, and its absence was reachable
in the one place it mattered: a subprocess harness inherits the operator's home directory,
so an armed flag file armed the harness too, and running the query path appended synthetic
traffic to a real person's log with no way to opt out from the environment. Harnesses that
run the query path on a machine that may have shadow armed should set
`TOPOS_SCOPE_SHADOW=0`, or redirect with `TOPOS_SCOPE_SHADOW_LOG`.
**Related:** [`AUDIT_ROLE_COMPETENCE_CATALOG_V3.md`](AUDIT_ROLE_COMPETENCE_CATALOG_V3.md),
[`PLAN_ROLE_COMPETENCE_EVAL.md`](PLAN_ROLE_COMPETENCE_EVAL.md)

Replace/front the LLM classify step with a local text classifier over the 14 UMA scopes, with
calibrated escalation to an LLM. Ships on infrastructure the node already runs in production.

---

## 1. Ground truth about what exists today

Three facts that shape everything below. All verified against the live engine
(`topos-control-plane/topos`), not assumed.

**1.1 The node already runs local transformer classifiers in production.**
[`topos/topos/engine/backends/huggingface.py`](topos/topos/engine/backends/huggingface.py) ships:

| constant | model | slot |
|---|---|---|
| `DEFAULT_EMOTION_MODEL` | `SamLowe/roberta-base-go_emotions` | `ModelSlot.EMOTION` |
| `DEFAULT_NER_MODEL` | `djagatiya/ner-roberta-base-ontonotesv5-englishv4` | `ModelSlot.NER` |
| `DEFAULT_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | `ModelSlot.EMBEDDING` |
| `DEFAULT_SENTIMENT_MODEL` | `cardiffnlp/twitter-roberta-base-sentiment-latest` | `ModelSlot.SENTIMENT` |
| `DEFAULT_URL_CLASSIFICATION_MODEL` | `KnutJaegersberg/website-classifier` | `ModelSlot.URL_PIPELINE` |

behind `get_model_cache().acquire(slot, model, loader)`, an `ensure_model()` that does HF
`snapshot_download`, a `_SUBTYPE_TO_SLOT` dispatch table, and an enrichment model registry keyed
on `task_name` / `model_type` / `is_preferred`
([`topos/topos/enrichment/models/registry.py`](topos/topos/enrichment/models/registry.py)).

**Adding a scope classifier is a new `ModelSlot`, a `_SUBTYPE_TO_SLOT` row, one backend method,
and one registry entry. It is not new infrastructure.**

**1.2 emo_27 is already a local model, not an LLM.** `subtype in ("emotion_classification",
"emo_27")` runs RoBERTa and writes to `message_emotions` (`emotion_label`, `confidence`,
`all_emotions_json`, `model`). Consequence for the eval track: the 216 emo cases — 30.4% of
`role_classify_3.json` — benchmark LLM families on a task the product does not buy from an LLM.
**emo_27 should be split out of the classify catalog into its own lane** scored against the local
model, not folded into `label_accuracy`.

**1.3 Scope selection is not LLM-predicted in the live query path.**
[`scope_resolution.py`](topos/topos/scope_resolution.py) is a static dict, and
`TurnClassifierLite.classify()` in [`topos/topos/query/turn_classifier.py`](topos/topos/query/turn_classifier.py)
takes `turn.scope_id` as an **input** — it denies on `missing_scope`, it does not predict one.
So this plan is **new capability** (let free-form text select scopes reliably), not a cost swap
against an existing hot-path LLM call. Frame the ROI accordingly: the win is coverage and
correctness on free-text entry, plus removing an LLM dependency from any future path that would
otherwise need one — not "we deleted an LLM call that was costing us."

**1.4 The taxonomy is already canonical and already matches the eval.**
[`topos/topos/query/scope_registry.json`](topos/topos/query/scope_registry.json) holds 14 scopes,
all `implementation_status: live`, and they are an **exact 14/14 match** with
`SCOPE_LABELS` in `topos-eval`. No taxonomy reconciliation is needed. The registry also carries
per-scope `description`, `example_questions` (25 human-authored total), `primary_dimensions`,
`raw_tables` and `default_mode_ceiling` — all usable as classifier priors.

---

## 2. Sequencing — why "v10 first, then classifier" is the wrong order

The proposed order was: grow the catalog v3 → v10, then train a classifier, then add escalation,
then add batch retraining. The instinct is right that honest data must come before training. The
ordering has three problems.

**2.1 Training on v10 destroys v10.** The catalog is the benchmark. If the classifier trains on
it, there is no held-out measurement and every subsequent score is contaminated. The catalog and
the training set must come off the **same generator with disjoint splits**, and the number you
quote as a production claim has to come from a node-local run on real traffic (§7).

**2.2 Synthetic data has a ceiling no amount of scale fixes.** Even a perfect v10 is template-
generated, and a classifier trained only on templates is excellent on templates and weaker on real
utterances. Distribution shift is the failure mode a synthetic catalog is structurally blind to.

The privacy constraint in §6 means the **shipped base model must live with that ceiling** — real
traffic never pools, so it can never train the base. Two consequences worth internalising now:
catalog quality is on this plan's critical path, not just the eval's; and the gap between base and
ceiling is closed **per-node**, by opt-in local adaptation, not centrally.

**2.3 Escalation is the data-collection mechanism, so it comes first, not last.** You cannot get
"iterative batch training" without an escalation loop already running, because escalations *are*
the labelled hard cases. Shipping the ladder first — even with nothing but an LLM behind it —
starts the flywheel immediately and is what makes everything downstream possible.

There is also no reason to serialize. The catalog track (v4 → v10) and the classifier track share
one prerequisite — P1 from the audit, decoupling gold from the fitted matcher — and after that
they run in parallel on different files.

### Corrected order

```
                 P1: decouple gold from fake_classify_label  (audit §6, in flight)
                                    |
              +---------------------+---------------------+
              |                                           |
     MEASUREMENT TRACK                            CAPABILITY TRACK
     catalog v4 -> v5 -> v8 -> v10                M0 prototype baseline (no training)
     near-miss band, real GoEmotions,             M1 escalation ladder + logging  <-- flywheel starts
     distractor packets, arg matching             M2 real-traffic corpus accrues
              |                                   M3 train head on MiniLM
              |                                   M4 promote in front of LLM, calibrated
              +------------------ shares generator, never shares split ----------+
                                    |
                          M5 batch retraining loop
```

**M0 and M1 do not depend on the catalog at all.** M0 needs zero training data. M1 needs zero
training data. Both can start now, in parallel with P1.

---

## 3. Label shape

Not single-label. `gold_labels` is a list and the eval scores exact set match. Two orthogonal
taxonomies are currently stapled into one closed set; this plan separates them.

| head | labels | shape | metric |
|---|---|---|---|
| **scope** | 14 UMA scopes | multi-label sigmoid + abstain | micro-F1, macro-F1, abstention calibration |
| **emotion** | GoEmotions 27 + `neutral` | multi-label sigmoid | macro-F1 vs GoEmotions dev |

The scope head must be genuinely multi-label: *"am I free after lunch Friday according to my
calendar?"* is `availability:read` **and** `schedule:read`, and
[`topos/topos/sources/registry.py`](topos/topos/sources/registry.py) already pairs scopes this way
in `allowed_scope_ids` (`["schedule:read", "availability:read"]`,
`["contacts:resolve", "relationship_context:read"]`, `["ai_conversations:read", "work_context:read"]`).
A 15-way softmax would structurally forbid the correct answer.

**`none` is not a 15th class.** It is "no sigmoid crossed threshold." Modelling it as a class
teaches the head to compete `none` against real scopes instead of calibrating confidence, and it
is the abstain path that matters for the privacy story.

---

## 4. Architecture — a three-rung ladder, not one model

Each rung is independently shippable and each falls back to the next. Do not skip to rung 3.

### Rung 1 — prototype similarity (no training data required)

Embed each scope's `description` + `example_questions` from `scope_registry.json` into prototype
vectors using the **already-slot-cached** `all-MiniLM-L6-v2`. Classify by cosine against
prototypes with a threshold.

- Zero training data, zero extra model download, zero extra RAM slot.
- Ships day one. Establishes the interface, the logging, and the escalation contract.
- Expect mediocre accuracy. That is fine — its job is to be the cold start and to make every
  subsequent rung a drop-in replacement behind a stable interface.

### Rung 2 — linear/MLP head on existing embeddings

Train a small head on MiniLM embeddings once real labelled data exists (M2).

- Still no extra model download, no extra RAM slot, no extra load latency — the embedding model
  is already resident for retrieval.
- Strong baseline for 14-way short-text routing. Retrains in seconds on CPU.
- **This is the recommended target.** Only go past it if it demonstrably plateaus.

### Rung 3 — fine-tuned DistilBERT / MiniLM encoder

Only if rung 2 plateaus below the gate. Costs a new `snapshot_download`, a new `ModelSlot`, and
additional resident RSS — which matters: the node ships a frozen `uv tool` snapshot, and
`PLAN_ROLE_COMPETENCE_EVAL.md` §3.4 already has a `bad-neighbor` exclusion on headroom ceiling.
Budget the RSS before committing.

### Escalation contract (all rungs)

```
classify(text) -> {labels, confidence, source: "prototype"|"head"|"llm", escalated: bool}

  max sigmoid >= tau_high         -> return labels, source=<rung>
  tau_low <= max < tau_high       -> escalate to LLM, log as HARD, return LLM labels
  max < tau_low                   -> return {} (abstain), log as ABSTAIN
```

Two thresholds, both tuned on held-out real traffic, both recorded in the model registry row so a
threshold change is a versioned event. A classifier miss degrades to **today's behaviour**, never
to a wrong answer. `tau_high` is the coverage knob: start it high (escalate often, cheap to be
wrong-and-caught) and lower it as the confusion matrix earns trust.

---

## 5. Integration points

| what | where | change |
|---|---|---|
| slot | `topos/topos/engine/model_cache.py` | add `ModelSlot.SCOPE_CLASSIFIER` |
| dispatch | same file, `_SUBTYPE_TO_SLOT` | `"scope_classification"`, `"scope_classification_batch"` |
| backend | `topos/topos/engine/backends/huggingface.py` | `_get_scope_model()` + `_run_scope_classification()`, mirroring `_get_emotion_model` / `_run_emotion_classification` exactly |
| default | same file | `DEFAULT_SCOPE_MODEL` (rung 1: reuse `DEFAULT_EMBEDDING_MODEL`) |
| registry | `topos/topos/enrichment/models/registry.py` | row with `task_name="scope_classification"`, thresholds, `is_preferred` |
| priors | `topos/topos/query/scope_registry.json` | read-only consumer — descriptions/examples become prototypes |
| escalation | new module `topos/topos/query/scope_classifier.py` | ladder + thresholds + logging |

`LEGACY_SCOPE_IDS` in `scope_registry_loader.py` (`aiChat:read`, `publicBio:read`, `events:read`, …)
must never be emitted by the classifier. Assert the output label set equals the 14 live registry
scope_ids at load time, so a registry change fails loudly instead of silently routing to a dead
scope.

---

## 6. Training data — and the privacy constraint that decides its shape

### 6.1 The published claim this plan must not break

`topos-website-v2/src/content/securityContent.ts:54`, live under "AI without surrender":

> **"No training on your data.** Run sensitive inference on local models, or bring your own keys."

`topos-website-v3_CLAIMS_LEDGER.md` item 5 already marks this **NEEDS PROOF**, but for an
unrelated reason — it reads it as a claim about *third-party providers'* training policies.
Nobody has yet read it as a constraint on **our own** training. It is one, and it is the binding
one here.

The privacy policy uses narrower language — `policySections.ts:63`, "not used to train
Dialogues-owned **foundation models**." A scope classifier is not a foundation model, so the
policy arguably leaves room. **Do not take that room.** The security page says "No training on
your data" with no qualifier, and that is the sentence a user reads. Leaning on the narrower
policy wording to permit the broader practice is precisely how a NEEDS-PROOF claim becomes a
FALSE one — and this repo has already had to retract two false security-page claims.

**Verdict: pooled training on real user chat data breaks the claim. Not ambiguously — directly.
So the architecture does not do it.**

### 6.2 What is and isn't a breach

| approach | breaks the claim? | why |
|---|---|---|
| Pool real query text off nodes, train a shipped model | **yes** | contradicts both "No training on your data" and "your data never leaves the machine" (`docs/006-multiple-topoi.md`) |
| Pool a *scrubbed derivative*, train centrally | **yes** | scrubbing is a mitigation, not a negation. The claim has no scrubbing exception, and short query text stays quasi-identifying after names are stripped ("what did I discuss about the Kestrel firmware contract") |
| Train the shipped base model on synthetic + public data only | no | contains zero user data |
| Node-local training on the owner's own data, artifact never leaves | no | same trust boundary the node already operates in — `SamLowe/roberta-base-go_emotions` and the OntoNotes NER already run over owner data locally every day |
| Train on the founder's own node data and **ship** it | not a *user* breach, but **banned** | small fine-tuned models memorise; shipping one distributes an artifact derived from one person's private life. Fine for answering "is rung 2 viable", then delete it |

The distinction that matters: **inference over private data is transient; a model trained on
private data is a persistent, distributable derivative of it.** Running a classifier on owner
data is what the node already does. Shipping a model that *learned from* owner data is a
different act.

### 6.3 The architecture that follows

**Shipped base model — synthetic and public data only.**
Catalog v4+ generator (disjoint split), GoEmotions, `scope_registry.json` descriptions and
example_questions. Zero user data. Ships to every node. This is what the v4 → v10 measurement
track actually feeds.

**Node-local adaptation — opt-in, per-node, never leaves.**
Each node calibrates its own thresholds and, at rung 2, trains its own head on its own owner's
escalations. The artifact stays on the node. For a personal-AI product this is *better* than a
pooled model, not a compromise: the thesis is that your node knows you, and a head fitted to one
owner's phrasing outperforms a population average on that owner.

**Telemetry — counts, never text, opt-in.**
"escalation rate 12%", "macro-F1 0.81", "`places:read` recall dipped." Never a query string,
never an embedding of one. Embeddings are invertible enough to count as content.

| source | volume | provenance | use |
|---|---|---|---|
| `scope_registry.json` example_questions | 25 | human-authored | rung-1 prototypes; never train/test |
| MASSIVE + CLINC150, mapped (§6.5a) | 10k+ | public, CC-BY | **the `none` class + register anchor**; positives only for schedule / messages / contacts |
| public QA filtered by scope vocabulary (§6.5b) | unbounded | public, licensed | **near-miss band, targeted at the 9 scopes MASSIVE cannot cover** |
| schema-grounded generation (§6.5c) | unbounded | node structure, zero values | **the positive class — the only source for the 9 distinctive scopes** |
| catalog v5+ generator (disjoint split) | ~5k at v10 | synthetic templates | base model training, capped — see §6.4 rule 2 |
| GoEmotions test+dev | public | public dataset | emotion head only (`train` reserved — see below) |
| node-local escalation log | grows from M1 | owner's own data | **node-local adaptation only — never pooled, never shipped** |
| aggregate confusion telemetry (§6.5g) | counts only | opt-in, no text | steers which synthetic bands to author next |

**GoEmotions `train` stays reserved.** P3 already deliberately built `role_emo_1.json` from
test+dev only so a locally fine-tuned emotion classifier is never scored on its own training
data. That reservation is now load-bearing for this plan: `train` is the emotion head's corpus,
test+dev is the ruler. Do not spend it.

### 6.4 Hard rules

1. **No user text, and no embedding of user text, leaves the node. Ever.** Enforced at the
   telemetry boundary, not by policy. A CI test should assert the telemetry payload schema
   contains no free-text field.
2. **Never train on any split that scores the benchmark.** Generator shared, splits disjoint,
   enforced by a CI test asserting empty intersection on normalized text.
3. **The shipped base model's training manifest is auditable.** Every corpus in it is synthetic
   or public and named in the model registry row. This is what makes "No training on your data"
   provable rather than merely asserted — and the open-source engine claim means someone will
   check.
4. **Over-sample the near-miss band** ("How does sleep affect memory?" → abstain, not
   `health:read`). Per the audit §2.2 this band is currently inexpressible; P1 unblocks generating
   it, and it is where a classifier fails silently and mis-routes a real user.
5. **Founder-node experiments are development-only.** Useful for answering viability questions;
   the resulting artifact never ships.

### 6.5 Building it without user data — the actual sources

Decision 2026-08-13: hold the `securityContent.ts:54` boundary and build the base model from
non-user sources. Ranked by value.

**(a) Public assistant-utterance corpora — the register match.** The one thing hand-written
templates cannot fake is how people actually phrase short first-person requests to an assistant.
That exists publicly, labelled, at scale:

**Licences verified 2026-08-13.** Open decision 5 is resolved: two of the three clear, one does not.

| corpus | licence | verdict | size / fit |
|---|---|---|---|
| **Amazon MASSIVE** | **CC BY-4.0** | ✅ **use** | ~843k utterances (v1.1: 859k), 60 intents, 18 scenarios, 54 languages |
| **CLINC150** (`clinc/oos-eval`) | **CC BY-3.0 Unported** | ✅ **use** | 150 in-scope intents (100/20/30 per intent) + **100 train / 100 val / 1000 test out-of-scope** |
| **Google SGD** (DSTC8) | **CC BY-SA-4.0** | ⚠️ **do not use without counsel** | 20k+ dialogues, 20 domains |
| GoEmotions *(already shipped in P3)* | **Apache-2.0** | ✅ clear | emotion head; existing use is fine |

#### Measured scope coverage — MASSIVE is a negative-class asset, not a positive-class one

Mapped against all 60 MASSIVE intents. **This corrects an earlier overstatement in this plan**,
which implied broad positive coverage.

| UMA scope | MASSIVE positives | verdict |
|---|---|---|
| `schedule:read` | `calendar_query`, `datetime_query` | ✅ clean |
| `messages:read` | `email_query`, `social_query` | ✅ clean |
| `contacts:resolve` | `email_querycontact` | ✅ clean |
| `availability:read` | subset of `calendar_query` | ◐ partial, needs filtering |
| `places:read` | `transport_query`, `transport_traffic`, `recommendation_locations` | ◐ weak — these are *going somewhere*, not *where I have been* |
| `health:read`, `activity:read`, `ai_conversations:read`, `attention:read`, `complexity:read`, `work_context:read`, `public_bio:read`, `relationship_context:read`, `resources:read` | none | ✗ **uncovered** |

**3 of 14 clean, 2 partial, 9 uncovered** — and the 9 are precisely Topos's distinctive scopes.
That is not a surprise in hindsight: MASSIVE descends from SLURP, a 2018-era smart-speaker
taxonomy (alarms, IoT, music, takeaway). Topos is a personal knowledge graph. Different ontologies,
overlapping only on the calendar/messaging/contacts core.

**Where it is genuinely strong is the negative class.** Roughly 45 of the 60 intents — every
`qa_*`, `general_*`, `weather_*`, `news_*`, `play_*`, `music_*`, `iot_*`, `alarm_*`, `audio_*`,
`cooking_*`, `takeaway_*`, `lists_*`, `recommendation_*` — are **real human assistant-directed
utterances that are not owner-personal-data reads**. That is exactly the register-matched `none`
class the audit found missing, and `qa_stock` / `qa_currency` are genuine near-misses for
`resources:read`. CLINC150 behaves the same way: its OOS split and most of its 150 banking/travel
intents read as `none` from the UMA point of view.

**So the division of labour is cleaner than first drawn:**

- **Public corpora own the `none` / near-miss class and the register anchor.** This solves a
  *named blocking problem* — audit §3.3, the AskReddit pool was exhausted at 225 of 229 available
  rows, and `none` was the single most lexically trivial segment at 0.996 CV.
- **Schema-grounded generation (§6.5c) owns the positive class.** It has to: nine scopes have no
  public analogue, and the schema is the only thing that knows what they contain.
- **Public QA filtered by scope vocabulary (§6.5b) is promoted** — for the near-miss band
  specifically it has unlimited supply and can be targeted at the exact nine uncovered scopes,
  which CLINC150's fixed 1,200 OOS rows cannot.

**Why SGD is out.** CC BY-**SA** is share-alike. Whether trained model weights are a "derivative
work" of their training data is legally unsettled, and some dataset providers have explicitly
asserted that models trained on their CC BY-SA data *are* derivatives. Creative Commons' own
guidance and standard practitioner advice both land on: don't fine-tune a model you intend to
ship under your own terms on CC BY-SA data without specific legal clearance; prefer CC-BY, CC0 or
Apache-2.0. For a product whose entire position is honest claims, taking a contested legal stance
on training-data licensing is precisely the wrong risk to accept. Drop SGD, or get counsel first.

The loss is small. CLINC150's OOS split is the closest public analogue to the band the audit found
v3 could not express, MASSIVE carries the register anchor, and both are cleanly licensed. SGD was
the least load-bearing of the three.

**Attribution obligation.** CC-BY-4.0 and CC-BY-3.0 both require attribution on redistribution.
The shipped base model's training manifest (§6.4 rule 3) must name MASSIVE, CLINC150 and
GoEmotions with their licences. That manifest is already required for the "No training on your
data" claim to be provable — it now carries the attribution obligation too.

**(b) Public QA corpora for the near-miss band.** The hardest class — a world question sharing
scope vocabulary — is abundant in public data. Filter Natural Questions / MS MARCO / ELI5 /
StackExchange by scope vocabulary (`sleep`, `heart rate`, `calendar`, `salary`, `relationship`)
and label `none`. Strictly better than the exhausted AskReddit pool, which is register-mismatched
poll titles; these are genuine information-seeking questions on exactly the scopes' topics. This
is P2's raw material and it needs zero user data.

**(c) Schema-grounded generation — realism without data.** Designed in full in
[`DESIGN_SCHEMA_GROUNDED_GENERATOR.md`](DESIGN_SCHEMA_GROUNDED_GENERATOR.md). Generate from the
node's own structure
rather than a writer's imagination: `scope_registry.json` (`primary_dimensions`, `raw_tables`,
`signal_objects`, `summary_objects`, `default_source_ids`), `shared/schema_registry.py` (column
names + categories), `sources/registry.py` (`allowed_scope_ids` per connector). Queries can
reference **real column and connector names with zero real values** — "did my resting heart rate
from whoop drop this week" is structurally grounded and privately empty. Bonus: it self-updates.
Add a connector, the generator emits queries for it.

**(d) Style transfer, not free invention.** Where an LLM paraphrases, few-shot it with public
assistant utterances as *style exemplars* rather than letting it invent how people talk. An LLM's
unprompted idea of a user query is a recognisable register of its own and it will leak into the
head.

**(e) Few-shot methods cut the requirement by ~10×.** The earlier "300–500 real examples per
scope" figure assumed standard fine-tuning. SetFit-style contrastive tuning on the already-cached
MiniLM is designed for 8–64 examples per class — 14 scopes × ~32 authored-and-audited examples is
~450 items, reachable from (a)–(c) alone. **Evaluate this before assuming a data shortage exists.**

**(f) Authored ≠ observed.** A user who *writes* an example query knowing it becomes training data
is donating, not being harvested — categorically different from reading their chat log, and the
claim permits it. Keep it explicitly opt-in, keep it small, and never describe it in copy in a way
that muddies the boundary. Your own team and community can author near-miss queries without
touching any node at all. Low yield; last resort, not first.

**(g) The loop: real traffic shapes the *curriculum*, never the *corpus*.** This is the piece that
makes the constraint survivable. Node-local, the classifier knows where it is uncertain. Report
**aggregate confusion, not content** — "`availability:read` ↔ `schedule:read` confusion at 14%",
"`places:read` recall 0.51". That is a number, not a query. Centrally, author more synthetic data
in exactly that band; the base model improves for every node. **Real usage improves the model
through error signals rather than through examples**, and nothing leaves that could be inverted
back into text.

**Founder data is the ruler, not the material.** Training the shipped model on your own node data
is banned (§6.2). But evaluating a synthetic-trained model *against* your real queries,
node-locally, is the single highest-value use of that data under the constraint: it measures the
register gap directly, contaminates no training set, and moves nothing. Do that at M3 to decide
whether rung 2 is viable at all.

### 6.6 The cost of this, stated plainly

This is slower and harder than pooling. You cannot look at a pile of real queries and iterate. You
debug through aggregate metrics, node-local eval runs, and whatever the owner volunteers. Base-
model quality is capped by synthetic data quality, which puts more weight on the catalog track
than it would otherwise carry — the audit's P1 is now on this plan's critical path too.

That cost is the product. It should be paid deliberately and named in the plan rather than
discovered at M2.

---

## 6A. The schema becomes a versioned interface

Shipping a classifier binds a trained artifact to the scope taxonomy. `scope_registry.json` stops
being a file you can edit freely and becomes a schema with migration semantics — like a wire
protocol enum or a DB schema. This is a real architectural consequence and it needs stating before
M3, not after.

### 6A.1 Not everything couples equally

| artifact | what it feeds | coupling | change cost |
|---|---|---|---|
| `scope_registry.json` **scope_id set** | the classifier's **output space** | **hard** | head reshapes; retrain + revalidate |
| `scope_registry.json` descriptions / example_questions | rung-1 prototypes, generation seeds | soft | regenerate + retrain; no architecture change |
| `sources/registry.py` `allowed_scope_ids` | multi-label co-occurrence priors, connector vocabulary | soft | regenerate |
| `shared/schema_registry.py` column names | §6.5c generated query vocabulary only | very soft | regenerate; see below |

Only the **scope_id set** is a breaking interface. The rest is a data-generation input.

On column names specifically: the classifier should **not** be keyed on them. Users say "my
resting heart rate", not `resting_heart_rate`. Schema-grounded generation (§6.5c) uses the schema
to enumerate *what is askable*, then phrases it naturally — the column name is a coverage index,
not a feature. Done that way, a column rename costs a regeneration, not a regression.

### 6A.2 Migration rules

Same discipline as an enum in a wire protocol:

1. **Never reuse a `scope_id`. Never change what one means.** Deprecate and add; do not mutate.
   A renamed scope with a positionally-indexed head is silent catastrophic mis-routing — the worst
   possible failure for a permission-adjacent component.
2. **Record the scope-registry version in the model registry row.** The load-time check in §5
   should be a *versioned compatibility* check, not just a set-equality assert. Set-equality fails
   closed on any additive change, which is too brittle.
3. **Additive changes degrade safely.** A new scope the model cannot predict routes to its nearest
   neighbour or abstains — degraded but not wrong, and the escalation path catches it. A new scope
   should therefore ship as *LLM-only* until the next base-model cycle covers it. That is a
   feature: taxonomy can move faster than the model.
4. **Removals leave a dead output.** Map to abstain, never silently to a neighbour.

### 6A.3 The inversion — the classifier audits the taxonomy

A classifier is only as separable as its label space. If two scopes overlap semantically, **no
model can separate them**, and the evidence shows up as a permanent confusion-matrix cell that
more data never fixes. That makes the confusion matrix a **measurement instrument for taxonomy
quality** — the classifier does not just consume the schema, it grades it.

Candidate seams already visible in the current 14:

- `schedule:read` ↔ `availability:read` — availability is *computed from* schedule. "Am I free
  Friday?" is not linguistically separable from "what's on Friday?"
- `attention:read` ↔ `complexity:read` — both derived cognitive-state overlays.
- `work_context:read` ↔ `public_bio:read` — overlap on exactly the "staff engineer" vocabulary.

Note the multi-label seeds in `sources/registry.py` already pair the first and third
(`["schedule:read", "availability:read"]`, `["public_bio:read", "work_context:read"]`) — the
system has effectively already conceded these co-occur.

### 6A.4 The design tension worth resolving deliberately

The current scope boundaries are drawn along **storage and permission** lines: which tables, which
disclosure ceiling, which grant. That is correct for authorization and should not be compromised
to suit a model. But it is a different job from being **separable in language**, and one taxonomy
is currently being asked to do both.

Two ways out, and this is a real decision rather than a detail:

- **(A) Keep one taxonomy, accept the confusion cells.** Where scopes are linguistically
  inseparable, predict the *set* — multi-label already supports this, and over-predicting
  `{schedule:read, availability:read}` is safe if both are granted. Cheapest; the cost is a
  permanently softer confusion matrix and some over-broad scope requests.
- **(B) Split routing from authorization.** The classifier predicts a coarser **routing**
  taxonomy that maps many-to-one onto permission scopes — predict `time:read`, expand to
  `{schedule:read, availability:read}` at the permission layer. Decouples "what the model can tell
  apart" from "what the system must authorize separately", and lets the permission taxonomy get
  *finer* over time without hurting the model.

**Recommendation: (A) now, design for (B).** Don't restructure the permission taxonomy on
speculation. Let M2/M3 produce a real confusion matrix on the current 14, then decide with
evidence — the seams above are hypotheses, and this is exactly what the measurement is for.
Keeping the classifier's output behind a mapping layer from day one makes (B) a later
configuration change rather than a rewrite, so build that indirection at M1 even while it is an
identity map.

---

## 7. Metrics and gates

Reuse the existing envelope discipline (`_rate_envelope`, Wilson CIs) rather than inventing a
second reporting shape.

| metric | why | gate |
|---|---|---|
| macro-F1 (scope head) | 14 scopes, imbalanced — a micro average hides rare-scope collapse | ≥ LLM baseline |
| per-scope recall | one dead scope is invisible in any average | no scope < 0.60 |
| **near-miss abstain rate** | the band that mis-routes real users | ≥ 0.85 |
| escalation rate | the cost knob | reported, budgeted |
| p95 latency | the reason to do this at all | measured via `scripts/bench_embedders.py` |
| resident RSS delta | node headroom / bad-neighbor exclusion | budgeted before rung 3 |

**Promotion gate (rung N in front of LLM):** macro-F1 ≥ the LLM's own macro-F1 on the same
held-out set, **and** near-miss abstain ≥ 0.85, **and** no per-scope recall < 0.60. Beating the LLM
on average while silently destroying one scope is not a win.

**Where the gate is evaluated.** Two runs, both required. (a) The **synthetic held-out split**,
run centrally — this gates the shipped base model and is fully reproducible. (b) A **node-local
run on real traffic**, executed on-node with only the aggregate numbers returned (§6.3). The
second is the one that predicts production behaviour; the first is the one you can debug. Never
report (a) alone as a production claim.

Latency: measure, don't estimate. Both sides are already instrumented — `bench_embedders.py` for
the encoder, `llm_p95_ms` / `wall_ms_per_case` in the eval harness for the LLM.

---

## 8. Milestones

| id | milestone | depends on | ships |
|---|---|---|---|
| **M0** | ✅ **DONE 2026-08-13** — rung-1 prototype classifier | nothing | `topos/query/scope_classifier.py`, `scripts/eval_scope_classifier.py`, 15 tests. **Viability answered: see §9A.** |
| **M1** | ✅ **DONE 2026-08-13** — escalation ladder + logging boundary | M0 | `topos/query/scope_router.py`, 16 tests. **Finding in §9B.** |
| **M2** | Public-corpus adapters (§6.5a/b) + schema-grounded generator (§6.5c); node-local escalation corpus accrues; telemetry = counts only | M1 running | corpus adapters + licence audit, scope-mapping table, no-free-text telemetry CI test |
| **M3** | Rung-2 base head, few-shot first (§6.5e); founder-node reality check as the ruler | M2 | base head artefact + auditable training manifest, split-disjointness CI test, register-gap measurement |
| **M4** | Promote head in front of LLM behind the gate in §7 | M3 | calibrated `tau_low`/`tau_high`, promotion report |
| **M5** | Batch retraining loop on accumulated escalations | M4 | scheduled retrain, champion/challenger, rollback |
| **M6** | *(conditional)* Rung-3 fine-tune | M4 plateau + RSS budget | only if the gate is unmet |

M0 and M1 are unblocked today. M3 is the first thing that needs P1.

**M5 note — champion/challenger, not blind retrain.** A retrained head ships only if it beats the
incumbent on the frozen held-out real-traffic set. Otherwise the flywheel silently drifts toward
whatever the LLM happened to say, which is exactly the circularity the audit found in the catalog,
reintroduced through the back door. The LLM labels escalations; it does not get to define truth
unchallenged. Keep a human-audited slice of the test set that no model labelled.

---

## 9A. M0 result — the viability question, answered

Measured 2026-08-13 on `role_classify_7.json` (930 cases), `tau_high=0.42`, `tau_low=0.28`.
No training, no extra model: prototypes are the registry's own `description` +
`example_questions`, embedded with the already-resident MiniLM.

| metric | M0 | gate (§7) |
|---|---|---|
| macro-F1 | **0.403** | ≥ the LLM's |
| exact-set accuracy | 0.357 | — |
| escalation rate | 0.368 | budgeted |
| **negatives abstained** | **0.877** | ≥ 0.85 ✅ |
| per-scope recall | **0/14 clear 0.60** ❌ | none < 0.60 |

**M0 fails the promotion gate, and it should — but it splits cleanly into good news and
bad news, and they are not the same half.**

**The safety half already works.** The bands where a mistake is a *leak* rather than a
wrong answer are the strong ones: `none` 0.986, near-miss 0.856, third-party 0.821, and
negatives abstain at 0.877 — already over the gate. The escalation ladder does what it
was designed to do: 37% of traffic hands to the LLM instead of guessing a scope.

**The routing half does not.** `work_context:read` 0.000, `relationship_context:read`
0.032, `health:read` 0.184. Rung 1 is safe but not yet useful.

**Diagnosis, and it decides the next lever.** The obvious hypothesis was thin prototypes —
several scopes have only one or two example questions. It is wrong: the correlation
between prototype count and per-scope recall is **+0.14 across 14 scopes**, i.e. nothing.
Authoring more example questions will not fix this.

What is actually wrong is register. Descriptions are written in *schema* language
("Relationship signals derived from communications: who you interact with and how those
connections evolve") and queries arrive in *user* language ("am I drifting from Tomas").
Cosine over a general-purpose sentence embedder does not bridge that gap. **That is
exactly what a trained head learns and a prototype cannot** — so M0's failure is
evidence *for* rung 2, not against the approach.

**Threshold behaviour is a real knob, not a formality.** Sweeping `tau_high` trades
routing quality against leak safety in the direction you would want:

| `tau_high` | macro-F1 | escalation | negatives abstained |
|---|---|---|---|
| 0.30 | 0.487 | 0.05 | 0.589 ❌ |
| 0.40 | 0.430 | 0.31 | 0.830 |
| 0.45 | 0.349 | 0.45 | 0.921 |
| 0.50 | 0.276 | 0.54 | 0.964 |

The best macro-F1 sits at the *loosest* threshold, where negatives leak — which is why
the gate is a conjunction and not an average. 0.42 is the honest default: it is the
lowest threshold that still clears the 0.85 abstain gate.

**Verdict: proceed to rung 2.** The interface, the ladder, the thresholds and the
measurement harness are all in place and rung-agnostic, so M3 is a drop-in behind a
stable API. What M0 rules out is shipping rung 1 as the answer.

---

## 9B. M1 result — the ladder holds, and one structural gap it cannot close

`topos/query/scope_router.py`. classify → escalate → log, with the two-sided data
boundary from §6.3 made concrete: `EscalationRecord` has **two serializers that are
deliberately not interchangeable**. `as_local_row()` keeps everything including the text
— it is the owner's own data on the owner's own machine, and it is what makes M3
possible. `as_telemetry()` emits counts and closed-set enums only; confidence is bucketed
because a raw float is a fingerprint and a band is a statistic.

That boundary is a test, not a promise. `test_record_telemetry_is_closed_set_only`
asserts every string in the telemetry payload is a live scope id or a known enum, so
adding a free-text field to it fails the build. §6.4 rule 1 asked for exactly this.

**The ladder fails closed everywhere it can fail.** An LLM outage, an unwired escalator,
or an LLM returning a legacy/unknown scope id all hold the abstain rather than opening a
scope. A logging failure never breaks routing. Verified end to end.

**The structural gap: rung 1 cannot represent "whose".** Probing six owner/third-party
pairs, one leaked — *"what's on Priya's calendar Friday"* answered `schedule:read` at
0.53. The other five escalated or held. But the reason they held is that third-party
phrasings happen to score lower against a topic prototype, **not** because anything in
rung 1 encodes possession. Cosine similarity to a topic centroid is blind to whose
records are being asked for; the safety here is incidental.

This is not tunable — lowering `tau_high` to catch it costs the routing quality M0
already lacks. It is precisely what a trained head learns from **G4's third-party
twins**, which is now the clearest argument for building them into the M3 corpus rather
than the ranking catalog. Until M3 lands, treat third-party protection as *the LLM's*
job, not the classifier's.

---

## 9C. Three-arm bake-off — the LLM baseline §7 was missing

`scripts/compare_scope_classifiers.py`. Until now §9A reported only the left-hand side of
a gate written as "macro-F1 ≥ **the LLM's**", which made the gate unevaluable. All three
arms below run on **one identical test split** — 708 train / 222 test, grouped by
`template_id` so no phrasing spans both sides.

| arm | macro-F1 | exact-set | negatives abstained | scopes ≥0.60 | p50 latency |
|---|---|---|---|---|---|
| prototype (M0, no training) | 0.421 | 0.329 | 0.797 | 1/14 | 11.7 ms |
| **trained head @0.40** | **0.650** | **0.559** | **0.875** ✅ | 5/14 | **0.1 ms** |
| mistral:7b (local LLM) | 0.449 | 0.216 | **0.078** ❌ | 8/14 | 1977 ms |

**The trained head beats the local LLM on macro-F1 (0.650 vs 0.449), on exact-set (0.559
vs 0.216), and on latency by ~20,000×** — on 708 synthetic training examples, logistic
regression over MiniLM embeddings, no tuning beyond the threshold.

**The LLM's failure is the one that matters here: it will not abstain.** 0.078 of
negatives held, verified by hand rather than inferred from a parse:

| question (gold `none`) | mistral:7b said |
|---|---|
| How does sleep affect memory consolidation? | `health`, `complexity:read` |
| What is a good resting heart rate for adults? | `health:read` |
| **what is the capital of Mongolia** | `activity:read`, `places:read` |
| What's Priya's mood been like last month? | `activity:read`, `complexity:read`, `health:read` |

A classifier that opens a scope for "what is the capital of Mongolia" is a
data-minimisation failure, and it is exactly the near-miss band
AUDIT_ROLE_COMPETENCE_CATALOG_V3.md §2.2 said the old catalog could not even represent.
Its 8/14 per-scope recall is an artifact of the same defect — labelling nearly everything
makes recall look good while precision collapses.

**Three caveats, all load-bearing:**

1. **This is one small local model, not "LLMs".** mistral:7b is what you would actually
   run on-device, so it is the right comparison for *this* decision — but a frontier
   model would very likely abstain far better. Do not generalise the row.
2. **The head has home-field advantage.** It is trained and tested on synthetic
   classify-7; the LLM meets that distribution cold. On real user phrasing the gap
   narrows, possibly a lot. This is the register gap from §9A cutting the other way, and
   it is precisely why §6.5's real-traffic evaluation is not optional.
3. **The head still fails the §7 gate** — 9 of 14 scopes sit below 0.60 recall
   (`work_context:read` 0.292, `relationship_context:read` 0.300). Beating the LLM on
   average is not the gate; the gate is a conjunction.

**What this settles:** the rung-2 approach is validated — a tiny trained head on already-
resident embeddings outperforms the local LLM on both quality and safety at a fraction of
the cost. **What it does not settle:** whether that holds on real traffic, and whether
per-scope recall can clear 0.60. Both are M3 questions. **Nothing gets wired into a
caller until they are answered.**

---

## 9D. M2 result — adapters land, and the disjointness gate catches a live contamination

`catalog/corpora/{massive,clinc150,qa_stream}.py`, `scripts/build_training_corpus.py`,
20 tests. Both corpora fetched and parsed for real, not mocked.

| corpus | size on disk | rows | licence |
|---|---|---|---|
| MASSIVE (tarball, 52 langs) | **38.4 MB** | 16,521 en-US | CC BY-4.0 |
| CLINC150 `data_full.json` | **2.4 MB** | 23,700 | CC BY-3.0 |

Both in the megabyte tier as expected — no SSD, no streaming. `qa_stream` covers the
tens-of-GB tier by filtering an injected row iterator, so a caller streams from HF and
keeps only the few thousand near-miss rows; nothing here materialises a corpus.

**Two mappings asserted in §6.5a were wrong and are corrected in code.** `datetime_query`
("what time is it in Tokyo") and `transport_query` ("when is the next train") are world
questions, not owner records. Mapping them to `schedule:read`/`places:read` would have
taught the head exactly the failure mistral shows in §9C. All 60 intents are now
classified exactly once, enforced by `test_every_upstream_intent_is_classified_exactly_once`,
and only **three** claim a scope: `calendar_query`, `email_query`, `email_querycontact`.

**MASSIVE's positives carry label noise; its negatives do not.** Measured cue-carrying
share of mapped positives: `messages:read` 0.95, `contacts:resolve` 0.75,
**`schedule:read` 0.61** — upstream `calendar_query` conflates owner-calendar reads with
general time and event questions ("check when the show starts"). Positives are therefore
cue-filtered by default; `none` rows never are. This confirms §6.5a's conclusion with a
number: MASSIVE is a negative-class and register asset.

**The gate did its job on the first run.** `build_training_corpus.py` refused to write,
reporting **769 rows colliding with a benchmark catalog**. Restricting the mix to the
generator's `train` split cut it to 270 — and the residue localises entirely to
`role_classify_7.json`, which shipped containing *all* 332 generated cases, train split
included. **The benchmark over-claimed, not the corpus.**

**Consequence, and it must be settled before M3:** any head trained on this corpus and
scored on classify-7 would be reading its own training data — the same defect
AUDIT §2 found in v3, arriving from the opposite direction. The fix is on the benchmark
side: regenerate as **classify-8 holding only `heldout` templates**, and raise generation
volume so both halves stay usable (a 20% heldout share leaves the benchmark thin). Until
that lands, `build_training_corpus.py` **fails closed and writes nothing**, which is the
correct behaviour.

---

## 9E. Lexicons extended to 14 — and §9C's conclusion is RETRACTED

**Lexicon work landed.** 228 realizations across all fourteen live scopes (was 9), every
gate green. Two entity types genuinely missing from the engine definitions were added to
make it possible: `ContactRecord` on `relationships` (which declared `contacts` and
`contact_identifiers` as contributors but had no entity for them) and `MessageThread` on
`memory` (which serves `messages:read` but had no entity for a thread). classify-8
regenerated at **1,071 cases**: ceiling 0.455, macro-F1 0.504, leak 0.177, majority 0.236 —
better on every axis, ratchet clean.

**But the head still fails, and I diagnosed it wrong twice.**

| arm (classify-8) | macro-F1 | negatives abstained |
|---|---|---|
| prototype, no training | **0.387** | 0.797 |
| head, M2 corpus, balanced | 0.293 | **0.906** |
| mistral:7b | **0.436** | 0.078 |

*First hypothesis — distribution shift.* Refuted. Splitting the test set by provenance,
the head scores **0.178 on hand-authored cases and 0.184 on cases from its own
generator**. It fails equally on data it should find easy, so an unseen register is not
the cause.

*Second hypothesis — class imbalance.* Real but partial. Each one-vs-rest problem is 2-4%
positive (96 `health:read` rows against 3,906 others), and an unweighted fit answers
"never" to all of them. `class_weight="balanced"` moved it 0.206 → 0.293. Not enough.

**Retraction.** §9C concluded "the rung-2 approach is validated — a tiny trained head
outperforms the local LLM on both quality and safety". **That is withdrawn.** It rested on
a head trained and tested on splits of the same catalog, where positives run ~35%; on a
real corpus at 20.7% positives the same recipe lands *below the untrained prototype*. The
0.650 measured within-catalog generalisation, which is not the thing that matters. My own
§9C caveat named this risk and still under-weighted it.

**What is actually established:** the prototype is a genuine floor (0.387), the local LLM
scores higher on macro-F1 but will not abstain (0.078, unchanged from classify-7), and a
linear head over MiniLM embeddings trained on this corpus beats neither. Rung 2 is
**unproven**, not disproven — the untried levers are more positives per scope (the
generator is unbounded; 830 across 14 scopes is thin, `relationship_context:read` under
25), a lower twin-to-positive ratio (893 twins against 830 positives may be drowning the
signal), and fine-tuning the encoder rather than fitting a linear head on frozen
embeddings. None of those has been tested, and no wiring decision should rest on rung 2
until one of them moves the number.

---

## 9F. More positives helped, and located the real ceiling

Pushed the generator to 20 renderings per template and capped negative twins at 0.6×
positives (they had outnumbered positives 893:830, and a twin differs from its source by
one word). Corpus: 8,000 rows, **1,858 positives**, per-scope minimum **8 → 41**, median
107.

| head, classify-8 | macro-F1 | negatives abstained | scopes ≥0.60 |
|---|---|---|---|
| before (830 positives) | 0.293 | 0.906 | 3/14 |
| **after (1,858 positives)** | **0.369** | 0.859 | 3/14 |

Real movement (+0.076) — but still under the untrained prototype (0.387) and the local
LLM (0.436).

**Two levers are now measured out.**

*Positive count is near-exhausted.* At 20 renderings the generator yields 13.7 unique
cases per template, not 20 — dedup is already biting, because a template's combinatorial
ceiling is ~21 (7 timeframe groups × 3 surfaces, minus the stative and temporal
restrictions). More renderings will add near-duplicates, not diversity. Growing positives
further needs **more templates**, i.e. more authored realizations, not more sampling.

*Head capacity is not the constraint.* An MLP (256 hidden) scores **0.305 against the
linear head's 0.369** — worse, not better. Extra capacity does not help, which means the
bottleneck is the **frozen MiniLM representation**, not the classifier on top of it.

**Where that leaves rung 2.** A linear probe on frozen sentence embeddings does not
separate these fourteen scopes well enough, and no amount of head engineering or sampling
fixes that. The remaining honest options are **rung 3** — fine-tune the encoder so the
representation itself learns the taxonomy — or **more authored realizations** to widen
template diversity. Both are real work; neither is a tweak. Rung 2 as specified (a head on
frozen embeddings) is now **measured and insufficient**, which is a firmer result than the
"unproven" of §9E.

---

## 9G. Rung 3 trained — best arm on macro-F1, still short on recall

DistilBERT fine-tuned on the M2 corpus (8,309 rows), MPS, 3 epochs. Evaluated on
classify-8, same held-out set as every other arm.

| arm | macro-F1 | negatives abstained | scopes >=0.60 |
|---|---|---|---|
| prototype, untrained | 0.387 | 0.797 | 1/14 |
| linear head on frozen MiniLM | 0.369 | 0.859 | 3/14 |
| mistral:7b | 0.436 | **0.078** | 8/14 |
| **DistilBERT (rung 3)** | **0.446** | **0.937** | 1/14 |

**The first arm to beat everything on macro-F1, including the LLM, with a 12x better
abstention rate.** It passes two of three §7 clauses. It fails the third: 13 of 14 scopes
below 0.60 recall. The head is precise and insensitive — it abstains well and
discriminates poorly.

**§9F is confirmed and its conclusion narrowed.** The frozen representation WAS the
ceiling: fine-tuning moved macro-F1 0.369 -> 0.446 where an MLP on frozen embeddings had
moved it *backwards*. Rung 3 is the right lever. It is just not sufficient on this corpus.

**Three bugs found by running it, all mine, all the same family.**

1. *No `pos_weight`.* Each label is 0.5-4% positive (`relationship_context:read` is 41
   rows in 8,309), so unweighted BCE minimised by predicting nothing: training loss 0.006,
   macro-F1 0.280, abstention 0.968, 14/14 scopes at zero recall. §9E had already
   diagnosed exactly this for the linear head and I did not carry it across. Fixing it:
   0.280 -> 0.402.
2. *Unshuffled validation holdout.* `rows[-N:]` on a corpus written in source order is
   pure CLINC — all `none`, zero positives, macro-F1 NaN at every threshold. The sweep
   silently learned nothing and kept the arbitrary default.
3. *Row-level validation split — the template leak, third occurrence.* Validation read
   0.986 against 0.368 eval. The corpus did not carry `template_id` at all, so a grouped
   holdout was not expressible. Fixed at the source: `CorpusRow.template_id`, populated
   by all three generators, holdout grouped by template. Honest threshold selection
   followed: 0.446.

That leak has now been fixed in three separate places — catalog splits (§6.4 rule 2),
the difficulty ratchet (grouped CV, +0.153), and here. Each time it presented as a
different problem. **Any new split anywhere in this pipeline must be grouped by template
before its number is believed.**

**What is still binding.** Validation sits at 0.985 against 0.446 eval even with grouping,
because the validation slice is dominated by MASSIVE/CLINC negatives that split per-row
and are easy. The remaining gap is distribution, not leakage: the corpus's positives are
template-generated and the benchmark's are half hand-authored. The recall clause is where
that shows up.

**Next levers, in order:** a validation slice stratified to match the benchmark's band mix
(so threshold selection is informative rather than saturated), then more distinct
templates per scope — not more renderings, which G3 measured as exhausted at ~13.7 unique
per template. Not more epochs: training loss is already 0.026.

**Nothing is installed.** The artifact is in a scratchpad, loads correctly through the
seam, and answers sensibly by hand ("how did I sleep this week" -> `health:read` at 1.00;
"what is the capital of Mongolia" -> abstain at 0.00). Promoting it needs the recall
clause, and a 265 MB RSS decision that shadow-mode traffic should inform.

---

## 9. Risks

| risk | mitigation |
|---|---|
| **Self-training circularity** — head trains on LLM labels, learns the LLM's errors, and the eval agrees because it also came from the LLM | frozen human-audited test slice; champion/challenger at M5; never let a model label its own test set |
| **Synthetic overfit** — head is excellent on templates, fails on real text | synthetic ≤ 40% of mix; real-traffic-only reported test set |
| **Silent scope collapse** — macro-F1 fine, one scope dead | per-scope recall floor 0.60 in the gate |
| **Taxonomy drift** — registry gains a scope, head still emits 14 | versioned compatibility check (§6A.2), not bare set-equality; new scopes ship LLM-only until the next base-model cycle |
| **Silent mis-routing from a renamed scope** — positional head index now points at a different meaning | never reuse or redefine a `scope_id` (§6A.2 rule 1); scope-registry version pinned in the model registry row |
| **Inseparable scopes blamed on the model** — permanent confusion cell read as "the classifier is bad" | §6A.3 — treat the confusion matrix as evidence about the *taxonomy*; decide (A)/(B) on measurement, not intuition |
| **RAM/RSS regression on the node** | rungs 1–2 add zero resident models; budget before rung 3 |
| **Breaking the published "No training on your data" claim** | shipped base model is synthetic + public only, with an auditable training manifest; node-local adaptation never leaves the node; telemetry carries counts, never text or embeddings (§6) |
| **Threshold rot** — data drifts, `tau` stale | thresholds versioned in the registry row; re-calibrate at every M5 cycle |

---

## 10. Open decisions

1. **Where does free-text scope selection actually get consumed?** §1.3 shows the live query path
   takes `scope_id` as an input. Which caller should start predicting instead of requiring it —
   home chat, MCP `query_scope`, or a new entry point? This determines M1's integration surface
   and is the one thing blocking M1 scoping.
2. ~~**On-device vs. scrubbed-derivative training.**~~ **CLOSED 2026-08-13 — on-device only.**
   The scrubbed-derivative branch is removed: it breaks the live "No training on your data" claim
   (`securityContent.ts:54`) just as squarely as raw pooling does. See §6. The shipped base model
   is synthetic + public only; node-local adaptation never leaves the node.
3. **Does emo_27 move out of the classify catalog now?** §1.2 argues yes — it is already a local
   model, so scoring LLM families on it measures nothing the product buys. Splitting it also
   removes 30.4% of the trivial mass the audit flagged in §3. Cheap, and it improves the catalog
   immediately.
4. **Escalation budget.** What escalation rate is acceptable in steady state? This sets `tau_high`
   and therefore the entire cost/accuracy trade. Needs a product answer, not a technical one.
5. ~~**Which public corpora clear licence review?**~~ **CLOSED 2026-08-13.** MASSIVE (CC BY-4.0)
   and CLINC150 (CC BY-3.0) clear; SGD (CC BY-SA-4.0) is dropped pending counsel. GoEmotions
   (Apache-2.0), already shipped in P3, is clear. See §6.5a.
6. **Does the opt-in *authored* contribution flow (§6.5f) exist at all?** It is claim-compliant but
   it costs UX surface and invites exactly the confusion the boundary is meant to prevent. Default
   to no unless (a)–(c) come up short.
7. **(A) accept confusion cells, or (B) split routing from authorization?** §6A.4. Recommendation
   is (A) now with the indirection built at M1 so (B) stays cheap. **Decide with the M3 confusion
   matrix, not before** — but decide consciously, because drifting into (A) by default is how the
   permission taxonomy quietly starts getting shaped by model convenience.
