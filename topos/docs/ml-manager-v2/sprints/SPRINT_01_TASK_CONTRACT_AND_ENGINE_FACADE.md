# Sprint 01 — Task contract and Engine facade

**Topos Engine V1**

---

## Objective

Introduce the **task contract** (ProcessingTask, ProcessingResult) and the **Engine** as the single entry point for processing. No callers are migrated yet; the Engine has stubbed validator and router so that a minimal sync `run(task)` path exists and can be tested.

**Plan refs:** [../MIGRATION_AND_GAPS.md](../MIGRATION_AND_GAPS.md) Steps 1–2; [../IMPLEMENTATION_MAP.md](../IMPLEMENTATION_MAP.md) §§4–6; [../ARCHITECTURE_MAPPING.md](../ARCHITECTURE_MAPPING.md) (Engine layout).

---

## Scope

- **Task contract**
  - Add `ProcessingTask` with fields from PRD §6.1: id, type, subtype, source_id, record_ids, input, model_request, execution, options, requested_by, created_at. JSON-serializable (Pydantic or dataclass).
  - Add `ProcessingResult` with fields from PRD §6.2: task_id, status, output, output_type, confidence, provenance, execution_meta, error.
  - Optional: helper to build a task from (source_id, job_name, record_ids, input).
- **Engine package**
  - Add `topos/engine/` (or `topos/enrichment/engine/` per architecture doc): `__init__.py`, `tasks.py`, `intake.py`, `validator.py`, `router.py`, `result_formatter.py`, `engine.py`.
  - **Intake:** accept task, normalize defaults (e.g. execution.mode, model_request.provider).
  - **Validator:** validate required fields (id, type, model_request); stub: always valid or reject only obviously invalid (e.g. missing id).
  - **Router:** stub that returns a placeholder or “no backend” for provider; no real inference yet.
  - **Result formatter:** build ProcessingResult from (raw_output, execution_meta, task_id, status, error).
  - **Engine.run(task):** intake → validate → route → (stub: no inference, return formatted “not implemented” or mock result).
- **No caller changes** — ingest_helpers, api/enrichment, jobs unchanged.

---

## Acceptance criteria

| ID | Criterion | How to verify |
|----|-----------|----------------|
| AC-1.1 | ProcessingTask and ProcessingResult are defined and JSON-serializable. | Instantiate from dict/JSON; serialize back; assert round-trip. |
| AC-1.2 | ProcessingTask includes all PRD §6.1 fields; ProcessingResult includes all PRD §6.2 fields. | Unit test: assert field names and types (or schema). |
| AC-1.3 | Engine.run(task) returns a ProcessingResult (not an exception) for a valid minimal task. | Unit test: build minimal task, call Engine.run(), assert result.task_id, result.status. |
| AC-1.4 | Engine.run(task) returns a structured error result for an invalid task (e.g. missing id or type). | Unit test: invalid task → result.status indicates failure, result.error set. |
| AC-1.5 | Engine package is importable; no direct dependency on transformers/torch in engine core. | Import engine; run test without HF/Ollama installed (router returns stub). |

---

## Implementation notes

- **Location:** Prefer `topos/engine/` for a clear Engine boundary (see [../ARCHITECTURE_MAPPING.md](../ARCHITECTURE_MAPPING.md)); alternatively `topos/enrichment/engine/`.
- **Stub router:** Return a small adapter stub that has `run_inference` returning a fixed dict (e.g. `{"status": "stub"}`) so the pipeline (intake → validate → route → format) is exercised without real backends.
- **created_at:** Default to `datetime.utcnow().isoformat()` or equivalent when not provided.

---

## Tests

| Test | Description |
|------|-------------|
| Task round-trip | Build ProcessingTask from dict; serialize to JSON/dict; deserialize; assert equality of key fields. |
| Result round-trip | Same for ProcessingResult. |
| Engine run valid minimal task | Minimal task (id, type=enrichment, subtype=url_classification, input={url, title}, model_request.provider=huggingface); Engine.run() returns ProcessingResult with task_id and status (e.g. completed or stub). |
| Engine run invalid task | Task with missing id or type; Engine.run() returns result with status failed/error and result.error populated. |
| Engine run triggers intake and formatter | Run valid task; assert result has provenance or execution_meta if formatter sets them (smoke test for pipeline). |
| No HF/torch in engine core | In a clean env or mock, import engine and run; no ImportError for transformers/torch from engine/engine.py or engine/router.py. |

---

## Definition of done

- [ ] `ProcessingTask` and `ProcessingResult` defined in `engine/tasks.py` (or equivalent) with PRD-aligned fields.
- [ ] Engine package has intake, validator, router (stub), result_formatter, and engine.run() wired.
- [ ] Engine.run(valid_task) returns ProcessingResult; Engine.run(invalid_task) returns error result.
- [ ] All acceptance criteria met.
- [ ] Tests above added and passing (or documented as manual if test infra is added in a later sprint).
