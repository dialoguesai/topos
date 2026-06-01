# PRD: Topos Dynamic Source Install Update

## 1. Vision
Topos should support installing source definitions dynamically from app registry version rows, then reliably running ingestion and enrichment using those installed definitions. This backend update should be shaped so the Topos React App can power a copy/paste install and instant validation flow in the next sprint.

## 2. Problem
Current static registries for parsers/mappers/enrichment jobs are fine for built-in sources but do not scale to user-created sources that use new IDs and custom contracts from app registry.

## 3. Goals
- Install a source from `app_registry_app_source_versions` data (`version_id` or row JSON).
- Register parser and mapper under exact IDs from the source contract.
- Support manual and automatic enrichment semantics from installed source config.
- Provide stable backend APIs (proxied by control plane) for UI install/test actions.

## 4. Non-goals (this sprint)
- Full frontend implementation in Topos React App.
- Arbitrary code execution from registry payloads.
- Final production hardening for multi-tenant concurrency without phased rollout.

## Goal Alignment Check
- Change: Introduce dynamic source install/runtime execution in Topos, with control-plane-proxied install/test APIs and frontend-ready contracts.
- Goal score: G1=2, G2=2, G3=2, G4=2, G5=2, Total=10
- Risks to goals:
  - Global registry mutation can weaken least-privilege isolation if scoping/concurrency are incomplete.
  - Unvalidated enrichment function references can create opaque failures and lower trust.
  - UI/proxy/backend contract drift can reduce transparency and user control.
- Required adjustments before merge:
  - Enforce scoped runtime resolution by user/device/app/dataset.
  - Block install when parser/mapper/function contracts fail validation.
  - Ship clear install/test envelopes with diagnostics for user-facing transparency.
- Validation plan:
  - Deterministic tests for manual-not-auto enrichment and manual-trigger execution.
  - Install lifecycle tests (install/uninstall/upgrade/rollback/idempotency).
  - Scoped concurrency tests to prove no cross-talk across users/sources.
  - Optional real-model integration tests (HF/Ollama) outside deterministic CI lane.

## 5. Requirements for the actual Topos update

### 5.1 Install lifecycle
- Add install service with explicit operations:
  - install
  - list installed
  - uninstall
  - upgrade
  - rollback
- Install by either:
  - `version_id` (preferred; fetched server-side), or
  - full source version row JSON (fallback/testing).
- Enforce idempotent install behavior for repeated requests.
- Persist installed package state per user/device/app:
  - `installed`
  - `active`
  - `failed`
  - `rolled_back`
- Support rollback to prior version on failed upgrade.

### 5.2 Runtime registry behavior
- Resolve installed source definitions before static defaults.
- Scope runtime state by source/user/device context to avoid unsafe global collisions.
- Scope resolution by user/device/app/dataset to prevent global cross-talk.
- Ensure concurrency-safe registry access (no unsafe global mutable races).
- Preserve backward compatibility for static built-in sources.

### 5.3 Dynamic parser + mapper
- Register parser classes under installed `schema_id` / `parser_id`.
- Register mapper classes under installed `canonical_mapper_id`.
- Validate parser spec at install time:
  - required fields
  - path grammar
  - type coercion rules
- Validate canonical mapper output contract:
  - required IDs
  - sender/content/timestamp semantics
- Ensure mapper compatibility with both canonicalization paths currently used in engine.
- Reject invalid contracts with clear install errors.
- Provide deterministic parse errors and install-time rejection on invalid parser specs.

### 5.4 Enrichment function handling (updated)
- Treat enrichment as installed declarative runtime config, not hardcoded static names only.
- Install `source_enrichments` plan from source definition:
  - stage
  - trigger mode
  - function ID
  - input/output mappings
  - output table target
- Resolve `function_id` through a trusted engine-side function catalog/adapter registry.
- Bind `function_id -> runtime adapter` at install time.
- Fail install when enrichment function references are unresolved or incompatible.
- Do not execute arbitrary code from app registry payload.
- Enforce trigger policy:
  - `manual` enrichment does not run during ingest
  - manual trigger endpoint runs enrichment post-ingest
  - `automatic` enrichment runs during ingestion only when configured.

### 5.5 Execution and orchestration
- Ingestion must use installed parser/mapper/enrichment config directly (no alias hacks).
- Manual enrichment should never auto-run on ingest when trigger is manual.
- Explicit manual trigger should execute configured jobs and persist derived outputs.

### 5.6 API contracts for UI-ready integration
- Add engine-level install/test endpoints (then proxied by control plane):
  - install source
  - test ingestion on sample payload/file
  - test enrichment on sample payload
  - list install/test status
