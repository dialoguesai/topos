# Response-quality integration manifest — 2026-10-26

Prepared for the combined node/viewer release. No staging, commits, publishing,
deployment or runtime restart was performed by the regression-suite task this turn.
The checkouts contain unrelated concurrent changes: integrate hunks, not whole
dirty-file sets. The release coordinator owns reconciliation and publishing.

## Node retrieval changes

- New `topos/query/latest_exchange.py` and `tests/query/test_latest_exchange.py`:
  exact participant identity scoped to selected dataset; roster joins on dataset,
  source and conversation; event-time ordering before limit; explicit unresolved,
  ambiguous, missing-dataset and undated outcomes. No contact writes or guesses.
- `topos/query/retrieval.py`: owner-only latest-text branch and result receipt;
  existing permission/disclosure/blackhole/exclusion processing retained. Shared
  file also contains conversation provenance changes; do not replace wholesale.
- `topos/query/types.py`: optional dataset_id on RetrievalRequest.
- `topos/query/pipeline.py`: pass dataset_id to retrieval; bypass memory hits for
  latest-text and dated-activity questions so stale results do not survive imports
  or interpretation fixes.
- `topos/core/handlers/query.py`: forward selected dataset to the pipeline. Shared
  handler; review alongside the newer installed/remote implementation.
- `topos/query/game_layer.py`: expose latest_exchange receipt in public results.
- `topos/protocol/query_field_contract.json`: dataset_id is forwarded, not consumed
  before the handler; local stores can contain several datasets.
- `topos/query/planner.py`: recognize “was/am I doing/up to” and “have I been doing”
  as activity recall so the parsed date window reaches strict retrieval.
- `tests/query/test_query_multi_turn_cache.py`: latest-text and recap freshness.
- `tests/query/test_activity_recap_recency.py`: recap paraphrases and strict-window
  forwarding, plus deterministic healthy-empty and failed-vector coverage.
- `tests/query/test_owner_source_urls.py` includes shared conversation-source work;
  it was run by this task, but do not assign its entire diff to retrieval changes.

## Web changes (paths relative to topos-react-app/next)

- `features/home/runHomeChatTurn.ts`: resolve selected Topos dataset and pass it to
  agent dispatch. Shared file with viewer/source work.
- `lib/mcp/homeChatAgent.ts`: bind query_scope to selected dataset; carry complete
  public receipts to latest-text grounding; preserve identity-unresolved result
  through insufficient-evidence and max-step exits. Shared file.
- `lib/mcp/conversationCitations.ts` and `.test.ts`: receipt-aware citation guidance
  and explicit unresolved/ambiguous identity responses, including scope-set payloads.
  This new file originated in conversation/source work and is shared; preserve the
  exact ASCII `topos-source:` URL convention.
- `lib/mcp/homeChatOrchestration.ts` and `.test.ts`: suppress AI-summary enrichment
  after explicit latest-text receipts and for explicit AI-conversation exclusions.
- `lib/mcp/scopeRoutingRules.ts`: bounded explicit AI-chat exclusion detector.
- `lib/mcp/scopeQueryRouter.ts` and `.test.ts`: apply exclusion to selected routes;
  test exclusion paraphrases versus positive mixed-source / “not only” requests.
- `lib/mcp/homeChatAgent.toolElection.test.ts`: actual dispatch uses selected dataset,
  only messages:read, and retains the unresolved-identity answer.
- Source URLs, markdown renderer, cards and viewer components are shared/other-task
  work. This task has not staged or committed those files.

## Regression tooling and documentation

Node repo: `scripts/run_response_quality_regression.py`,
`scripts/response_quality_catalog.json`,
`scripts/tests/test_response_quality_regression.py`,
`docs/response-quality-regression.md`, `docs/response-quality-changelog.md`,
`docs/response-quality-runs/*.json`, this manifest, `docs/DEVELOPMENT.md`, and the
`.gitignore` entry for raw `.response-quality-artifacts/`.

Workspace-only: AGENTS.md now requires the full paired gate and log entries;
REPO_MAP.md links the quality log. These are not changes inside the node Git repo.

## Historical verification and resolved integration blockers

- Original baseline and after (before offline-policy repair): **652 tests; 651 pass, 1 fail, 0 skipped**, no
  runner errors, no newly failing tests, no inventory or fixture changes. All ten
  families ran. Nine gate self-tests passed, including a fix-plus-regression case
  which must fail even when total passing counts are unchanged.
