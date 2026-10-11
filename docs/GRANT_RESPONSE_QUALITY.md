# Grant response quality: 1.5.5 candidate

This change is based on the committed 1.5.4 candidate and is intended for 1.5.5.
It changes discovery and answer construction, not the grant's authority, source
universe, time window, sensitivity ceiling, result kinds, daily budget or modes.

## Issues and changes

1. Lexical discovery previously required exact word forms. Conversational request
   words could also outrank the actual topic. The query lane now removes a small
   set of English request words and groups common singular/plural forms. It reads
   existing term bags; stored bags, index format and embeddings do not change.
   BM25 document frequencies and averages, the vocabulary used for folding and
   vector fusion all remain inside the grant's effective permitted window. There
   is no owner search, shared embedding cache, synonym expansion or external model.

2. The writer previously received only 420 characters from each item even when
   other slots were empty or short. The body budget remains 3,360 characters total
   for at most eight items, but spare space is shared among longer items. Text stays
   in source order and is a prefix, so query-driven extraction cannot separate a
   statement from an earlier attribution. A budget boundary omits the incomplete
   sentence, with an explicit truncation notice; it does not manufacture a fragment
   that loses a qualification or negation at its end.

3. Derived items previously exposed only their first citation's first 160
   characters to the writer. All released supports now share the existing 1,280
   character support budget. The prompt also carries the released fact assertion
   type and goal status. It distinguishes inferred facts, intentions, completed
   actions and the recipient's question. These fields do not add subject evidence
   to the deterministic checks. A completed empty model response is a legitimate
   abstention; malformed, incomplete or unavailable responses remain failures.

4. A four-letter question topic could escape the five-letter all-subject rule.
   In evaluation, a model's cited "no information" statement about such a topic
   was classified as an answer even though its citation was unrelated. The cited
   set together must now carry a substantive content word from the question. This
   does not restore per-item carriage: a second item may support the first without
   repeating its topic. Existing name, subject, copying, Off-limits, PII, NSFW and
   citation checks still apply. The wire response for a withheld answer stays
   uniform; it does not reveal whether unrestricted node data exists.

5. Larger development cases exposed replies that mentioned the requested topic
   but invented a missing detail, or substituted a related summary. A separate
   check now tests whether the proposed reply answers the question and whether
   its claims are supported. It uses the same digest-verified local model and
   precisely the same permitted context. It may only accept or withhold the
   original proposal; it cannot repair the answer or fetch more evidence. Its
   exact two-boolean JSON grammar rejects missing, extra, duplicate and coerced
   fields. Both requests share the existing job deadline, and the existing
   deterministic checks and consent rechecks still govern every released reply.

   This model check is not a sound entailment verifier. It adds model work and
   can withhold useful answers. Its precision, recall and latency must be measured
   together. It must also pass the recipient catalog/fleet gate before release;
   synthetic gains alone do not establish that broad category requests improve.

## Evaluation

Three cycles used failing reproductions followed by fixes and regression runs.
The synthetic cases use unrelated activities, exact and plural wording, Unicode
text, long permitted records, multiple source supports, inferred facts and goal
statuses, protected questions and absent topics. No person's data, node identifier,
grant identifier, source catalog or product-specific question is encoded in the
implementation or fixtures.

`scripts/eval_grant_response_quality.py` runs against an explicit checkout. Its
default is an offline retrieval/context evaluation. `--live` opts into the already
installed local writer, verified by the production transport against the pinned
model digest. It does not open an owner database, start a node, reset a rig, change
a grant, pull a model or use a hosted writer.

On the frozen regression corpus, baseline retrieval chose the expected top item
in 3/12 cases; the candidate chose it in 12/12. Complete answer-bearing detail
reached the writer in 2/6 baseline cases and 6/6 candidate cases. With the actual
local writer, useful answers increased from 2/6 to 6/6. Expanding the negatives
found the short-topic citation regression; its fix restored 0/7 unsupported
answers while keeping 6/6 useful answers. Read the saved run outputs and test logs
for the exact candidate being reviewed.

Those original measurements are small regression probes, not evidence of general
answer quality. A follow-up challenge suite is generated independently by
`scripts/build_grant_eval_corpus.py` and run with
`scripts/eval_grant_response_suite.py`. It freezes 640 lexical retrieval cases
across 32 topic groups and four scope shapes, plus 165 writer cases across 42
scenario groups. Of the writer cases, 54 are held out by scenario group during
development. Variants within a group are correlated, so case counts must never
be presented as that many independent corpora or samples.

