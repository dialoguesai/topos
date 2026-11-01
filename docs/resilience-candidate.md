# Request lifetime resilience candidate — 2026-11-01

Base: published 1.5.36 (`4e2ac21`). Candidate only: no version bump, release,
deployment, active-node restart, live traffic, or Rig D.

## Included

- Standard verification: one request budget (60s default, configurable positive
  `deadline_s` up to 120s), cooperative cancellation, bounded socket lifetime,
  expiry as unavailable rather than late heuristic success. Remaining duration
  propagates to remote Engine; database/compute separation is retained.
- Permissioned shared answers: writer verification + generation share the existing
  100s ceiling; judge verification + generation share the existing 60s ceiling.
  Expired calls never open a transport. Provider judge failure stops further
  sentence calls via fail-closed judge-unavailable handling. Negative quality
  verdicts remain distinct and grant/release/model guards are unchanged.
- Shared-answer diagnostics: fixed role, phase, outcome, status and elapsed fields.
  No exception strings, content, URLs, tokens or identifiers in these new events.
  Public receipt schemas remain unchanged.
- ASGI disconnect regression, stalled/trickling fake provider tests, remote queue
  expiry, and retained ownership of uncooperative workers. Large invalid numeric
  budgets reject cleanly without float-conversion overflow.

## Verification

The combined frozen-test gate restores published production paths to capture the
pre-fix behavior after independent development, then restores the combined patch.
The new request-budget helper exists but is unwired in that control. All ten
families run in both phases; reports retain exact test/catalog/runtime hashes.

- Before: 662 pass, 38 fail; 700 tests, no skips or runner errors. Failures are new
  regression cases for lifetime handling and diagnostic behavior.
- After: 700 pass; comparable PASS, zero regressions/missing/added tests.
- Focused lifecycle/transport/sharing suite: 348 pass in 50.68s, 1624 deprecation
  warnings (existing SQLite binding style and Starlette TestClient dependency).
  These counts overlap the quality gate and must not be summed as unique tests.
- Separate HTTP edge check: 19 pass, included again in the combined focused run.

Reports:
- `response-quality-runs/20261101T191142178667Z-RQ-2026-11-01-resilience-integration-before.json`
- `response-quality-runs/20261101T191229667903Z-RQ-2026-11-01-resilience-integration-after.json`

These tests use synthetic records, replayed outputs and fake local transports.
They establish contract/lifecycle behavior, not live model quality or a throughput
improvement. The original live provider-error cause remains unresolved.

## Explicitly excluded

The separate opt-in provider-admission experiment is not included. It only bounds
participating same-host clients and lacks uncertainty reconciliation and complete
transport coverage. It is not ready to enable or to claim global GPU scheduling.
No UI is implemented here. No routine quota bypass or reservation API is created.
The separate owner-verification reader-budget issue is unchanged.

## Release and capacity gates

1. Review the default 60s/maximum 120s verification policy against fixed live cases.
   A deadline is a failure bound, not a successful-response SLO. Deploy both sides
   to validate remote propagation; old compute hosts retain caller-side bounds only.
2. Use a newly commissioned benchmark baseline with pre-run grant policy and model
   digests, then focused pairs and a gradual ramp. Keep older reports unchanged;
   post-run grant observations cannot establish comparable pre-run policy.
3. Ensure sufficient naturally available routine quota for workload and recovery.
   Test routines remain no-delivery and archived. Accepted shared asks are polled,
   never automatically resubmitted; honor Retry-After.
4. Measure caller terminal state, worker terminal state, and provider work separately.
   Closing a connection is not evidence that GPU work drained. Prove per-service
   recovery without restart before claiming the live failure resolved.
5. Preserve the required fixed-corpus live query-quality comparison before a release
   affecting generation. Offline success does not measure useful-answer quality.

Implement provider uncertainty reconciliation and integrate every selected ingress
before enabling shared admission. Preserve remote compute and protected-local
assessment boundaries. A larger stress run follows these focused checks, not an
automatic maximum workload.