- Return compact, machine-readable result objects for UI rendering.

### 5.7 Control-plane proxy compatibility
- Control plane mediates UI calls, resolves `version_id`, forwards to active engine.
- Enforce authz and audit trail for install/test actions.

### 5.8 Observability and diagnostics
- Include `source_id`, `version_id`, install/test run IDs in logs.
- Emit install/ingest/enrichment metrics for success/failure/latency/counts.
- Provide actionable errors for unresolved parser/mapper/function references.
- Surface mapping/validation failures with clear root-cause diagnostics.

### 5.9 Security and governance
- Enforce install authorization (org/app/user scope).
- Validate and sanitize installed contracts before activation.
- Audit install/update/uninstall and enrichment trigger actions.

### 5.10 Backward compatibility and migration
- Preserve static source behavior while dynamic path rolls out.
- Feature-flag dynamic install path initially.
- Provide migration/cutover strategy and compatibility checks for legacy sources.

### 5.11 Testing requirements
- Unit tests:
  - contract validation
  - parser/mapper/function resolution
  - trigger policy handling
- Integration tests:
  - install -> ingest -> manual enrichment flow
  - install via `version_id` and JSON fallback
  - uninstall/rollback behavior
- Add install lifecycle tests for install/uninstall/upgrade/rollback/idempotency.
- Add scoped concurrency tests (multi-source/multi-user isolation).
- Add enrichment binding tests (function resolution success/failure).
- Add optional integration tests for real model execution (HF/Ollama), separate from deterministic CI path.
- Keep deterministic control-flow tests that do not require external model runtime.

## 6. Engineering breakdown — teams, sprints, tickets

**Sprint pack:** [dynamic_source_install_team_sprints](./dynamic_source_install_team_sprints/README.md)

### 6.1 Engine

| Sprint | Ticket ID | Title | Acceptance criteria | Tests |
|---|---|---|---|---|
| S1 | EN-S1-01 | Install lifecycle + persistence | install/list/uninstall/upgrade/rollback APIs and persisted install states (`installed`, `active`, `failed`, `rolled_back`) | unit lifecycle tests + DB integration |
| S1 | EN-S1-02 | Scoped dynamic registries | runtime resolution installed-first, scoped by user/device/app/dataset, concurrency-safe | scoped concurrency tests |
| S2 | EN-S2-01 | Dynamic parser + mapper validation | parser/mapper install-time validation and deterministic failure envelopes | contract tests for valid/invalid specs |
| S2 | EN-S2-02 | Execution path migration | ingest/enrichment execution uses installed config directly (no alias fallback in install path) | integration install->ingest |
| S3 | EN-S3-01 | Dynamic enrichment install model | install `source_enrichments`, bind `function_id` via trusted catalog/adapter registry | function binding success/failure tests |
| S3 | EN-S3-02 | Manual trigger orchestration | manual trigger executes post-ingest for manual configs; no auto-run on ingest | integration manual trigger test |
| S4 | EN-S4-01 | Observability + diagnostics | install/test run IDs, structured logs, metrics, mapping failure diagnostics | log/metrics assertions where feasible |

### 6.2 Control plane

| Sprint | Ticket ID | Title | Acceptance criteria | Tests |
|---|---|---|---|---|
| S1 | CP-S1-01 | Install/test proxy endpoints | control-plane routes proxy install/test/list to active engine with normalized envelopes | route integration tests with mocked engine |
| S1 | CP-S1-02 | Version-id resolver | resolve `version_id` from app registry and forward canonical source payload to engine | resolver tests + deny paths |
| S2 | CP-S2-01 | Authz + audit | org/app/user authorization and audit records for install/update/uninstall/test actions | authz allow/deny tests + audit write assertions |

### 6.3 Frontend dependency boundary
- Topos React App install/instant-validation implementation is intentionally tracked in a separate PRD and sprint pack:
  - `topos-react-app/docs/PRD_SOURCE_INSTALL_AND_INSTANT_VALIDATION_UI.md`
  - `topos-react-app/docs/source_install_and_instant_validation_ui_team_sprints/README.md`
- This backend PRD only owns engine + control-plane deliverables and contracts needed by that frontend pack.

### 6.4 Shared Definition of Done
- All ticket acceptance criteria met with listed tests passing.
- No regression for existing static source ingestion/enrichment behavior.
- Install/test route contracts documented for engine and control plane.
- Goal alignment remains >= 9/10 and unresolved risks tracked explicitly.

## 7. Rollout strategy
- Phase 1: feature-flagged install/test backend path for internal usage.
- Phase 2: control-plane proxy endpoints consumed by UI.
- Phase 3: default-on dynamic source installs with static fallback retained.

