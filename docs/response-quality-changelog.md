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

### RQ-2026-10-31-01 — Summary packet empty on asks the store answers (rare gate)

- Recorded / implemented: 2026-11-01T03:42:09Z (UTC; 31 Oct 2026 local). Not loaded on a node,
  not live-verified, not released.
- Status: implemented on branch `claude/summary-empty-packet-lme` (node repo); PR to
  follow. Owner merges.
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
