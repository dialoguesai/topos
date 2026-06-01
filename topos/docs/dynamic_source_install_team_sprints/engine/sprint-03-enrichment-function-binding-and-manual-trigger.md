# Engine — Sprint 3: Enrichment function binding and manual trigger

| Field | Value |
|---|---|
| Sprint ID | EN-S3 |
| PRD | [PRD_TOPOS_UPDATE_REQUIREMENTS.md](../../PRD_TOPOS_UPDATE_REQUIREMENTS.md) |
| Depends on | EN-S2 |
| Unblocks | Topos React App UI sprint pack trigger semantics |

## Sprint goal
Install dynamic enrichment definitions safely by binding function IDs to trusted adapters and enforce trigger semantics.

## Product goal
Users can trust enrichment behavior: automatic runs when configured automatic, and manual runs only when explicitly triggered.

## Gap (current -> target)
| Area | Before | After |
|---|---|---|
| Enrichment install model | Static job assumptions | Installed enrichment contracts resolved via trusted catalog |
| Function execution safety | Potential unbounded IDs | Function ID binding restricted to known adapters |
| Trigger semantics | Partial/manual flow coverage | Deterministic automatic/manual semantics with tests |

## In scope
- Dynamic enrichment install representation and activation.
- Trusted function catalog/adapter binding.
- Manual trigger API/runtime orchestration.

## Out of scope
- Arbitrary user-provided code execution.
- New enrichment model architecture beyond adapter mapping.

## Tickets
### EN-S3-01 — Trusted enrichment function catalog binding
Goal mapping: improves G2, G4, G5.

Acceptance criteria:
- Installed source enrichment `function_id` resolves only through trusted catalog.
- Unknown function IDs fail install with clear diagnostics.
- Adapter invocation metadata captured for traceability.

Gap closed:
- Replaces implicit runtime assumptions with explicit safe binding.

Tests:
- Unit tests for known/unknown function resolution.
- Integration install tests with valid and invalid enrichment specs.

### EN-S3-02 — Manual and automatic trigger orchestration
Goal mapping: improves G1, G3, G4, G5.

Acceptance criteria:
- `enrichment_trigger=automatic` runs post-ingest automatically.
- `enrichment_trigger=manual` does not auto-run on ingest and requires explicit trigger endpoint.
- Manual trigger returns progress/outcome envelope with records processed.

Gap closed:
- Gives predictable enrichment control aligned with source contract.

Tests:
- Existing manual trigger flow test expanded for installed-source path.
- Integration tests for automatic trigger path and manual trigger endpoint.

## Risks
- Adapter interface drift across enrichment jobs.
- Manual trigger endpoint misuse without scope auth checks.

## Test plan summary
- Validate deterministic manual trigger in CI lane.
- Optional real model run smoke in non-blocking lane.

## Definition of Done
- EN-S3 AC met with automated coverage.
- Manual vs automatic semantics demonstrably enforced.
- Unsupported function IDs never execute.
