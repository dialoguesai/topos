# Control plane — Sprint 1: Install/test proxy and version resolver

| Field | Value |
|---|---|
| Sprint ID | CP-S1 |
| PRD | [PRD_TOPOS_UPDATE_REQUIREMENTS.md](../../PRD_TOPOS_UPDATE_REQUIREMENTS.md) |
| Depends on | EN-S1 API shell |
| Unblocks | Topos React App UI sprint pack install flow |

## Sprint goal
Provide stable control-plane proxy endpoints for install/test/list operations and resolve `version_id` to canonical source payload.

## Product goal
Frontend can run install and validation through one trusted API surface without embedding registry internals.

## Gap (current -> target)
| Area | Before | After |
|---|---|---|
| Install/test API surface | No CP endpoints for dynamic source install flows | CP routes for install/test/list/trigger |
| Registry row resolution | Manual local row input for testing | First-class `version_id` resolver path |
| Contract consistency | Ad hoc payload assumptions | Stable request/response envelopes |

## In scope
- CP route definitions and forwarding logic.
- Resolver service for registry `version_id`.
- Error normalization between registry/engine/CP layers.

## Out of scope
- UI form implementation.
- Engine internal install logic changes.

## Tickets
### CP-S1-01 — CP install/test/list/trigger proxy routes
Goal mapping: improves G1, G2, G4, G5.

Acceptance criteria:
- CP exposes endpoints for install, list, uninstall, ingest test, and manual enrichment trigger.
- Engine error envelope is propagated with CP context without losing root diagnostics.
- Endpoint docs include required auth headers and request schema.

Gap closed:
- Adds a production-ready bridge from frontend to engine dynamic install flows.

Tests:
- Integration tests with mocked engine responses (success/failure).
- Contract tests for response envelope stability.

### CP-S1-02 — `version_id` resolver to canonical source payload
Goal mapping: improves G3, G4, G5.

Acceptance criteria:
- CP accepts install requests by `version_id` and resolves source definition from app registry.
- Missing/invalid version IDs return deterministic 4xx responses.
- Resolved payload normalized before forwarding to engine.

Gap closed:
- Removes manual row export requirement for normal install path.

Tests:
- Resolver unit tests for found/not-found/malformed rows.
- Integration tests for install-by-version_id flow.

## Risks
- Registry access latency can slow install calls.
- Resolver and engine contract versions can drift.

## Test plan summary
- Validate both install-by-version_id and raw-source-json fallback path.
- Snapshot response envelope shape for frontend client safety.

## Definition of Done
- CP-S1 tickets complete with passing tests.
- Frontend can install/test via CP without direct engine URL knowledge.
