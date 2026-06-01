# Engine — Sprint 2: Dynamic parser/mapper validation and execution

| Field | Value |
|---|---|
| Sprint ID | EN-S2 |
| PRD | [PRD_TOPOS_UPDATE_REQUIREMENTS.md](../../PRD_TOPOS_UPDATE_REQUIREMENTS.md) |
| Depends on | EN-S1 |
| Unblocks | EN-S3, CP-S1 test endpoint quality |

## Sprint goal
Make parser/mapper install contracts first-class and ensure ingestion executes against installed definitions directly.

## Product goal
Installed sources run ingestion deterministically without hidden aliases, so users can trust what they installed is what executes.

## Gap (current -> target)
| Area | Before | After |
|---|---|---|
| Parser/mapper contract validation | Partial runtime assumptions | Explicit install-time schema/contract validation |
| Execution path | Mixed dynamic/static assumptions | Installed config drives parser/mapper selection directly |
| Failure visibility | Low-level exceptions | Typed install/test validation errors |

## In scope
- Parser/mapper contract validators.
- Installed parser/mapper registration lifecycle integrated with EN-S1 service.
- Ingestion path updates to resolve installed config first.

## Out of scope
- Arbitrary custom code download or execution.
- Non-Topos canonical model expansions.

## Tickets
### EN-S2-01 — Install-time parser and mapper contract validation
Goal mapping: improves G2, G3, G4, G5.

Acceptance criteria:
- Install rejects malformed parser specs and incompatible mapper definitions.
- Error envelope includes field-level reason and remediation hint.
- Successful install records normalized contract metadata.

Gap closed:
- Prevents runtime surprises by validating before activation.

Tests:
- Unit validator tests for valid and invalid contracts.
- Integration install API tests for error envelope structure.

### EN-S2-02 — Installed-source execution path for ingest
Goal mapping: improves G1, G3, G4, G5.

Acceptance criteria:
- Ingestion for installed source resolves parser/mapper from installed scope first.
- No alias fallback is used in installed-source execution path.
- Existing static source path still resolves and ingests unchanged.

Gap closed:
- Aligns runtime behavior with user expectation of installed source identity.

Tests:
- Integration test install -> ingest with dynamic source row payload.
- Regression test ingest for static built-in source.

## Risks
- Validation strictness may block real-world contracts initially.
- Execution migration can affect legacy assumptions in ingestion manager.

## Test plan summary
- Validate both JSON row install path and version-id install path.
- Confirm deterministic outputs for sample JSONL ingest.

## Definition of Done
- EN-S2 tickets complete with tests.
- Installed-source ingest parity validated against expected canonical output.
- Regression suite for static sources stays green.
