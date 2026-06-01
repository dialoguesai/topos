# Engine team plan

## Mission
Implement dynamic source install/runtime resolution in the Topos engine so source contracts from app registry can be installed, validated, executed, and observed safely.

## Product goal
A user-installed source behaves as a first-class engine source: install succeeds or fails clearly, ingest runs with installed parser/mapper, and manual enrichment is explicitly triggerable.

## Engineering requirements
1. Install lifecycle supports install/list/uninstall/upgrade/rollback with persisted state.
2. Runtime resolution is installed-first and scoped (user/device/app/dataset) with concurrency safety.
3. Parser/mapper/enrichment function contracts are validated before activation.
4. Execution paths use installed config directly and enforce manual vs automatic trigger semantics.
5. Logs/metrics/diagnostics expose source/version/install/test context.

## Sprint index

| Sprint | Theme |
|---|---|
| [sprint-01-install-lifecycle-and-registry-safety.md](./sprint-01-install-lifecycle-and-registry-safety.md) | Install persistence + scoped runtime registry safety |
| [sprint-02-dynamic-parser-mapper-validation-and-execution.md](./sprint-02-dynamic-parser-mapper-validation-and-execution.md) | Parser/mapper validation and ingest execution migration |
| [sprint-03-enrichment-function-binding-and-manual-trigger.md](./sprint-03-enrichment-function-binding-and-manual-trigger.md) | Dynamic enrichment binding and manual trigger orchestration |
| [sprint-04-observability-and-hardening.md](./sprint-04-observability-and-hardening.md) | Metrics, diagnostics, and hardening tests |
