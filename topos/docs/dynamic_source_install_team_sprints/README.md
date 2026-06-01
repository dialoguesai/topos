# Dynamic source install — team sprints

Execution pack for [PRD_TOPOS_UPDATE_REQUIREMENTS.md](../PRD_TOPOS_UPDATE_REQUIREMENTS.md), scoped to backend delivery only (engine + control-plane).

## Goal Alignment Check
- Change: Deliver installable runtime source definitions with control-plane-proxied install/test and frontend-ready validation flows.
- Goal score: G1=2, G2=2, G3=2, G4=2, G5=2, Total=10
- Risks to goals:
  - Cross-tenant runtime collisions if scoping is incomplete.
  - Opaque install failures if validation/diagnostics are weak.
- Required adjustments before merge:
  - Enforce scoped resolution + deterministic install-time validation.
  - Require standardized response envelopes for UI transparency.
- Validation plan:
  - Lifecycle, concurrency, and manual-trigger integration tests.
  - Proxy authz/audit tests.

| Team | Folder | Sprint range |
|---|---|---|
| Engine | [engine](./engine/README.md) | S1-S4 |
| Control plane | [control-plane](./control-plane/README.md) | S1-S2 |

Frontend implementation is tracked separately:
- PRD: `topos-react-app/docs/PRD_SOURCE_INSTALL_AND_INSTANT_VALIDATION_UI.md`
- Sprint pack: `topos-react-app/docs/source_install_and_instant_validation_ui_team_sprints/README.md`

Suggested read order:
1. Engine S1-S2
2. Control-plane S1
3. Engine S3-S4
4. Control-plane S2

Implementation sequencing, commands, and manual verification checklist:
- [IMPLEMENTATION_RUNBOOK.md](./IMPLEMENTATION_RUNBOOK.md)