- Exact reports: `docs/response-quality-runs/20261026T210519773395Z-RQ-2026-10-26-04-before.json`
  and `docs/response-quality-runs/20261026T210713103299Z-RQ-2026-10-26-04-after.json`.
  This pair validates the gate on current application behavior; it does NOT measure
  improvement from the earlier fixes (those had already been applied).
- **Original gate blocker (resolved in integration):** `test_empty_window_is_no_match_when_source_history_exists`
  in `tests/query/test_activity_recap_recency.py` expects no_match, gets engine_failed.
  It also fails with the pre-change date detector. Investigate the deliberately
  minimal fixture / engine error classification; do not blanket-waive it.
- Earlier focused web checks: 184 passed. Earlier query-field/retrieval-text
  contracts: 45 passed / 1 skipped. The later broad suite is the paired record.
- Last typecheck at this task's earlier live verification: 12 generated `.next`
  references to removed permissions/lab routes; no changed-file diagnostics. The
  viewer task may have since repaired generation; rerun on integrated code.
- Repo-map checker still references absent `topos/topos/query/minimizer.py`.
- **Original runtime/release blocker (resolved by schema90 integration):** checkout previously had schema83 migrations while the
  installed node was 1.5.27/schema90. Never run the old checkout against the real DB,
  downgrade the installed package, or bypass the schema guard. Reconcile remote
  release code and reapply only reviewed changes before building a new release.
- Installed runtime patches are temporary, not a release. Local backups:
  `/tmp/topos-latest-exchange-backup-1793039162` and
  `/tmp/topos-yesterday-retrieval-backup-1793041393`. Do not copy a whole old checkout
  over the installed package. The release coordinator owns future restarts.

## Live findings (not a frozen-corpus benchmark)

- Clean latest-text run: receipt binds the selected personal dataset; only messages:read, eight
  iMessage records, no AI sources, latest stored exchange by event time. Identity now
  resolves because contacts populated during testing; missing-identity behavior is
  fixture-tested, not asserted for the changed live dataset. No contacts were written
  by this task. UI elapsed 3m18s (overlapped another generation).
- Yesterday recap: October25 local window reaches retrieval; work/browsing/social
  activity replaces false absence claims. UI elapsed 6m27s. Detailed chronology
  has not been individually audited. Neither live run proves overall model quality.
- Open quality issues: slow synthesis and plain record-text citations rather than
  reliable clickable inline citations. The offline gate does not measure live-model
  faithfulness or response latency. Record release evaluation separately.

Re-run the broad gate on the integrated node/web revision pair before release. A
nonzero gate exit is a blocker, not a success with a footnote. Preserve failed and
non-comparable runs in the log.

## Current integration status

The reviewed node changes are now in the coordinator's current-main schema90
release worktree. Existing owner-agent policy and viewer changes were preserved.
The original empty-window failure came from an unstubbed semantic service that
could initialize a real embedding model; production error classification was
correct. The fixture now covers healthy empty results and a synthetic service
outage explicitly. The runner disables model downloads and detects fixture edits
during execution. Nine runner self-tests pass.

The first integrated after run passes all 653 tests but correctly reports
NOT_COMPARABLE to the overlapping baseline. RQ-2026-10-26-06 records the fresh
frozen pair. This is contract/replay verification, not proof of live-model quality
improvement. The coordinator owns the combined commit, full release checks,
publishing and restarts. No independent commit or deployment is authorized here.

Final frozen verification: **PASS — 653/653 in both runs**, zero skipped,
zero runner errors, zero regressions, matching fixture inventory/runtime policy.
Reports:
- `docs/response-quality-runs/20261026T212332346443Z-RQ-2026-10-26-06-before.json`
- `docs/response-quality-runs/20261026T212358145552Z-RQ-2026-10-26-06-after.json`

The behavioral node changes require a new node release; the web changes require
the coordinated web deployment. The test suite and log alone need neither.
Cross-repository CI enforcement is not installed by this change; the paired gate
is required by the contributor workflow and must be run for subsequent changes.

Final release coordinator recheck after anonymizing the remaining cache fixture: RQ-2026-10-26-07, 653/653 passing in both runs, no skips, errors or regressions. Reports: `20261026T212808514273Z-RQ-2026-10-26-07-before.json` and `20261026T214404651611Z-RQ-2026-10-26-07-after.json`. This remains contract verification, not a pre-fix quality benchmark.
