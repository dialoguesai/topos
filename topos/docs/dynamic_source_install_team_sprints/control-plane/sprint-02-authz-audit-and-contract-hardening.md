# Control plane — Sprint 2: Authz, audit, and contract hardening

| Field | Value |
|---|---|
| Sprint ID | CP-S2 |
| PRD | [PRD_TOPOS_UPDATE_REQUIREMENTS.md](../../PRD_TOPOS_UPDATE_REQUIREMENTS.md) |
| Depends on | CP-S1 |
| Unblocks | Rollout readiness and security review |

## Sprint goal
Enforce authorization and auditable install/test actions while hardening API contracts.

## Product goal
Install and test actions remain user-controlled, policy-safe, and traceable across all entry points.

## Gap (current -> target)
| Area | Before | After |
|---|---|---|
| Authorization | Incomplete per-action policy checks | Explicit authz checks per install/test action |
| Auditability | Sparse trail for dynamic source operations | Durable audit logs for all lifecycle operations |
| Contract hardening | Partial edge-case handling | Strong validation and clear 4xx/5xx taxonomy |

## In scope
- Authz middleware/rules for install/test endpoints.
- Audit event schema + writer integration.
- Contract hardening and docs updates.

## Out of scope
- Enterprise RBAC redesign.
- New external audit pipeline.

## Tickets
### CP-S2-01 — Authorization policy for install/test operations
Goal mapping: improves G1, G2, G5.

Acceptance criteria:
- Only authorized principals can install/update/uninstall/test for permitted org/app/user scope.
- Deny responses do not leak resource existence.
- Policy checks consistent across all CP install/test endpoints.

Gap closed:
- Moves from permissive/implicit access to policy-first behavior.

Tests:
- Authz allow/deny integration tests across operation matrix.
- Negative tests for scope mismatch and missing credentials.

### CP-S2-02 — Audit and contract hardening
Goal mapping: improves G2, G4, G5.

Acceptance criteria:
- Audit records capture actor, action, source/version, scope, outcome, and timestamp.
- Response envelopes include `request_id` and stable error codes.
- API docs updated with examples for install/test success and failure.

Gap closed:
- Enables root-cause traceability and frontend-safe contract handling.

Tests:
- Audit write assertions in integration tests.
- Contract snapshot tests for key failure modes.

## Risks
- Audit volume may grow quickly during repeated smoke runs.
- Overly strict authz may block expected internal workflows if scopes are misconfigured.

## Test plan summary
- Exercise full lifecycle through CP with both authorized and unauthorized actors.
- Validate audit records for each lifecycle action.

## Definition of Done
- CP-S2 AC complete and tested.
- Security review items resolved or explicitly deferred with owners.
