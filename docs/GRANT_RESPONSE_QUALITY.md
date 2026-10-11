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

These are small regression measurements, not a population-wide quality estimate.
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
  citation checks. This change does not claim a general entailment verifier.
- No schema migration, enrichment reprocessing or index invalidation is needed.
  The version remains 1.5.4 in this branch until the normal 1.5.5 release cut.
  The answer template is v8 so writer disclosure identifies the new behavior.
