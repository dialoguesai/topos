# Engine — Sprint 4: Observability and hardening

| Field | Value |
|---|---|
| Sprint ID | EN-S4 |
| PRD | [PRD_TOPOS_UPDATE_REQUIREMENTS.md](../../PRD_TOPOS_UPDATE_REQUIREMENTS.md) |
| Depends on | EN-S1, EN-S2, EN-S3 |
| Unblocks | Production rollout gate |

## Sprint goal
Add install/test observability and finish hardening coverage for rollout confidence.

## Product goal
Operators and users can understand install/test outcomes quickly, and regressions are caught before rollout.

## Gap (current -> target)
| Area | Before | After |
|---|---|---|
| Observability | Sparse logs across install/test flow | Structured logs + metrics + correlation IDs |
| Diagnostics | Ad hoc failures | Standardized install/test response envelopes and failure taxonomy |
| Coverage | Narrow integration focus | End-to-end install->ingest->manual enrichment matrix |

## In scope
- Structured observability across install/test lifecycle.
- Failure taxonomy + consistent diagnostics payload.
- Final integration + regression suite expansion.

## Out of scope
- Full production dashboard implementation.
- Long-term analytics data warehouse exports.

## Tickets
### EN-S4-01 — Structured logs and metrics for install/test lifecycle
Goal mapping: improves G2, G4, G5.

Acceptance criteria:
- Logs include source/version/install scope/test run identifiers.
- Metrics emitted for install success/failure, ingest success/failure, enrichment success/failure.
- Logs redact sensitive payload content by default.

Gap closed:
- Moves from opaque failure triage to traceable operations.

Tests:
- Unit tests for log envelope builder/redaction behavior.
- Integration assertions for metrics counters (or mock emitter assertions).

### EN-S4-02 — Hardening integration matrix
Goal mapping: improves G1, G2, G3, G4, G5.

Acceptance criteria:
- Integration tests cover version-id install and source-row-json install fallback.
- Matrix includes install, ingest, manual enrichment trigger, uninstall cleanup.
- Regression tests prove static source flow unaffected.

Gap closed:
- Provides rollout confidence for dynamic install feature flag expansion.

Tests:
- Full integration suite in `pytest`.
- Optional smoke test script execution docs updated.

## Risks
- Observability overhead may impact high-volume ingest paths.
- Test matrix runtime may exceed default CI budget.

## Test plan summary
- Keep deterministic CI lane mandatory.
- Keep external-model lane optional and non-blocking.

## Definition of Done
- EN-S4 AC complete.
- Required deterministic integration matrix green.
- Rollout checklist inputs generated from logs/metrics.
