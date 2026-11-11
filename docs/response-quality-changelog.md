# Response-quality change log

Run the full [paired regression gate](response-quality-regression.md) for every
response-quality change. It appends per-family before/after results here and saves
individual test outcomes in `response-quality-runs/`.

Record changes to retrieval, query interpretation, ranking, prompts, evidence filtering,
fallbacks, caching, and model configuration here. This is a cross-component record:
include web and control-plane changes even when the entry lives in the node repo.
Do not include private message bodies, identifiers, credentials, or raw personal data.

## How to use this log

Add an entry with the implementation, then append dated verification or regression
updates. Preserve the original hypothesis and failed checks. Distinguish implemented,
locally loaded, live verified, and released; a passing example is not a general quality
improvement. Record the actual model and dataset selection when testing live.

For each change, test the original failure, paraphrases, contrasting questions that
must retain their behavior, and adjacent query families. Compare evidence quality
separately from answer quality: correct dataset, source type, identity, event dates,
coverage, citations, uncertainty, and latency. Record losses as well as gains.

Use a fixed, privacy-safe fixture set for before/after comparisons and keep the
model/configuration constant when isolating a code change. Live data can change
during a run, so record that separately. Do not infer a regression from different
data or models, and do not call a single-example improvement a release gate. Link
the commit/PR after one exists; local uncommitted entries must say so.

## Methods currently involved

| Method | Purpose | Regression risk to check |
| --- | --- | --- |
| Intent/phrase detection | Choose date, participant, or general retrieval behavior | Equivalent wording takes different paths; overbroad matches route unrelated questions |
| Canonical lookups and dataset/source constraints | Ground identity and retrieve the requested records | Missing contact metadata produces unresolved results; wrong scope can hide or mix evidence |
| Semantic retrieval, fusion, recency and result caps | Find and prioritize relevant evidence | Old but similar records crowd out relevant events; weights trade relevance against recency |
| Prompt and citation guidance | Tell synthesis how to use evidence and express limits | More instructions can compete; unsupported negative claims can survive |
| Deterministic fallback/receipt handling | Preserve explicit outcomes when synthesis cannot answer | Narrow fallbacks may suppress useful synthesis if triggered too broadly |
| Cache policy | Reuse work without stale answers | Cached failures survive fixes/imports; bypassing cache adds latency |
| Regression fixtures and live checks | Verify behavior at each layer | Narrow fixtures do not establish broad quality or live deployment correctness |

These are application changes, not model training. No model change was made in the
entries below. Whether the configured model itself contributes requires controlled
comparisons; it is not established by these incidents.

## RQ-2026-10-26-01 — Latest text exchange used mentions and AI conversations

- **Recorded:** 2026-10-26. **Status:** implemented in local node/web checkouts;
  isolated installed-node patch loaded. Live selected-dataset/text-only lookup
  verified (see dated updates); no release claimed.
- **Failure:** A request for the latest text exchange with a named person returned
  an old message mentioning that person, mixed with AI conversation sources.
- **Root cause:** Semantic relevance did not establish participant identity or
  chronological completeness. The home-chat dataset selection also needed to be
  carried through explicitly rather than falling back to the node default.
- **Method:** Narrow deterministic participant lookup, dataset/source restrictions,
  event-time ordering, explicit result receipts, citation/fallback handling, and
  cache freshness. No contact identity inferred or written.
- **Exact behavior:** Carry selected dataset from home chat to retrieval. Resolve
  exact contact name or identifier within that dataset; join the conversation roster
  on dataset, source and conversation. Sort dated messages by parsed event time
  before limiting results. Exclude AI sources. Return identity_unresolved,
  identity_ambiguous, dataset_required or no_dated_messages when applicable.
  Preserve unresolved identity through web insufficient-evidence exits and ask for
  an identifier instead of citing an unrelated mention. Group conversations remain
  possible and must not be described as proven direct exchanges.
- **Files:** node `topos/query/latest_exchange.py`, `retrieval.py`, `types.py`,
  `pipeline.py`, `game_layer.py`, `topos/core/handlers/query.py`,
  `topos/protocol/query_field_contract.json`; web
  `next/features/home/runHomeChatTurn.ts`, `next/lib/mcp/homeChatAgent.ts`,
  `next/lib/mcp/conversationCitations.ts`.
- **Evidence:** Selected the selected personal dataset contains contacts without display names. A named
  contact exists in another dataset; using that identity would be incorrect.
  Read-only installed retrieval returned unresolved identity, zero summaries and no
  AI evidence for the selected dataset, with zero database changes.
- **Checks:** Node retrieval/cache/source URL selection: 19 passed; query-field and
  retrieval-text contracts: 45 passed, 1 skipped. Web citation/agent/tool election:
  121 passed; source URLs: 5 passed; engine scope: 9 passed. Cases include mentions
  versus participants, timezone ordering, ambiguous names, exact identifiers,
  missing identity, dataset isolation and unresolved web output.
- **Limits:** These are targeted checks, not a broad quality benchmark. Live UI
  regeneration initially hit node-connectivity/session failures; not counted as a
  successful end-to-end check. Full web typecheck has generated settings/lab route
  errors. Repository-map check references an absent minimizer.py.
- **Rollback:** Revert this entry's hunks across node/web together; preserve other
  uncommitted work. Installed-runtime pre-patch files are locally backed up under
  `/tmp/topos-latest-exchange-backup-1793039162` (temporary, not release artifacts).
  No migration or personal-data modification was part of this fix.
- **Live update, 2026-10-26 14:10 America/Chicago:** The real home-chat tool trace
  confirmed the selected the selected personal dataset dataset and a resolved participant lookup,
  latest record `[private record ID omitted]`, a verified event time. During this session the
  selected dataset's named-contact count changed from zero to 135, including the
  requested person. This work did not write those contacts. The previously expected
  unresolved result is therefore no longer the correct live outcome; it remains
  tested against missing-identity fixtures. Do not erase the earlier observation.
  The live run also exposed an unnecessary AI-conversation enrichment call; see
  RQ-2026-10-26-03. Dataset/identity lookup verified; exclusion was not yet green.

## RQ-2026-10-26-02 — Everyday yesterday recap missed the strict date path

- **Recorded:** 2026-10-26. **Status:** implemented and loaded locally; live answer
  verification pending.
- **Failure:** “What was I doing yesterday?” returned one incoming text and claimed
  no browsing or work activity, despite stored activity for that day.
- **Root cause:** The date parser correctly selected October 25 in America/Chicago.
  The event-recall phrase detector recognized “been up to” and “did I do” but not
  “was I doing.” Without the strict event window, old semantic hits filled capped
  result sets. Later filtering removed them, leaving too little evidence. Synthesis
  converted inadequate retrieval into unsupported absence claims.
- **Method:** Extend intent recognition and bypass stale recap cache results.
  This is a targeted phrase-detector repair, not a complete intent redesign.
- **Exact change:** Add “was/am I doing/up to” and “have I been doing” to
  `topos/query/planner.py:is_dated_activity_query`; use that detector alongside
  latest-text detection to make owner memory hits run a live query in `pipeline.py`.
- **Evidence:** Read-only counts in the selected dataset for the local-day interval
  found 21 text rows, 209 browser-event/visit rows, and 512 AI/coding-session message
  rows; the browser categories may overlap. No calendar rows were found. Saved
  query artifacts contained the correct requested window but many out-of-window
  semantic results. This establishes a retrieval defect, not model degradation.
- **Checks:** New paraphrase, strict-window forwarding and cache-refresh tests in
  `tests/query/test_activity_recap_recency.py` and
  `tests/query/test_query_multi_turn_cache.py`. Targeted date tests passed; cache
  suite: 8 passed. The wider activity-recap file had 17 passes and one failure
  (`test_empty_window_is_no_match_when_source_history_exists`: engine_failed versus
  no_match); baseline attribution still needs verification. Do not call this suite green.
