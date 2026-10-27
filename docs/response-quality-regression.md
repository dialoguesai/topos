# Response-quality regression gate

Run the complete gate for every change to retrieval, routing, prompts, synthesis,
fallbacks, source/dataset selection, cache rules or model configuration. Run it
before editing production behavior and again after the change. Do not select only
the family that motivated the fix.

## Paired workflow

From the workspace containing the node and web checkouts:

```bash
python3 topos/scripts/run_response_quality_regression.py before \
  --change RQ-YYYY-MM-DD-NN --note 'Symptom and hypothesis before the change'

# Make the implementation change. Use the exact before-report path printed above.
python3 topos/scripts/run_response_quality_regression.py after \
  --baseline topos/docs/response-quality-runs/BEFORE_REPORT.json \
  --note 'What changed and why'
```

For a new regression test, add the failing case first, then take the baseline,
then fix production code. Freeze the catalog and selected tests between runs.
Changing the inventory, selected tests, lockfiles or runtime versions makes the
comparison NOT_COMPARABLE; it must not silently become a quality improvement.
If fixtures need correction, take a new baseline and retain the old comparison.
Never change expected answers merely to get a green gate.

The runner requires the node checkout's `.venv/bin/python` with pytest and the web
checkout's installed `node_modules/.bin/vitest`. It does not install dependencies,
change the running node, call a live model, or opt into live-data pytest lanes.
`--web-root` supports a different web checkout. There is no family-only flag.

Both phases automatically append a summary to
[the change log](response-quality-changelog.md). Sanitized, machine-readable
reports in `docs/response-quality-runs/` retain individual test outcomes, family
counts, test durations, catalog/test hashes, toolchain pins, commit SHAs and dirty
working-tree fingerprints. Commit these reports with the change. Raw JUnit/logs
remain in ignored `.response-quality-artifacts/`; do not publish them by default.

## Coverage and gate rules

The versioned [catalog](../scripts/response_quality_catalog.json) composes existing
production-entry and unit tests across ten question families:

1. Participant identity, ambiguity, mentions versus participants, selected datasets.
2. Relative dates, timezones, DST, event clocks and recency.
3. Source routing, exclusions, positive mixed-source requests and tool dispatch.
4. Empty stores, incomplete retrieval and unsupported absence claims.
5. Follow-up resolution and preserving question breadth.
6. Source citations, reported speech and speaker attribution.
7. Work recall versus advice/recommendations.
8. General knowledge questions that must not spuriously retrieve personal data.
9. Cache freshness and caller/access boundaries, including memory-off agent tests.
10. Query field and synthesis contracts.

An improvement in one family cannot offset a regression elsewhere. A previously
passing test becoming failed, errored or skipped is a regression. Existing
failures and skipped coverage keep the gate red. Missing tests, collection errors,
changed fixture fingerprints and mismatched baselines prevent a valid comparison.
Exit zero means the complete after run passes under comparable conditions. Exit
one means investigate the report; it is not permission to waive a failure.

This is a required contributor/agent workflow, not a claim that cross-repository
branch protection has been installed. Both checkouts must be available. The report
identifies both revisions so a passing result cannot be attributed to one repo
without knowing the paired version. Automated CI enforcement needs that same
explicit pairing of node and web revisions.

## What this does not establish

These are synthetic contract tests and replayed model outputs. They check evidence
selection, orchestration and answer safeguards, not whether the currently selected
model generates a faithful, useful answer for arbitrary questions. File hashes cover
the selected test files; imported fixture helpers are also represented by repository
provenance but are not a complete immutable corpus snapshot. Review helper changes
when judging comparability. Test duration is not live response latency.

Before a release affecting generation or ranking, also run the existing query-quality
and MCP agent evals described in `topos-control-plane/docs/eval_science/README.md`
and `topos-control-plane/scripts/run_mcp_agent_eval.py`. Keep the corpus, question
catalog, model/version, sampling configuration and judge fixed for a paired run.
Record groundedness, citation correctness, uncertainty and end-to-end latency in
the log separately. Do not compare mutable the selected personal dataset runs as though only code
changed. Those optional live evals can expose private data or incur model cost;
the offline gate does not silently launch them.

Test the gate itself with:

```bash
python3 topos/scripts/tests/test_response_quality_regression.py
```
