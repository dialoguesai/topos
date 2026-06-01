# Engine — Sprint 1: Install lifecycle and registry safety

| Field | Value |
|---|---|
| Sprint ID | EN-S1 |
| PRD | [PRD_TOPOS_UPDATE_REQUIREMENTS.md](../../PRD_TOPOS_UPDATE_REQUIREMENTS.md) |
| Depends on | Existing runtime install module and registry contracts |
| Unblocks | EN-S2, CP-S1, Topos React App UI sprint pack |

## Sprint goal
Add a durable install lifecycle service and isolate runtime registry behavior with scoped resolution.

## Product goal
Users can install/uninstall sources reliably and trust that installs do not interfere with existing sources or other user scopes.

## Gap (current -> target)
| Area | Before | After |
|---|---|---|
| Install persistence | Process-local install helper only | Durable install state machine with rollback |
| Runtime isolation | Global mutation with limited safeguards | Scoped installed-first lookup and concurrency control |
| Error handling | Script-level failures | Structured install error envelopes and rollback outcomes |

## In scope
- Install state store and lifecycle service.
- Install/list/uninstall/upgrade/rollback APIs in engine layer.
- Scoped registry resolution with locking strategy.

## Out of scope
- UI rendering details.
- Production multi-process distributed lock implementation.

## Tickets
### EN-S1-01 — Install lifecycle service + persistence
Goal mapping: improves G1, G2, G4, G5.

Acceptance criteria:
- Engine supports install/list/uninstall/upgrade/rollback operations for source versions.
- Install states persisted with timestamps and failure reason details.
- Failed install triggers safe rollback to prior active version for scope.

Gap closed:
- Moves from ephemeral installs to operational lifecycle management.

Tests:
- Unit lifecycle transition tests.
- Integration tests for successful install, failed install with rollback, uninstall.

### EN-S1-02 — Scoped installed-first runtime registry
Goal mapping: improves G1, G2, G3, G4.

Acceptance criteria:
- Runtime resolution checks installed definitions first by scope key (user/device/app/dataset).
- Fallback to static registries remains for non-installed sources.
- Concurrent installs for different scopes do not cross-pollute runtime resolution.

Gap closed:
- Prevents global cross-talk while preserving backward compatibility.

Tests:
- Scoped lookup unit tests.
- Concurrency integration test across two scope keys.

## Risks
- Scope key design mismatches upstream identity model.
- Lock granularity too coarse may reduce throughput.

## Test plan summary
- Run focused engine tests for lifecycle and scoped resolution.
- Validate static-source regression path remains unchanged.

## Definition of Done
- All EN-S1 AC met and tests green.
- No regression on existing static sources.
- Failure envelopes include actionable diagnostics.