- **Contrasting behavior:** A question about what another person said about an
  interview must not become an owner activity recap. Identity lookup, latest-text
  lookup, aggregate questions and undated questions need continuing regression coverage.
- **Risks:** More live queries increase latency. Phrase matching remains brittle;
  additional wording can still miss the path. No broad ranking, prompt, model, or
  empty-evidence semantics change is claimed here.
- **Live configuration:** Ollama `smtek/Qwen3.8-27B:IQ2_M`, selected the selected personal dataset.
- **Rollback:** Revert the detector additions and recap-only cache bypass together.
  Installed planner/pipeline originals are under
  `/tmp/topos-yesterday-retrieval-backup-1793041393`. Keep the earlier latest-text
  changes. Runtime patches are temporary and must be carried into a normal release.
- **Verification update, 2026-10-26:** Combined latest-exchange, query-cache and
  activity-recap suite: 34 passed, 1 failed. Re-running the failing fixture with the
  HEAD version of the phrase detector still fails identically. The detector change
  did not introduce that failure; the broader suite remains non-green.
- **Live update, 2026-10-26 14:19 America/Chicago:** Regenerated home-chat response
  now includes work/research, browsing and social activity. Activity tool trace
  explicitly reports October 25 00:00–23:59:59 at -05:00 and in-window browser
  evidence; the Sources panel contains 21 records. The earlier false no-browsing/
  no-work statements are gone. Elapsed UI time: 6m 27s, versus 2m 24s in the original
  screenshot; synthesis prompt grew from 28,061 to 64,833 characters. This is not a
  controlled latency benchmark. Inline citations remain absent and the answer's
  detailed time-of-day assignments have not been individually audited. Retrieval
  improvement verified; overall answer quality and latency are not declared solved.

## RQ-2026-10-26-03 — Summary fallback queried AI chats after a text lookup

- **Recorded:** 2026-10-26. **Status:** implemented locally; clean live recheck passed
  for dataset/source routing (see below); no release claimed.
- **Failure:** Live the requested contact regeneration returned a correct participant receipt,
  then issued a second query to `ai_conversations:read` despite the text-only ask.
- **Root cause:** `needsSummaryEnrichment` recognizes “summary” as a topic question.
  When the outer response omits access_mode, it can treat a completed lookup as
  needing extra summary evidence. It did not recognize the explicit lookup receipt.
- **Method/change:** In web `next/lib/mcp/homeChatOrchestration.ts`, a
  `latest_exchange` receipt suppresses AI summary enrichment. Both resolved and
  unresolved participant outcomes are final for this fallback. No broad change to
  topic enrichment or source permissions.
- **Checks:** Added resolved/unresolved/ambiguous receipt cases with omitted outer
  mode, plus a contrasting topic-inference question that still needs enrichment.
  Orchestration, tool-election and citation suites: 70 passed.
- **Risk:** Future receipt statuses must continue to communicate what is unresolved;
  a fallback must not manufacture identity evidence. This guard addresses this
  enrichment path; it is not a claim that every routing path honors exclusions.
- **Rollback:** Revert the receipt check/import and its tests. Retain the participant
  lookup and selected-dataset propagation from RQ-2026-10-26-01.
- **Hypothesis correction, 2026-10-26 14:14 America/Chicago:** An agent integration
  test that asserted the actual queried scopes still failed after the fallback
  guard. The initial explanation above was insufficient: initial keyword routing
  also matched the negated phrase “not AI conversations” and queued that scope.
  Added `excludesAiConversations` in `scopeRoutingRules.ts`, applied after routing
  in `scopeQueryRouter.ts` and before summary enrichment. It handles explicit
  not/no/without/exclude/excluding AI chats/conversations. This is a bounded
  exclusion parser, not general natural-language negation understanding.
- **Updated checks:** 184 passed across scope routing, query router, orchestration,
  tool election and citations. Added exclusion paraphrases, positive mixed-source
  requests, “not only AI conversations,” and an actual-dispatch assertion that
  the unresolved text lookup calls only messages:read with the selected dataset.
  The earlier 70-test pass did not establish this behavior.
- **Updated rollback scope:** Include the exclusion helper, router filter and new
  router/dispatch tests alongside the fallback guard. All changes remain local
  and uncommitted; no release is claimed.
- **Live update, 2026-10-26 14:20 America/Chicago:** A clean home chat queried only
  messages:read (2.8s retrieval). Its receipt names the selected the selected personal dataset dataset,
  resolved identity, eight iMessage records and the correct latest stored record.
  The Sources panel contains Messages only, with no AI chats. The final answer
  summarizes the latest stored exchange and names its record. Elapsed UI time: 3m 18s
  (overlapped a recap generation on the same runtime; not a standalone benchmark).
  Missing identity remains covered by fixtures, but cannot honestly be asserted
  for the now-populated live contact. Inline source appears as plain record text
  rather than a clickable citation: still open. Proof screenshots are local at
  a local-only screenshot and a second local-only screenshot;
  they contain private data and are not checked into this repository.
- **Final static check:** TypeScript reports the same 12 generated `.next` references
  to removed settings/permissions/lab routes; no diagnostics in changed files.

## Entry template

### RQ-YYYY-MM-DD-NN — Observable symptom

- Recorded / implemented / loaded / live-verified / released timestamps (with zone):
- Status and linked commit/PR when available:
- Failing question family and expected behavior:
- Observed root cause; hypothesis or proven:
- Method category and exact files/behavior changed:
- Dataset/source/model/configuration and cache conditions:
- Before/after evidence and answer assessment:
- Original case, paraphrases, negative controls, adjacent-family results:
- Latency and known regressions / unresolved checks:
- Rollback scope and interactions with other changes:
- Follow-up observations (append dates; preserve previous findings):

### Regression gate — RQ-2026-10-26-04 — before

