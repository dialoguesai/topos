# Sprint 07 — Observability and configuration

**Topos Engine V1**

---

## Objective

Add **observability** (task counts, latency per model, queue wait times, error rates, cache usage) and consolidate **Engine configuration** (default models, backend preferences, queue settings) so the Engine is production-ready and operable.

**Plan refs:** [../MIGRATION_AND_GAPS.md](../MIGRATION_AND_GAPS.md) Step 9; [../IMPLEMENTATION_MAP.md](../IMPLEMENTATION_MAP.md) §12 (Observability), §13 (Configuration); [../ARCHITECTURE_MAPPING.md](../ARCHITECTURE_MAPPING.md) (Execution Logger, Config).

---

## Scope

- **Observability**
  - Implement or extend `topos/observability/metrics.py`: record task_completed, task_failed, inference_duration_ms, queue_wait_ms, cache_hit (and optionally model name, task type). Backend can be in-memory counters, Prometheus, or logging; document choice.
  - Engine: after run() and after submit/worker completion, call metrics (task count, duration, cache hit from execution_meta). For submit path, record queue_wait_ms when task is dequeued.
  - Optional: execution_meta in ProcessingResult already has duration_ms, provider, model; ensure it is populated and can be used for metrics.
- **Configuration**
  - Consolidate in `config/settings.py` (or loaded YAML): engine_default_provider, engine_ollama_base_url, engine_queue_max_size, engine_worker_count (if applicable), optional engine_default_models (task_type/subtype → model id), optional engine_queue_persistence_path (for future).
  - Engine and queue/scheduler read from config; document env vars or config file keys.
- **Execution logger (optional)**
  - Optional: structured log (e.g. JSON lines) per task: task_id, model, duration_ms, status, cache_hit. File or stdout; document location.

---

## Acceptance criteria

| ID | Criterion | How to verify |
|----|-----------|----------------|
| AC-7.1 | At least one of: task_completed and task_failed are recorded when a task finishes. | Unit test: run task; assert metric recorded (e.g. counter incremented or log line present). |
| AC-7.2 | inference_duration_ms (or equivalent) is recorded with model/provider dimension. | Unit test: run task; assert duration metric recorded; optional: assert model/provider tagged. |
| AC-7.3 | queue_wait_ms is recorded when a submitted task is dequeued and run. | Integration test: submit task; worker runs it; assert queue_wait_ms recorded (or documented as best-effort). |
| AC-7.4 | Config values (engine_queue_max_size, engine_ollama_base_url, etc.) are loaded and used by Engine/queue/adapter. | Unit test: set config (env or mock); assert Engine or queue reads correct value. |
| AC-7.5 | Document how to enable/configure metrics and where to find logs (if any). | README or sprint doc: list of metrics, config keys, and log location. |

---

## Implementation notes

- **Metrics backend:** If the project has no existing metrics system, start with simple counters/timers in memory and optional logging; Prometheus or similar can be added later.
- **Config precedence:** Env vars override config file; document in settings or engine config doc.

---

## Tests

| Test | Description |
|------|-------------|
| Task completed metric | Engine.run(task) succeeds; assert task_completed (or equivalent) incremented or logged. |
| Task failed metric | Engine.run(invalid_task) returns error result; assert task_failed recorded. |
| Duration metric | Run task; assert inference_duration_ms (or duration) recorded; value reasonable (e.g. > 0). |
| Queue wait metric | Submit task; worker runs after delay; assert queue_wait_ms present in metrics or execution_meta. |
| Config loading | Load settings with env ENGINE_QUEUE_MAX_SIZE=100; assert engine_queue_max_size == 100 (or equivalent). |
| Config used by Engine | Set engine_ollama_base_url; Ollama adapter uses it for HTTP base URL (mock or integration). |
| Documentation | README or docs list metrics and config keys; reviewer can find them. |

---

## Definition of done

- [ ] Metrics recorded for task_completed, task_failed, inference_duration_ms; optional queue_wait_ms and cache_hit.
- [ ] Engine and components read config (queue size, ollama URL, default provider, etc.).
- [ ] Config keys and env vars documented.
- [ ] Optional: execution logger writes structured log per task.
- [ ] All acceptance criteria met.
- [ ] Tests above added and passing.