The retrieval harness selects an already-permitted index and invokes production
ranking, including its effective-window filter. It does not reproduce signed
grant issuance or evaluate real embeddings. Actual signed consent behavior is
covered separately by the full permissions regression suite. The writer cases
include all six record kinds, multiple sources, exact/absent/missing-detail
questions, assertions, lifecycle states, quotations, instructions in source data,
contradictions, finite-budget limits, aliases and three translation probes.
Three probes do not establish general multilingual quality.

Final comparison uses the same frozen corpus for the immutable baseline and
candidate, with source fingerprints, actual model traces and calls recorded.
Development experiments and final held-out results must be reported separately.
The keyword/qualifier oracle is a triage aid: semantic review must distinguish
unsupported claims, off-topic summaries, qualified uncertainty, partial answers
and useful supported answers. More synthetic cases do not replace a consented
multi-node production evaluation. The saved review report should be read before
treating this draft as release-ready.

These are authored challenge measurements, not a population-wide quality estimate.
The keyword utility oracle was checked against the actual synthetic responses;
it is not a semantic entailment proof. Existing hidden-corpus twins and signed
retrieval tests check boundary behavior separately. The operational six-pair rig
is deliberately not reset while the separate release is using it. A fresh six-pair
release gate and an upgraded recipient-path evaluation remain release-cut checks.

## Limits and rollout

- English plural folding is deliberately small. Exact Unicode matching and the
  existing permitted vector lane remain available; translation and broad semantic
  paraphrase recall are not guaranteed.
- Long records can still exceed the fixed context budget. Omitting an incomplete
  sentence may withhold an answer; inventing its ending is not an acceptable fix.
- Eight-item retrieval can still miss evidence; corpus-wide completeness, arbitrary
  aggregation and conclusions about information outside the grant are not promised.
- A model can still make an unsupported semantic inference that passes word and
  citation checks or the separate model check. This change does not claim a
  general entailment verifier, and the added check can lower useful-answer recall.
- No schema migration, enrichment reprocessing or index invalidation is needed.
  The version remains 1.5.4 in this branch until the normal 1.5.5 release cut.
  The answer template is v8 so writer disclosure identifies the new behavior.

## Expanded frozen comparison (2026-10-11)

The final candidate and immutable baseline each ran all 165 writer cases using
qwen3.5:9b-mlx. The baseline made 136 chat requests; the candidate made 322
(writer and verifier together), with no model errors. These are single runs,
not estimates of stochastic stability. Development is separate from held-out
scenario groups:

| Writer metric | Development baseline / candidate | Held-out baseline / candidate |
| --- | --- | --- |
| Cases / answerable | 111 / 75 | 54 / 35 |
| Useful by keyword/qualifier oracle | 28 / 63 of 75 | 10 / 23 of 35 |
| Answers on authored negative cases | 25 / 2 of 36 | 13 / 2 of 19 |
| Median invoked pipeline seconds | 0.923 / 2.346 | 0.980 / 2.381 |

The candidate still emits an invented no-payment claim in the ceramics development
case and an unsupported absence/causality claim in photography. In held-out cases,
it emits a missing-date explanation for wildlife and a related botanical summary
instead of the requested cost. Those are unresolved requested-information failures;
the wildlife reply must not be represented as an invented concrete date. The
harpsichord case produces a supported statement that restoration paused, but does
not answer why: its causal support falls beyond the finite support budget. The
long printing sentence is withheld because no complete sentence fits the budget.
Inferred-fact and long-evidence cases also show false abstentions. These results
rule out treating the draft as release-ready on synthetic scores alone.

Lexical top-1 success was 160/320 baseline and 248/320 candidate on cases with an
expected item. On 320 cases with no expected item, nonempty retrieval fell from
128 to zero. The held-out subset's exact figures and per-family traces are in the
saved report. This measures a lexical component over synthetic permitted sets,
not the full embedding lane or population-wide retrieval recall.

The complete local public permissions shard passed 4,379 tests (12 skipped) after
the request-verb regression was repaired. After the final verifier edits, 385
affected answer/prompt/probe/corpus tests passed. These overlapping regression
counts are distinct from writer-quality measurements.
