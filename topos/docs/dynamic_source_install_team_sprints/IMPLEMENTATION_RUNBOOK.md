# Dynamic source install — implementation runbook

PRD: [PRD_TOPOS_UPDATE_REQUIREMENTS.md](../PRD_TOPOS_UPDATE_REQUIREMENTS.md)  
Sprint pack: [README.md](./README.md)

## Goal Alignment Check
- Change: Execute backend dynamic source install/test delivery (engine + control-plane) with policy-safe contracts.
- Goal score: G1=2, G2=2, G3=2, G4=2, G5=2, Total=10
- Risks to goals:
  - Scope/authorization gaps could allow unintended install operations.
  - Contract drift could hide important diagnostics from users.
- Required adjustments before merge:
  - Freeze install/test envelope schema with shared fixtures.
  - Require authz/audit coverage before enabling by default.
- Validation plan:
  - Run wave-level automated tests plus manual checklist at the end.

## 1) Architecture in this repo

| Layer | Paths | Role |
|---|---|---|
| Engine HTTP + runtime | `topos/topos/app.py`, `topos/topos/api/`, `topos/topos/sources/runtime_install.py`, `topos/topos/ingestion/`, `topos/topos/canonicalization/` | Install lifecycle, dynamic registry resolution, ingest/enrichment execution, install/test APIs |
| Engine handlers/events | `topos/topos/core/handlers.py` | Handler wiring for engine-side command routing used by CP patterns |
| Engine tests/scripts | `topos/tests/`, `topos-control-plane/scripts/topos-node-maintenance/run_registry_source_smoke.py` | Deterministic and smoke validation paths |
| Control plane proxy | `topos-control-plane-test/control_plane/main.py` | Authz-gated proxy routes, version resolver, audit records |
| App registry source-of-truth | `app_registry/next-app/src/features/registration/`, `app_registry/next-app/supabase/migrations/` | Version contract schema and resolver contract expectations |
| Frontend consumer (separate pack) | `topos-react-app/docs/PRD_SOURCE_INSTALL_AND_INSTANT_VALIDATION_UI.md` | Consumes backend contracts from this pack in a separate frontend sprint track |

## 2) Execution waves (dependency order)

| Wave | Scope | Deliverables | Goal validation checkpoint |
|---|---|---|---|
| W1 | Engine EN-S1 | Install lifecycle service, scoped registry resolution, rollback semantics | Install/uninstall by scope works; static source regression unchanged |
| W2 | Engine EN-S2 + CP-S1 start | Parser/mapper validation and installed-source ingest execution; CP proxy skeleton | Invalid contracts fail clearly; ingest path uses installed source IDs directly |
| W3 | Engine EN-S3 + CP-S1 finish | Trusted enrichment function binding + manual trigger orchestration; CP version-id resolver | Manual trigger required for manual configs; unknown function IDs denied |
| W4 | CP-S2 | Authz/audit hardening and envelope consistency | Unauthorized operations denied; audit evidence complete |
| W5 | Engine EN-S4 | Observability, diagnostics taxonomy, integration matrix hardening | Logs/metrics correlate install->ingest->enrich; rollout evidence complete |

## 3) Per-sprint executor checklist
1. Read sprint gap table and tickets before editing code.
2. Implement smallest slice that satisfies acceptance criteria.
3. Add/adjust tests named in sprint file.
4. Run commands in section 4.
5. Record outcome and blockers in PR notes.

## 4) Automated test commands

Run from repo root `/Users/dialogues/developer/topos-control-plane`:

```bash
# Engine test lane
cd topos && pytest tests/ -q --tb=short

# Focused dynamic source/enrichment tests
cd topos && pytest tests/ -q -k "registry_source or enrichment or install" --tb=short

# Control-plane lane (if CP changes are in scope this wave)
cd topos-control-plane-test && pytest tests/ -q --tb=short

# Frontend tests are executed in separate UI sprint pack:
# topos-react-app/docs/source_install_and_instant_validation_ui_team_sprints/
```

## 5) Manual verification checklist

### 5.1 Install lifecycle
- [ ] Install by `version_id` succeeds and appears in installed list.
- [ ] Install by JSON row fallback succeeds when resolver is unavailable.
- [ ] Failed install returns clear diagnostics and previous active install remains valid.
- [ ] Uninstall removes installed runtime binding for the selected scope only.

### 5.2 Ingest and enrichment validation
- [ ] Ingest test passes for installed source and returns count/summary metadata.
- [ ] `enrichment_trigger=manual` does not auto-run enrichment on ingest.
- [ ] Manual enrichment trigger runs and returns deterministic status/results.
- [ ] `enrichment_trigger=automatic` runs post-ingest automatically.

### 5.3 Security and transparency
- [ ] Unauthorized install/test requests are denied without leaking resource existence.
- [ ] Audit records exist for install/update/uninstall/test actions.
- [ ] Response envelopes include request IDs and stable error codes.

### 5.4 Backward compatibility
- [ ] Existing static source ingest/canonicalization/enrichment flows still pass.
- [ ] Legacy parser/mapper registry behavior remains unchanged for non-installed sources.

## 6) Exit criteria
- All sprint acceptance criteria complete with listed tests passing.
- Manual checklist complete without P0/P1 issues.
- Goal alignment score remains >= 9/10 with no unresolved security regressions.