- Recorded (UTC): 2026-10-26T21:05:19.773395+00:00
- Note: Initial broad gate on the current dirty checkouts; infrastructure baseline, not a pre-fix baseline for the earlier retrieval changes.
- Report: [machine-readable results](response-quality-runs/20261026T210519773395Z-RQ-2026-10-26-04-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 25 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 51 pass / 1 fail / 0 skip | pending |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 103 pass / 0 fail / 0 skip | pending |

### Regression gate — RQ-2026-10-26-04 — after

- Recorded (UTC): 2026-10-26T21:07:13.103299+00:00
- Note: Gate validation with unchanged application behavior: verify stable cross-family results and that the existing failure remains blocking. This does not measure improvement from earlier fixes.
- Report: [machine-readable results](response-quality-runs/20261026T210713103299Z-RQ-2026-10-26-04-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 25 pass / 0 fail / 0 skip | 25 pass / 0 fail / 0 skip |
| dates_and_recency | 51 pass / 1 fail / 0 skip | 51 pass / 1 fail / 0 skip |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | 140 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 103 pass / 0 fail / 0 skip | 103 pass / 0 fail / 0 skip |

- Fixed: 0; regressed: 0; missing: 0; added: 0; remaining non-passes: 1.

### Regression gate — RQ-2026-10-26-05 — before

- Recorded (UTC): 2026-10-26T21:17:28.022987+00:00
- Note: Current-main integration baseline before investigating the empty-window failure; includes ported retrieval changes and the coordinator’s stable web snapshot.
- Report: [machine-readable results](response-quality-runs/20261026T211728022987Z-RQ-2026-10-26-05-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 25 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 51 pass / 1 fail / 0 skip | pending |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 103 pass / 0 fail / 0 skip | pending |

## RQ-2026-10-26-05 — Current-main integration and deterministic empty-search fixtures

- Retrieval and regression tooling were ported onto current schema90 main, preserving
  current caller policy and concurrent viewer changes. No runtime restart or release
  was performed by this task.
- The initial integrated baseline overlapped another task's privacy-fixture edits.
  Its results are retained but must not be used as an attributable before/after pair.
- Investigation showed that the empty-window fixture mocked summary retrieval but
  let semantic retrieval initialize a real model. Model-loader failure correctly
  produced engine_failed. A deterministic fake vector service now returns either
  a healthy empty result (no_match) or an injected exception (engine_failed).
  Production error classification was not weakened or changed.
- The runner now sets HF_HUB_OFFLINE and TRANSFORMERS_OFFLINE and rejects catalog/test
  files changed during execution. This repairs an earlier inaccurate offline claim:
  previous runs could attempt Hugging Face metadata requests on an uncached runtime.
- Public copies use synthetic contacts and identifiers. Private local live details
  are omitted; these redactions do not turn mutable live data into a frozen benchmark.
- Fixture inventory and runtime policy changed. Comparisons to the old baseline must
  report NOT_COMPARABLE, followed by a fresh frozen before/after verification pair.

### Regression gate — RQ-2026-10-26-05 — after

- Recorded (UTC): 2026-10-26T21:21:06.950312+00:00
- Note: Fixture and offline-policy correction: expected NOT_COMPARABLE against overlapping old baseline; do not count this as a measured product improvement.
- Report: [machine-readable results](response-quality-runs/20261026T212106950312Z-RQ-2026-10-26-05-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **NOT_COMPARABLE**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 25 pass / 0 fail / 0 skip | 25 pass / 0 fail / 0 skip |
| dates_and_recency | 51 pass / 1 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | 140 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 103 pass / 0 fail / 0 skip | 103 pass / 0 fail / 0 skip |

- Fixed: 0; regressed: 0; missing: 1; added: 2; remaining non-passes: 0.
- reasons: `fixture_hash changed`
- reasons: `toolchain changed`
- reasons: `runtime_policy changed`
- reasons: `test inventory changed`
- missing: `node::tests/query/test_activity_recap_recency.py::tests.query.test_activity_recap_recency::test_empty_window_is_no_match_when_source_history_exists`

### Regression gate — RQ-2026-10-26-06 — before

- Recorded (UTC): 2026-10-26T21:23:32.346443+00:00
- Note: Frozen integrated schema90 node/web verification after fixture and offline-policy repair; establishes a new comparable baseline, not a pre-fix product benchmark.
- Report: [machine-readable results](response-quality-runs/20261026T212332346443Z-RQ-2026-10-26-06-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 25 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 103 pass / 0 fail / 0 skip | pending |

### Regression gate — RQ-2026-10-26-06 — after

- Recorded (UTC): 2026-10-26T21:23:58.145552+00:00
- Note: Frozen integrated repeat: all question families checked against the same fixture inventory and runtime policy. This confirms the gate and integrated contracts, not pre-fix live response improvement.
- Report: [machine-readable results](response-quality-runs/20261026T212358145552Z-RQ-2026-10-26-06-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 25 pass / 0 fail / 0 skip | 25 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | 140 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 103 pass / 0 fail / 0 skip | 103 pass / 0 fail / 0 skip |

- Fixed: 0; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.

### Regression gate — RQ-2026-10-26-07 — before

- Recorded (UTC): 2026-10-26T21:28:08.514273+00:00
- Note: Final 1.5.28 candidate with remaining cache fixture anonymized; repeated contract validation, not a pre-fix product-quality benchmark.
- Report: [machine-readable results](response-quality-runs/20261026T212808514273Z-RQ-2026-10-26-07-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 25 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 103 pass / 0 fail / 0 skip | pending |

### Regression gate — RQ-2026-10-26-07 — after

- Recorded (UTC): 2026-10-26T21:44:04.651611+00:00
- Note: Final integrated 1.5.28 contract validation after anonymizing the remaining cache fixture. No pre-fix model quality baseline was captured.
- Report: [machine-readable results](response-quality-runs/20261026T214404651611Z-RQ-2026-10-26-07-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 25 pass / 0 fail / 0 skip | 25 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | 140 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 103 pass / 0 fail / 0 skip | 103 pass / 0 fail / 0 skip |

- Fixed: 0; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.

### Regression gate — RQ-2026-11-01-verification-deadline — before

- Recorded (UTC): 2026-11-01T19:05:06.484463+00:00
- Note: Published 1.5.36 production behavior restored after exploratory focused checks; new budget helper is present but not wired into production, with frozen regression fixtures. Capture pre-fix deadline and cancellation failures without live calls.
- Report: [machine-readable results](response-quality-runs/20261101T190506484463Z-RQ-2026-11-01-verification-deadline-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 103 pass / 2 fail / 0 skip | pending |

### Regression gate — RQ-2026-11-01-verification-deadline — after

- Recorded (UTC): 2026-11-01T19:05:56.143355+00:00
- Note: One monotonic verification budget bounds local and remote compute, cancels provider transport on expiry/disconnect, prevents late heuristic success, and retains honest Engine worker ownership. Fake socket and admission regressions passed separately; no live capacity claim.
- Report: [machine-readable results](response-quality-runs/20261101T190556143355Z-RQ-2026-11-01-verification-deadline-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | 145 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 103 pass / 2 fail / 0 skip | 105 pass / 0 fail / 0 skip |

- Fixed: 2; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.
- fixes: `node::tests/query/test_verification_deadline.py::tests.query.test_verification_deadline::test_cancelled_handler_signals_its_worker`
- fixes: `node::tests/query/test_verification_deadline.py::tests.query.test_verification_deadline::test_expiry_is_unavailable_not_a_heuristic_success_and_worker_finishes`

### Regression gate — RQ-2026-11-01-sharing-recovery — before

- Recorded (UTC): 2026-11-01T18:57:01.154126+00:00
- Note: Frozen sharing deadline failures before implementation; no live generation authorized
- Report: [machine-readable results](response-quality-runs/20261101T185701154126Z-RQ-2026-11-01-sharing-recovery-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 103 pass / 6 fail / 0 skip | pending |

### Regression gate — RQ-2026-11-01-sharing-recovery — before

- Recorded (UTC): 2026-11-01T18:58:00.205979+00:00
- Note: Corrected baseline: Python 3.12 and explicit synthetic transport judge marker; earlier report retained, not comparable
- Report: [machine-readable results](response-quality-runs/20261101T185800205979Z-RQ-2026-11-01-sharing-recovery-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 103 pass / 6 fail / 0 skip | pending |

### Regression gate — RQ-2026-11-01-sharing-recovery — after

- Recorded (UTC): 2026-11-01T18:58:37.294168+00:00
- Note: Sharing verification and response share a cancellable wall-clock cap; provider judge errors stop subsequent calls; no live-model claim
- Report: [machine-readable results](response-quality-runs/20261101T185837294168Z-RQ-2026-11-01-sharing-recovery-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | 140 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 103 pass / 6 fail / 0 skip | 109 pass / 0 fail / 0 skip |

- Fixed: 6; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_deadline_cancels_inflight_closes_and_next_call_recovers[post-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_deadline_cancels_inflight_closes_and_next_call_recovers[post-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_deadline_cancels_inflight_closes_and_next_call_recovers[verify-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_deadline_cancels_inflight_closes_and_next_call_recovers[verify-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_expired_call_never_opens_transport[False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_expired_call_never_opens_transport[True]`

### RQ-2026-11-01-sharing-recovery — bounded grant-backed writer/judge lifecycle

Scope: Clark's grant-backed owner-compute `with_sources` answer path on published
1.5.36 (`4e2ac2167b0e18e37c5dc70a082525eaeb094c37`), not generic developer calls.
No grant, node, model residency, remote compute routing, database boundary or live
traffic was changed. This patch has not been deployed or merged.

Evidence: supplied `topos-benchmarks/results/expedited-baseline-1536-20261101`,
`focused-stress-1536-20261101-v2`, and `recovery-probes-1536-20261101` requests and
counts-only node receipts: baseline 4/4 answered; stress 2/7 answered, with one
deadline, one all_sentences_dropped, two model_error and one ask 429. Isolated
recovery ended model_error in 107.87 seconds (105 seconds between job start/end).
The preceding model observation listed a different resident model. This supports
investigating cold/model-switch latency, but does NOT establish its cause.
Model_error collapses HTTP errors, incomplete responses and other exceptions;
receipt sentence counts are zero when checked is absent, not proof the writer
never produced text. No raw owner text or private logs were copied into this change.

Code-level causes reproduced: writer/judge verification had an independent 25s
HTTP timeout before the 100s/60s generation read timeout; HTTP inactivity timeouts
are not overall deadlines. Expired calls still verified and posted with a forced
minimum one-second allowance. Separately, a judge model_error became False and
could trigger another provider call for every surviving sentence, obscuring
provider failure as quality rejection. The stress quality-filter case includes
four relevance drops, one support drop and three unjudged sentences; it cannot be
reclassified wholesale as provider failure. The ask 429 remains admission control.

Method: keep the existing 100s writer / 60s judge ceilings, but bound verification
and generation together with cancellable asyncio timeouts and close the client
on cancellation. Refuse expired work before opening transport. A judge
model_error now follows the existing judge_unavailable path and stops the phase;
a completed negative or malformed verdict still fails closed under existing
quality checks. No fallback, retry, ask resubmission, model pull/unload or routing
change. Existing kept-question deferral rules are unchanged (both provider error
categories already used the costly-deferral policy).

Affected families: all permissioned generated answers and sentence verification;
retrieval, prompts, permission filters, model/digest checks and release authority
rechecks are unchanged. Provider failure may now withhold an entire answer whose
earlier sentences passed, rather than continuing calls and returning a partial
answer. This is deliberate fail-closed behavior.

Verification: new tests were added before implementation. The first baseline
(`20261101T185701154126Z`) is retained but superseded: the workspace venv was Python
3.10 and the judge fixture lacked its explicit real-body/fake-transport marker.
Production code was restored before the corrected baseline. The comparable pair
is `20261101T185800205979Z` before (657 pass, 6 fail, 0 skip) and
`20261101T185837294168Z` after (663 pass, 0 fail, 0 skip), across all ten catalog
families, zero runner errors, identical catalog/fixtures/toolchain. Reports above
retain the full paths and hashes. The two service-level provider-error regressions
also failed before implementation. Tests exercise expired admission, cancellation
during verification/response, client closure, a fresh successful independent call,
and stopping after the first provider error; they do not prove server-side work
stops on client disconnect.

Real-model companion baseline and after result: unavailable/not run. This sprint
explicitly prohibits live generation, and this 1.5.36 checkout has neither
`docs/response-quality-live.md` nor `scripts/run_response_quality_live.py`.
Synthetic transport checks are not live-model recovery, answer-quality or latency
proof. Full live validation remains outstanding; no release readiness is claimed.
No Rig D was run. Rollback scope: revert this commit's sharing lifecycle changes,
associated tests/catalog entry and documentation; no database migration or runtime
configuration rollback is required.

Focused after-change validation: 230 passed in 43.20s across
`test_shared_model_recovery.py`, `test_answer_quality_aq158.py`,
`test_nr179_judge_reserve.py`, `test_answer_release.py`, and
`test_kept_answers_off1.py`. The run emitted 1,620 warnings (including existing
SQLite datetime-adapter deprecations), with no failures or skips. `git diff
--check` passed. Test durations are synthetic runtime, not user answer latency.

### Regression gate — RQ-2026-11-01-sharing-diagnostics — before

- Recorded (UTC): 2026-11-01T19:07:02.677870+00:00
- Note: Frozen diagnostic redaction and classification tests before implementation; no live generation
- Report: [machine-readable results](response-quality-runs/20261101T190702677870Z-RQ-2026-11-01-sharing-diagnostics-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 109 pass / 30 fail / 0 skip | pending |

### Regression gate — RQ-2026-11-01-sharing-diagnostics — after

- Recorded (UTC): 2026-11-01T19:08:13.500152+00:00
- Note: Fixed-field node diagnostics classify writer/judge phase failures without content; cancellation preserved; live cause remains unknown
- Report: [machine-readable results](response-quality-runs/20261101T190813500152Z-RQ-2026-11-01-sharing-diagnostics-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | 140 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 109 pass / 30 fail / 0 skip | 139 pass / 0 fail / 0 skip |

- Fixed: 30; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[generation-False-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[generation-False-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[generation-True-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[generation-True-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[verification-False-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[verification-False-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[verification-True-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[verification-True-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_judge_negative_verdict_is_not_provider_failure[synthetic-invalid-None-invalid_verdict]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_judge_negative_verdict_is_not_provider_failure[{"supported":"no"}-False-ok]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[http_status-generation-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[http_status-generation-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[http_status-verification-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[http_status-verification-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[incomplete_response-generation-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[incomplete_response-generation-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[incomplete_response-verification-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[incomplete_response-verification-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[malformed_response-generation-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[malformed_response-generation-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[malformed_response-verification-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[malformed_response-verification-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[timeout-generation-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[timeout-generation-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[timeout-verification-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[timeout-verification-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[transport_error-generation-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[transport_error-generation-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[transport_error-verification-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[transport_error-verification-True]`

### RQ-2026-11-01-sharing-diagnostics — node-only model failure attribution

Follow-up to local sharing lifecycle commit
`f7421b92ab638219fbe7f2cdeafe1384892dbc15`. Failure: the existing `model_error`
receipt cannot distinguish provider timeout, error status or incomplete response,
or identify whether verification or writer/judge generation consumed the time.
The 1.5.36 recovery cause remains **unknown**. A read-only scan of existing node
logs in the 2026-11-01 18:31–18:32 UTC recovery window found the sharing model_error
but no corresponding timeout or HTTP-status classification. Only fixed-word
counts were inspected/output; no private log excerpts were copied.

Method: `answer_release` now emits `permissions answer model` node log events
with exactly `role` (writer/judge), `phase` (verification/generation/admission),
`outcome` (closed code), `http_status` (numeric or null), and `elapsed_ms`
(monotonic phase duration). Structured handlers can read the `sharing_model`
LogRecord field; ordinary logs contain the same fixed-field JSON. Verification's
explicit exception causes are traversed at most eight links to classify the
wrapped HTTP exception, without formatting any exception. Outcomes distinguish
ok, timeout, http_status, transport_error, unreviewed_model, malformed_response,
incomplete_response, invalid_verdict, deadline_expired, cancelled and
unexpected_error. No exception strings, traceback, model content, prompt, URL,
token, request ID or grant ID is logged. Diagnostic handler failures do not change
answer decisions. Public receipts/refusals and quality-filter semantics remain
unchanged. A valid negative judge verdict logs ok; a malformed verdict logs
invalid_verdict but still returns None as before.

Affected family: diagnostics for all permissioned writer/judge calls; no routing,
model configuration, grant checks, prompts, admission gates or root deadlines
changed. Cancellation retains its original behavior: outer deadline -> model_error,
external task cancellation -> CancelledError, transport closed in either case.
The diagnostic timeout covers verification or generation according to the active
phase. It does not prove backend work stopped after disconnect or reveal the
provider's internal cause. No shared admission/root-deadline files were edited.

Verification: 30 new tests failed before production edits. Full ten-family gate
baseline `20261101T190702677870Z-RQ-2026-11-01-sharing-diagnostics-before.json`:
663 pass, 30 fail, zero skips/errors. The paired after report above records the
same catalog and fixtures. Focused suite (shared_model_recovery,
answer_quality_aq158, nr179_judge_reserve): 124 passed in 6.11s, 400 pre-existing
SQLite warnings. Synthetic private canaries in URLs, bodies, prompts and exception
messages are absent from complete captured LogRecords; no exception info is logged.
Tests also verify unchanged PolicyError codes, negative/invalid judge verdicts,
exactly one cancellation/timeout event per interrupted phase, and client closure.
`git diff --check` passed.

Live-model baseline and after: unavailable, explicitly prohibited in this sprint;
1.5.36 also lacks the companion live runner/docs. These synthetic tests are not
live recovery or actual-answer quality/latency proof. Active node untouched;
not deployed, merged or released. Roll back this diagnostics commit independently
of the lifecycle commit; no configuration or database migration is involved.

Completed comparable after gate:
`20261101T190813500152Z-RQ-2026-11-01-sharing-diagnostics-after.json` — 693 passed,
0 failed, 0 skipped, 0 runner errors; PASS with 30 fixes and no regressions.

### Regression gate — RQ-2026-11-01-resilience-integration — before

- Recorded (UTC): 2026-11-01T19:11:42.178667+00:00
- Note: Combined frozen regression fixtures against restored published 1.5.36 production paths; helper remains unwired. Integration baseline after independent development, not live traffic.
- Report: [machine-readable results](response-quality-runs/20261101T191142178667Z-RQ-2026-11-01-resilience-integration-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 103 pass / 38 fail / 0 skip | pending |

### Regression gate — RQ-2026-11-01-resilience-integration — after

- Recorded (UTC): 2026-11-01T19:12:29.667903+00:00
- Note: Combined verification deadlines, disconnect propagation, bounded sharing model phases, stop-on-judge-failure, and content-free diagnosis. Provider admission prototype remains excluded. Synthetic evidence only; no deployment or live recovery claim.
- Report: [machine-readable results](response-quality-runs/20261101T191229667903Z-RQ-2026-11-01-resilience-integration-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | 145 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 103 pass / 38 fail / 0 skip | 141 pass / 0 fail / 0 skip |

- Fixed: 38; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_deadline_cancels_inflight_closes_and_next_call_recovers[post-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_deadline_cancels_inflight_closes_and_next_call_recovers[post-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_deadline_cancels_inflight_closes_and_next_call_recovers[verify-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_deadline_cancels_inflight_closes_and_next_call_recovers[verify-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[generation-False-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[generation-False-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[generation-True-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[generation-True-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[verification-False-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[verification-False-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[verification-True-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_diagnostics_preserve_cancellation[verification-True-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_expired_call_never_opens_transport[False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_expired_call_never_opens_transport[True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_judge_negative_verdict_is_not_provider_failure[synthetic-invalid-None-invalid_verdict]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_judge_negative_verdict_is_not_provider_failure[{"supported":"no"}-False-ok]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[http_status-generation-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[http_status-generation-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[http_status-verification-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[http_status-verification-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[incomplete_response-generation-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[incomplete_response-generation-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[incomplete_response-verification-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[incomplete_response-verification-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[malformed_response-generation-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[malformed_response-generation-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[malformed_response-verification-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[malformed_response-verification-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[timeout-generation-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[timeout-generation-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[timeout-verification-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[timeout-verification-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[transport_error-generation-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[transport_error-generation-True]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[transport_error-verification-False]`
- fixes: `node::tests/permissions_v2/test_shared_model_recovery.py::tests.permissions_v2.test_shared_model_recovery::test_model_diagnostics_classify_without_content[transport_error-verification-True]`
- fixes: `node::tests/query/test_verification_deadline.py::tests.query.test_verification_deadline::test_cancelled_handler_signals_its_worker`
- fixes: `node::tests/query/test_verification_deadline.py::tests.query.test_verification_deadline::test_expiry_is_unavailable_not_a_heuristic_success_and_worker_finishes`

### Regression gate — RQ-2026-11-01-review-startup — before

- Recorded (UTC): 2026-11-01T19:46:02.264855+00:00
- Note: Review regression fixtures: retain published slow verification successes; preserve budget-aware cold startup. Production unchanged from first combined candidate.
- Report: [machine-readable results](response-quality-runs/20261101T194602264855Z-RQ-2026-11-01-review-startup-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 141 pass / 1 fail / 0 skip | pending |

### Regression gate — RQ-2026-11-01-review-corrections — before

- Recorded (UTC): 2026-11-01T19:47:21.059840+00:00
- Note: Frozen review regressions before production fixes: slow-success default and validated remote termination reason. Earlier startup-only baseline retained, superseded by this expanded test inventory.
- Report: [machine-readable results](response-quality-runs/20261101T194721059840Z-RQ-2026-11-01-review-corrections-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 143 pass / 3 fail / 0 skip | pending |

### Regression gate — RQ-2026-11-01-budget-review — before

- Recorded (UTC): 2026-11-01T19:48:49.502907+00:00
- Note: DNS cancellation and bounded transport lifecycle tests added before implementation; no live traffic.
- Report: [machine-readable results](response-quality-runs/20261101T194849502907Z-RQ-2026-11-01-budget-review-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 141 pass / 0 fail / 0 skip | pending |

### Regression gate — RQ-2026-11-01-budget-review — after

- Recorded (UTC): 2026-11-01T19:52:48.512439+00:00
- Note: Bounded reusable HTTP loop/resolver ownership, default120, synthetic cancellation/recovery and policy-independent lifecycle tests; no live traffic.
- Report: [machine-readable results](response-quality-runs/20261101T195248512439Z-RQ-2026-11-01-budget-review-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | 140 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 141 pass / 0 fail / 0 skip | 141 pass / 0 fail / 0 skip |

- Fixed: 0; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.

### RQ-2026-11-01-budget-review — bounded reusable transport resources

- Failure/cause: independent review of `b7290ca` reproduced an 80ms request taking 402ms with a fake 350ms native resolver. Per-request `asyncio.run()` waits for default-executor shutdown after cancelling HTTPX; native `getaddrinfo` is not cancellable. Repeating requests could retain many caller workers. Also, default60 would truncate previously successful 61–110s stress verifications per the coordinating review; this branch sets the authorized default120/max120 policy.
- Method: replace per-request loop ownership with one lazy, owned stdlib SelectorEventLoop, at most eight active HTTP operations, and at most four native resolver submissions. No waiting queue. DNS permits survive HTTP cancellation until the actual native future completes. HTTP-operation slots survive caller cancellation until their coroutine cleanup completes. Saturation returns typed compute_busy. Isolated per-call HTTPX clients preserve credential/context separation; no change to endpoint selection or transmitted content. Checks before and after resolution prevent stale resolution results starting new work after known cancellation.
- Lifecycle: `close_transport(timeout)` is an explicit hook, NOT wired into node/app shutdown in this narrow branch. It permanently refuses new work, signals existing budgets, observes cleanup boundedly, and keeps unresolved resources accounted for. No implicit replacement even when closed before first use. Root owns lifecycle integration if desired. Native DNS cannot be killed and may delay interpreter exit because the executor's exit hook joins native workers. A request can return while cleanup remains; resource counts are not GPU ownership/drain claims. Initial defaults 8 HTTP / 4 DNS are engineering bounds, not measured throughput/capacity. Initialized runtimes are not usable across fork.
- Scope/families: availability of all budgeted verification and remote Engine question families. No ranking, retrieval, prompt, permission or evidence change. Production changes only `engine/request_budget.py` and new `engine/budget_transport.py`; root owns startup, generic Engine dispatch checks, reason propagation and policy docs.
- Before: added five focused regressions before production edits. Targeted baseline: 14 passed / 5 failed (default policy, DNS caller return, three missing-lifecycle cases). Full frozen catalog baseline: 695 passed, zero skipped/errors, `20261101T194849502907Z-RQ-2026-11-01-budget-review-before.json`.
- After: complete paired gate 695 passed, zero skipped/errors/regressions/missing tests, same catalog/fixtures/toolchain, `20261101T195248512439Z-RQ-2026-11-01-budget-review-after.json`. Expanded focused suite 79 passed (23 request-budget tests plus surrounding Engine/API/verification contracts), two existing Starlette deprecation warnings. Four follow-up lifecycle/policy tests were added after the initial focused baseline; this is expanded focused coverage, not a frozen-count paired comparison. The full catalog remained frozen. The transport fixture now handles synthetic GET `/api/tags`, preventing root's forthcoming restored startup path from launching real services during tests.
- Synthetic coverage: stalled headers/trickling bodies; stalled DNS; explicit cancellation; twelve saturated offers with one retained resolver; eventual resolver recovery; repeated successful fake calls using the same native worker; cancellation-resistant HTTP cleanup retains its slot; context isolation; shutdown before dispatch; shutdown before first use; application event-loop-policy independence. Existing sync/remote interface and duration header tests pass.
- Live companion: NOT RUN, explicitly prohibited by the sprint. No actual-answer-quality, model-latency, live DNS/provider recovery or GPU-drain claim. All network execution was loopback fixture HTTP; DNS was replaced with controlled synthetic functions. The borrowed test environment was read-only.
- Deployment: dedicated `fix/budget-review` worktree from `b7290ca`, local commit only. No candidate production/test edits, active-node/service changes, deploy, release, model unload, grants, routines or Rig D. Integration with root startup/other review fixes remains to be tested on the combined candidate.
- Rollback: revert budget transport module and request wrapper/default change together; no database migration. Reverting cannot terminate already-running native resolver or provider work. Preserve truthful outstanding-resource reporting during any shutdown/restart decision.

### Regression gate — RQ-2026-11-01-review-corrections — after

- Recorded (UTC): 2026-11-01T19:55:12.077692+00:00
- Note: Reviewed integrated corrections: default120 preserves known slow success; bounded reusable HTTP/DNS runtime; budgeted cold startup; generic pre-inference expiry; narrow remote termination reasons. No live quality/capacity or provider-drain claim.
- Report: [machine-readable results](response-quality-runs/20261101T195512077692Z-RQ-2026-11-01-review-corrections-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | 145 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 143 pass / 3 fail / 0 skip | 146 pass / 0 fail / 0 skip |

- Fixed: 3; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.
- fixes: `node::tests/query/test_verification_deadline.py::tests.query.test_verification_deadline::test_default_budget_allows_previously_observed_slow_success`
- fixes: `node::tests/query/test_verification_deadline.py::tests.query.test_verification_deadline::test_remote_termination_reason_is_validated_and_preserved[cancelled]`
- fixes: `node::tests/query/test_verification_deadline.py::tests.query.test_verification_deadline::test_remote_termination_reason_is_validated_and_preserved[deadline_exceeded]`

### RQ-2026-10-31-01 — Summary packet empty on asks the store answers (rare gate)

- Recorded / implemented: 2026-11-01T03:42:09Z (UTC; 31 Oct 2026 local). Not loaded on a node,
  not live-verified, not released.
- Status: implemented on branch `claude/summary-empty-packet-lme` (node repo), PR
  dialoguesai/topos#232 (commit 8dbcc69d + this line). Owner merges.
- Failing question family and expected behavior: empty/incomplete evidence. A
  summary-mode ask whose subject is in the store and reached the candidates must
  return that evidence; an absent qualifier narrows the answer, it does not erase it.
- Symptom: in the 12-question LongMemEval S-split run (31 Oct–1 Nov 2026, Kaggle,
  real ingest path), 3 questions produced an EMPTY summary packet under every
  retrieval pack while raw mode returned 79–100 rows and found the gold session
  (ranks 5 and 27; the third's gold was not in raw's top 100 either).
- Observed root cause (proven on copies of the three bench DBs, Python 3.12, isolated
  HOME, offline models): `_rrf_fuse_summary_lists` → `_veto_for` rule 1 — every query
  token with FTS df ≤ 2 must be contained in some candidate's text blob or the whole
  fused list is dropped (`rare_gate emptied rare_token_unevidenced`,
  `empty_cause=gate_vetoed`). Per question: `sunday` df 0 (nowhere in 474 turns) with
  the other 7 content tokens evidenced by 4–16 of 43 candidates and the gold turn at
  vector rank 2; `homegrown` df 0 (nowhere in 503 turns) with serve/dinner/weekend/
  ingredients evidenced; a person's name df 0 although two stored turns carry it —
  the FTS `search_text` is the privacy-filtered rendering (`[NAME]`), so a name in
  an ask can never have df > 0 nor be evidenced — with 8 of 10 tokens evidenced and
  gold at vector rank 4. Not the supply-state abstention, not `unproven_closed`. Raw
  mode uses the same df map only to block its browse fallback, so it answered.
- Method and files: `topos/query/retrieval.py` — an absent word empties a part only
  when such words are not outnumbered by the words the candidates evidence, counted
  over the part's content tokens minus answer-shape vocabulary (so common subject
  words on a large store count); a lone fabricated subject and a tie still veto; the
  per-part gate and the browse-fallback gate are unchanged; a kept part records
  `rare_gate scoped absent_token_outnumbered` (tokens local-only) and the witness
  step keeps the items that evidenced the admitting words. `topos/query/narrowing.py`
  + `topos/protocol/narrowing_vocabulary.json` — the new reason slug.
- Dataset/model: LongMemEval S cleaned split, 3 bench DBs; no live model; vector
  lane with the node's offline embedder; no cache.
- Before/after evidence: before, 0 summary items for all three (4 packs each).
  After, 25 items each; gold session at rank 0 for the bike-ride and rotation-sheet
  questions (rank 0 and 3 by message); the dinner question's gold turn was never
  among the 43 fused candidates, so the gate change cannot reach it (lane recall,
  separate). Answer quality not assessed (no synthesis in this harness).
- Original case, negative controls, adjacent families: new
  `tests/query/test_absent_qualifier_narrows_not_erases.py` (12 tests: the three
  shapes, ledger, public-ledger redaction, lone-subject veto, tie veto, two-absent
  veto, per-part agreement, large-store count). 332 passed / 9 skipped / 14 xfailed
  across the rare-gate, multi-needle, output-shape, transform-invariance, narrowing
  vocabulary/ledger, ledger-conservation, lane-fault, corpus-density, scope-aware
  frequency, vector-head, index-lag and support-retention files.
- Paired gate RQ-2026-10-31-01 below: 669 tests, 4 fixed (the new test, added to
  the catalog before the baseline), 0 regressed, 0 outstanding, PASS.
- Latency: unchanged in kind (one extra membership pass over blobs already built).
- Known limits / unresolved: a name in an ask still has df 0 because the index text
  is redacted — covered here only when the rest of the sentence is evidenced; a
  proper fix needs entity linking or an unredacted df source. Whether synthesis
  states the absent word is not addressed: the public ledger carries the slug only.
- Rollback scope: revert the three files and the catalog line; no data migration.

### Regression gate — RQ-2026-10-31-01 — before

- Recorded (UTC): 2026-11-01T03:34:32.500232+00:00
- Note: Summary packet empties on LongMemEval asks the store answers (3/12 S-split questions, 31 Oct): the rare-token gate vetoes the whole fused list when one query word has FTS df 0 (sunday, homegrown, a [NAME]-redacted person) while every other content word is evidenced and the gold turn is in the vector lane. Hypothesis: an absent word should narrow, not erase, when the evidenced words outnumber it. New failing test added to the catalog before this baseline.
- Report: [machine-readable results](response-quality-runs/20261101T033432500232Z-RQ-2026-10-31-01-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 82 pass / 4 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 103 pass / 0 fail / 0 skip | pending |

### Regression gate — RQ-2026-10-31-01 — after

- Recorded (UTC): 2026-11-01T03:40:51.576068+00:00
- Note: Rare gate: an absent word (df<=2, unevidenced) now empties a part only when such words are not outnumbered by the words the candidates evidence, counted over the part content tokens; a lone fabricated subject and a tie still veto; a kept part records rare_gate scoped absent_token_outnumbered. Measured after: the three LongMemEval stores return 25 summary items each (gold at rank 0 for 1d4e3b97 and 7161e7e2; 06f04340 gold was never among the 43 candidates).
- Report: [machine-readable results](response-quality-runs/20261101T034051576068Z-RQ-2026-10-31-01-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 140 pass / 0 fail / 0 skip | 140 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 82 pass / 4 fail / 0 skip | 86 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 103 pass / 0 fail / 0 skip | 103 pass / 0 fail / 0 skip |

- Fixed: 4; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.
- fixes: `node::tests/query/test_absent_qualifier_narrows_not_erases.py::tests.query.test_absent_qualifier_narrows_not_erases.TestTheLedgerSaysWhatWasNotFound::test_a_narrowed_ask_is_not_an_empty`
- fixes: `node::tests/query/test_absent_qualifier_narrows_not_erases.py::tests.query.test_absent_qualifier_narrows_not_erases.TestTheMeasuredFailure::test_a_large_store_counts_the_subject_words_too`
- fixes: `node::tests/query/test_absent_qualifier_narrows_not_erases.py::tests.query.test_absent_qualifier_narrows_not_erases.TestTheMeasuredFailure::test_a_redacted_name_is_outnumbered_by_the_sentence_around_it`
- fixes: `node::tests/query/test_absent_qualifier_narrows_not_erases.py::tests.query.test_absent_qualifier_narrows_not_erases.TestTheMeasuredFailure::test_an_absent_day_does_not_empty_an_ask_about_bike_rides`

## RQ-2026-11-10-FACTS — Explicit grants for every bundled fact category

- **Problem and cause:** The selective-facts/v1 catalog exposed only `work.career` and four predicates. Twenty-seven other entries were unavailable and the two Moments categories were absent. Enabling derivation did not add grant eligibility. Existing structured values, inferred/dependent facts, producer-default owner-only disclosure, person bindings and day-only validity also required a release implementation.
- **Method:** Added a separate, frozen `selective-facts/v2` contract covering all 30 bundled categories, 237 pack predicates and 12 mapped legacy predicates. New consent pins exact category revisions/predicates, selected statement/inference types and individual exclusions. Added bounded schema-based projections, dependency and entity protection/revision checks, native evidence qualification, source-backed same-day validity, index discovery/invalidation, owner preview and control-plane output enforcement. Web choices explicitly select assertion types and require review for a v1-to-v2 upgrade. Existing signed v1 grants retain their original grammar and scope.
- **Affected question families:** Questions answered from granted facts across work, health, finances, relationships, beliefs, identity, preferences, life events, capabilities, moments and the other enabled categories. Extraction/inference labels are not truth verification. Native evidence must still satisfy existing source authorship/provenance/review rules; selecting a category cannot authorize an otherwise denied source or protected fact.
- **Validation:** Final focused node checks: 121 passed, plus the new structured-preview consent regression passed; control-plane contracts: 38 passed; web owner suite: 370 passed; TypeScript check passed. All 30 categories exercised through signed grant release; all 237 predicate schemas exercised; a real DerivationWriter integration covers person sidecars and same-day validity. The broader node run recorded 426 passes, one failure from a formerly unsupported category now becoming supported, one opt-in measurement skip, and four existing expected failures. The obsolete fixture was changed to explicitly registered future categories, preserving its outside-catalog property, and its focused rerun passed. These skip/xfail results are not claimed as a fully green broader suite.
- **Paired offline gate:** Baseline `20261110T230719487439Z-RQ-2026-11-10-FACTS-before.json` and final `20261110T234019947677Z-RQ-2026-11-10-FACTS-after.json`: 705 passes each, no missing tests, regressions, skips or runner errors; same fixtures, catalog and toolchain. New all-category cases were added alongside implementation; no recorded failing pre-change run of those new cases is available.
- **Earlier attempts retained:** Two baseline attempts could not write shared web test caches; a successful baseline followed with local caches. An intermediate after gate passed 705 tests. A later rerun passed all 705 but selected Node 20 through a login shell, so it was correctly non-comparable; the final rerun explicitly pinned baseline Node 22.21.1. One control-plane invocation imported the still-unmodified canonical node rather than the isolated implementation; rerunning with the tested node on PYTHONPATH passed all 38 cases. A malformed-fact test initially corrupted the row before fixture source review; moving corruption after setup fixed the test setup, without weakening the refusal assertion.
- **Real-model companion:** The before run completed 33 real answers and judgments but is INCOMPLETE: grant UI edits during execution changed web provenance. This is an unavailable valid live baseline, not a passing comparison. The after run completed 33 real answers; verdict **NOT_COMPARABLE** ([report](response-quality-runs/20261110T232757132651Z-live-after.json)). Comparison reasons: execution or judgment incomplete. After-run execution errors: []. Any quality/latency flags in this non-comparable run are retained in the report, not attributed to this change. The after-run judge flagged low scores in attribution, follow-up and empty-evidence answers, plus critical-failure flags in attribution, follow-up and citation cases. Seven cases also exceeded the comparison latency threshold. These remain unresolved diagnostic findings; they are not waived or claimed to be caused by the grant change. The synthetic companion exercises the home-chat agent; it excludes node retrieval, control-plane and browser latency. Model grades are uncalibrated, and timings from a shared desktop are descriptive.
- **Deployment:** Applied to local node, control-plane and web source checkouts only. No services deployed or restarted; no live grant, owner data, feature flag or derivation switch changed. Unrelated local edits preserved. Coordinated deployment and explicit owner consent are required to use the new scope.
- **Rollback:** Revert only this change's node/control-plane/web files together, leaving unrelated edits intact. Old signed v1 grants remain valid; an older implementation will refuse v2 grants, which must be re-reviewed under a supported contract. Existing index revision checks force rebuilds for changed implementation semantics.


### Regression gate — RQ-2026-11-10-FACTS — before

- Recorded (UTC): 2026-11-10T23:03:54.721634+00:00
- Note: Baseline before expanding explicit fact ontology consent; existing grants remain pinned.
- Report: [machine-readable results](response-quality-runs/20261110T230354721634Z-RQ-2026-11-10-FACTS-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 13 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 30 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 0 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 22 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 0 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 3 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 0 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 0 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 66 pass / 0 fail / 0 skip | pending |
- Runner error: web: FileNotFoundError: [Errno 2] No such file or directory: '/Users/dialogues/Documents/Codex/2026-11-10/righ/work/topos/.response-quality-artifacts/20261110T230354721634Z-RQ-2026-11-10-FACTS-before/web.xml'

### Regression gate — RQ-2026-11-10-FACTS — before

- Recorded (UTC): 2026-11-10T23:05:28.564800+00:00
- Note: Baseline before all-fact grant support; new all-fact tests run separately.
- Report: [machine-readable results](response-quality-runs/20261110T230528564800Z-RQ-2026-11-10-FACTS-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **FAIL**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 13 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 30 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 0 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 22 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 0 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 3 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 0 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 0 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 66 pass / 0 fail / 0 skip | pending |
- Runner error: web: FileNotFoundError: [Errno 2] No such file or directory: '/Users/dialogues/Documents/Codex/2026-11-10/righ/work/topos/.response-quality-artifacts/20261110T230528564800Z-RQ-2026-11-10-FACTS-before/web.xml'

### Regression gate — RQ-2026-11-10-FACTS — before

- Recorded (UTC): 2026-11-10T23:07:19.487439+00:00
- Note: Usable baseline after fixing sandbox-local test caches; unchanged grant behavior.
- Report: [machine-readable results](response-quality-runs/20261110T230719487439Z-RQ-2026-11-10-FACTS-before.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | pending |
| dates_and_recency | 53 pass / 0 fail / 0 skip | pending |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | pending |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | pending |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | pending |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | pending |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | pending |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | pending |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | pending |
| query_and_synthesis_contracts | 146 pass / 0 fail / 0 skip | pending |

### Regression gate — RQ-2026-11-10-FACTS — after

- Recorded (UTC): 2026-11-10T23:20:19.933871+00:00
- Note: All 30 categories use new explicit v2 fact consent; original v1 grants retain scope. Added source and dependency-bound stored-value projections.
- Report: [machine-readable results](response-quality-runs/20261110T232019933871Z-RQ-2026-11-10-FACTS-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | 145 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 146 pass / 0 fail / 0 skip | 146 pass / 0 fail / 0 skip |

- Fixed: 0; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.

### Live answer evaluation — RQ-2026-11-10-FACTS — before

- UTC: 2026-11-10T23:07:30.659600+00:00; verdict: **INCOMPLETE**.
- Report: [20261110T230730659600Z-live-before.json](response-quality-runs/20261110T230730659600Z-live-before.json).
- Lane: production home-chat agent + real local model + frozen MCP evidence; excludes node retrieval, CP and browser latency.
- Scores are uncalibrated model judgments (0–2), not human-validated truth. p95 is descriptive with this small sample.
- Raw answers, prompts, evidence and judge reasons are stored locally in ignored .response-quality-artifacts/.

| Case | Quality mean before → after | Median ms before → after | Samples |
| --- | --- | --- | --- |
| injection | — → 1.87 | — → 21391 | 3 |
| followup | — → 0.40 | — → 43278 | 3 |
| partial | — → 2.00 | — → 15205 | 3 |
| advice | — → 2.00 | — → 22040 | 3 |
| exclude-ai | — → 1.87 | — → 19524 | 3 |
| yesterday | — → 2.00 | — → 37006 | 3 |
| attribution | — → 0.47 | — → 23780 | 3 |
| citation | — → 2.00 | — → 32177 | 3 |
| empty | — → 1.80 | — → 9704 | 3 |
| identity-unresolved | — → 2.00 | — → 2 | 3 |
| general | — → 2.00 | — → 8493 | 3 |

### Regression gate — RQ-2026-11-10-FACTS — after

- Recorded (UTC): 2026-11-10T23:38:14.134843+00:00
- Note: Final all-category grant implementation, including production writer person bindings and source-backed same-day facts; v1 consent remains unchanged.
- Report: [machine-readable results](response-quality-runs/20261110T233814134843Z-RQ-2026-11-10-FACTS-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **NOT_COMPARABLE**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | 145 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 146 pass / 0 fail / 0 skip | 146 pass / 0 fail / 0 skip |

- Fixed: 0; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.
- reasons: `toolchain changed`

### Regression gate — RQ-2026-11-10-FACTS — after

- Recorded (UTC): 2026-11-10T23:40:19.947677+00:00
- Note: Final implementation, Node 22.21.1 pinned to baseline; previous rerun passed 705 tests but login shell selected Node 20 and was non-comparable.
- Report: [machine-readable results](response-quality-runs/20261110T234019947677Z-RQ-2026-11-10-FACTS-after.json)
- Catalog: `response-quality-contracts-1`; none: synthetic fixtures and mocked generation; not a live-model evaluation.
- Gate: **PASS**. Skips and existing failures are not waived.
- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.

| Question family | Before | After |
| --- | --- | --- |
| identity_and_dataset | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| dates_and_recency | 53 pass / 0 fail / 0 skip | 53 pass / 0 fail / 0 skip |
| routing_and_source_exclusions | 145 pass / 0 fail / 0 skip | 145 pass / 0 fail / 0 skip |
| empty_and_incomplete_evidence | 74 pass / 0 fail / 0 skip | 74 pass / 0 fail / 0 skip |
| followups_and_breadth | 17 pass / 0 fail / 0 skip | 17 pass / 0 fail / 0 skip |
| citations_and_attribution | 22 pass / 0 fail / 0 skip | 22 pass / 0 fail / 0 skip |
| work_recall_and_recommendations | 18 pass / 0 fail / 0 skip | 18 pass / 0 fail / 0 skip |
| general_knowledge_negative_controls | 172 pass / 0 fail / 0 skip | 172 pass / 0 fail / 0 skip |
| cache_and_access_boundaries | 29 pass / 0 fail / 0 skip | 29 pass / 0 fail / 0 skip |
| query_and_synthesis_contracts | 146 pass / 0 fail / 0 skip | 146 pass / 0 fail / 0 skip |

- Fixed: 0; regressed: 0; missing: 0; added: 0; remaining non-passes: 0.

### Live answer evaluation — RQ-2026-11-10-FACTS — after

- UTC: 2026-11-10T23:27:57.132651+00:00; verdict: **NOT_COMPARABLE**.
- Report: [20261110T232757132651Z-live-after.json](response-quality-runs/20261110T232757132651Z-live-after.json).
- Lane: production home-chat agent + real local model + frozen MCP evidence; excludes node retrieval, CP and browser latency.
- Scores are uncalibrated model judgments (0–2), not human-validated truth. p95 is descriptive with this small sample.
- Raw answers, prompts, evidence and judge reasons are stored locally in ignored .response-quality-artifacts/.

| Case | Quality mean before → after | Median ms before → after | Samples |
| --- | --- | --- | --- |
| injection | 1.87 → 1.87 | 21391 → 38163 | 3 |
| followup | 0.40 → 0.40 | 43278 → 62404 | 3 |
| partial | 2.00 → 2.00 | 15205 → 37178 | 3 |
| advice | 2.00 → 2.00 | 22040 → 40732 | 3 |
| exclude-ai | 1.87 → 1.93 | 19524 → 18477 | 3 |
| yesterday | 2.00 → 2.00 | 37006 → 39161 | 3 |
| attribution | 0.47 → 0.40 | 23780 → 81633 | 3 |
| citation | 2.00 → 2.00 | 32177 → 58248 | 3 |
| empty | 1.80 → 1.60 | 9704 → 36183 | 3 |
| identity-unresolved | 2.00 → 2.00 | 2 → 53 | 3 |
| general | 2.00 → 2.00 | 8493 → 7815 | 3 |

Comparison: `{"verdict": "NOT_COMPARABLE", "reasons": ["execution or judgment incomplete"], "quality_regressions": ["injection/attribution/below_floor", "attribution/groundedness/below_floor", "attribution/completeness/below_floor", "attribution/relevance", "attribution/relevance/below_floor", "attribution/attribution/below_floor", "attribution/critical_failure", "followup/groundedness/below_floor", "followup/completeness/below_floor", "followup/relevance/below_floor", "followup/attribution/below_floor", "followup/critical_failure", "empty/groundedness", "empty/groundedness/below_floor", "empty/attribution", "empty/attribution/below_floor", "citation/critical_failure"], "latency_regressions": ["injection", "attribution", "partial", "advice", "followup", "empty", "citation"]}`

### Release candidate 1.5.49 — integration validation

Prepared on the separate 1.5.48 candidate (`fe964dbf`, PR #280), preserving its backfill fix. The owner explicitly assigned this work to 1.5.49. Integrated checks: 122 focused node grant tests, 38 control-plane tests, 370 owner UI tests and TypeScript passed. Control-plane static checking found and removed an unused import; no permission behavior changed.

The legacy `scripts/run_release_eval.py` scorecard is **FAIL**, retained in `eval_reports/1.5.49.json`: the negotiation/minimality harness it imports was removed by `0f969868` (N8, retirement of old sharing doors). Its privacy pytest leg completed with 382 passes and two existing expected failures; unauthorized-access and canary-extraction probes measured zero leaks (6 and 270 probes), and deny p95 was 0.2 ms. Missing negotiation measurements are not a passing metric. Earlier interrupted helper attempts are retained locally and are not claimed as results. The completed run used scratch data, the owner-database tripwire and an unavailable local-model endpoint, consistent with its deterministic lane.

The procedure of record, `RELEASING.md`, and current `just eval-release` use the exact-tree six-pair Rig D gate. That gate and selected CI are still pending for this candidate. The other release's owner waiver does not cover this tree. No production deployment or tag has been made by this task at this point.
