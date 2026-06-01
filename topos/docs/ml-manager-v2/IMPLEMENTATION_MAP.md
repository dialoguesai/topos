# PRD → Codebase Implementation Map

This document maps each major section of the **Topos Engine** PRD to existing code paths and required new work in `topos/`.

---

## 1. Overview & Core Concept

| PRD concept | Existing code | New / change |
|-------------|---------------|--------------|
| Engine = unified runtime for enrichments, transformations, derivations, queries | `EnrichmentOrchestrator` + jobs; no unified “Engine” facade | Introduce **Engine** facade that owns task intake, routing, execution, and results. Orchestrator becomes one consumer of Engine. |
| All work as **tasks** (purpose, input, model, execution mode, output) | Jobs are class-based (`BaseEnrichmentJob`, raw jobs); no first-class task type | Add **ProcessingTask** / **ProcessingResult** types (see §6); jobs emit or consume these. |

**Files to touch:** New `topos/engine/` (or `topos/enrichment/engine.py` + task types); `topos/enrichment/orchestrator.py` to call Engine instead of jobs directly (or jobs delegate to Engine).

---

## 2. Functional Scope

### 2.1 Task Types

| Task type | Current implementation | Gap |
|-----------|------------------------|-----|
| **A. Enrichment** | Raw: `ingest_helpers._run_browser_url_classification_enrichment` + `website_classifier`; canonical: `EnrichmentOrchestrator.run_canonical` + `Emo27Job`, `EntitiesJob`, `TopicsJob`, `SentimentJob`, `EmbeddingsJob` | Unify under Engine task type `enrichment`; jobs become task producers/executors. |
| **B. Transformation** | Not implemented (Fisher mentioned in schema/sources) | Add task type `transformation`; Fisher as subtype or post-step (see §10). |
| **C. Derivation** | Not implemented (beliefs, profile inference) | Add task type `derivation`; future. |
| **D. Query** | Ad-hoc (e.g. summarization via services/llm); no standard task contract | Add task type `query`; user-triggered tasks via Engine. |
| **E. Agent** | Out of scope for V1 | — |

**Relevant files:**  
- Enrichment: `topos/enrichment/jobs/`, `topos/ingestion/ingest_helpers.py`, `topos/enrichment/website_classifier.py`, `topos/enrichment/jobs/canonical/emo_27_job.py`.  
- Query/LLM: `topos/services/llm/`, `topos/services/llm/openai.py`.  
- Sources: `topos/sources/definitions.py` (`raw_enrichment_jobs`, `canonical_enrichment_jobs`) already define “what” to run; can feed task creation.

### 2.2 Execution Modes

| Mode | Current support | Gap |
|------|-----------------|-----|
| **Synchronous** | Orchestrator runs jobs in process; API `process_enrichment` is blocking until done | Formalize as `execution.mode: "sync"`; Engine.run() blocks. |
| **Asynchronous (queued)** | None | Add queue; Engine.submit() returns TaskHandle; worker processes queue. |
| **Event-driven (on write)** | Raw enrichment on ingest: `ingest_helpers._run_browser_url_classification_enrichment` after normalized write | Formalize as “on write” trigger; create task and submit to Engine (sync or async). |
| **Batch** | Canonical enrichment over historical messages (`_find_unprocessed_messages` + `run_canonical`) | Formalize as batch execution mode; batch_key / model-aware batching (PRD §7.2). |

**Relevant files:**  
- Ingest write path: `topos/ingestion/ingest_helpers.py`.  
- Canonical batch: `topos/api/enrichment.py` (`_process_enrichment_core`, `_find_unprocessed_messages`), `topos/enrichment/orchestrator.py`.  
- New: queue, scheduler (see ARCHITECTURE_MAPPING.md).

---

## 3. Engine Architecture (PRD §4)

| Component | Current location | Notes |
|-----------|------------------|--------|
| **Task Intake** | N/A | New: accept `ProcessingTask` (from API, ingest pipeline, or internal). |
| **Task Validator** | N/A | New: validate task schema, required fields, model availability. |
| **Task Router** | N/A | New: route by task type and model_request to correct backend. |
| **Queue Manager** | N/A | New: queue for async tasks; optional persistence in V1. |
| **Scheduler** | N/A | New: priority (PRD §7.1), model-aware batching (§7.2). |
| **Model Loader / Cache** | Stub: `enrichment/models/manager.py` (`_loaded`); no cache policy | Extend to load/cache by model spec; integrate with registry. |
| **Backend Adapters** | Direct HF in `website_classifier.py` and `emo_27_job.py` | New: `BackendAdapter` protocol; Ollama + HuggingFace adapters. |
| **Result Formatter** | Ad-hoc per job (e.g. emo_27 dict, website_classifier dict) | Standardize to **ProcessingResult** (PRD §6.2). |
| **Execution Logger** | Logging only | Optional: structured execution log (duration, model, cache_hit) in result and/or observability. |
| **Transport Layer** | N/A | Future: remote Engine node. |

**Files:**  
- New: `topos/engine/` (or under `topos/enrichment/engine/`) for Engine, validator, router, queue, scheduler.  
- Adapters: e.g. `topos/engine/backends/ollama.py`, `topos/engine/backends/huggingface.py` (or `topos/enrichment/backends/`).  
- Registry: `topos/enrichment/models/registry.py` — extend for Ollama, task→model resolution.

---

## 4. Engine Interface (PRD §5)

| Interface | Current | New |
|-----------|---------|-----|
| `run(task: ProcessingTask) -> ProcessingResult` | No single entry point; orchestrator calls `job.enrich()` or ingest calls `classify_url()` | Engine.run() validates, routes, runs (sync), formats result. |
| `submit(task: ProcessingTask) -> TaskHandle` | N/A | Engine.submit() enqueues; returns handle for status/result later. |

**Location:** New Engine class in `topos/engine/` (or `topos/enrichment/engine.py`). All callers (orchestrator, ingest_helpers, API) eventually go through Engine.

---

## 5. Task Contract (PRD §6)

| Contract | Current | New |
|----------|---------|-----|
| **ProcessingTask** | Job-specific inputs (e.g. list of canonical messages); no standard id, type, model_request, execution | Introduce dataclass or Pydantic model matching PRD §6.1 (id, type, subtype, source_id, record_ids, input, model_request, execution, options, requested_by, created_at). |
| **ProcessingResult** | Job-specific dicts (e.g. emotion_label, confidence, model) | Introduce type matching PRD §6.2 (task_id, status, output, output_type, confidence, provenance, execution_meta, error). |

**Location:** e.g. `topos/engine/tasks.py` or `topos/enrichment/tasks.py` — define ProcessingTask, ProcessingResult, and helpers to convert from/to current job inputs/outputs.

---

## 6. Queueing and Scheduling (PRD §7)

| Requirement | Current | New |
|-------------|---------|-----|
| Priority rules | None (orchestrator runs jobs in fixed order) | Implement priority ordering: sync user > async user > write-event > batch > background. |
| Model-aware batching | None | Group tasks by model/batch_key; run same model together to reduce load overhead. |
| Fairness | N/A | Prevent long batches from starving user tasks; allow small tasks to interrupt. |
| Queue persistence | N/A | V1 optional; V2 required. |

**Location:** New queue + scheduler in `topos/engine/` (queue_manager.py, scheduler.py or combined).

---

## 7. Model Runtime Integration (PRD §8)

| Item | Current | New |
|------|---------|-----|
| **Ollama** | Not used | New Ollama adapter implementing BackendAdapter (load_model, run_inference, unload_model). |
| **HuggingFace** | Used directly in `website_classifier.py` (pipeline) and `emo_27_job.py` (AutoModel + tokenizer) | HuggingFace adapter that wraps pipeline / model loading; model name from registry or task. |
| **Backend interface** | N/A | Define `BackendAdapter` protocol; Engine selects adapter by `model_request.provider`. |
| **Model routing** | Registry has `task_name`, `huggingface_path`, `get_preferred_model` | Extend registry for `provider` (ollama/huggingface), model id; per-task override; fallback. |

**Files:**  
- `topos/enrichment/models/registry.py` — add provider, ollama model name.  
- New backends: `topos/engine/backends/` (or `topos/enrichment/backends/`).  
- Manager: `topos/enrichment/models/manager.py` — use adapters and registry.

---

## 8. Source Integration (PRD §9)

| Requirement | Current | New |
|-------------|---------|-----|
| Source-defined enrichments | `DataSourceDefinition.raw_enrichment_jobs`, `canonical_enrichment_jobs` | Keep; use to **create** Engine tasks (enrichment type, source_id, record_ids). |
| Trigger conditions | Write: in ingest after normalized write; batch: API/backfill; manual: API | Map to execution mode + requested_by.origin (e.g. write_event, batch, manual). |
| Required models | Not per-source | Optional: per-source model override in definition or registry. |

**Files:**  
- `topos/sources/definitions.py` — optional fields for preferred model or task config.  
- `topos/ingestion/ingest_helpers.py` — create ProcessingTask for url_classification, submit to Engine.  
- `topos/api/enrichment.py` — create ProcessingTasks for canonical jobs from source config, pass to Engine.

---

## 9. Fisher Information Control (PRD §10)

| Requirement | Current | New |
|-------------|---------|-----|
| Transformation at output time | Schema/sensitivity tiers in definitions | Engine supports Fisher as task subtype **or** as post-processing step on result. |
| Global and per–data type control | `filter_tier_kind`, `default_filter_tiers` on DataSourceDefinition | Same; Engine applies when formatting output or in a dedicated Fisher step. |

**Files:**  
- New: Fisher as transformation task or post-process in Engine result formatter.  
- `topos/sources/definitions.py` — already has filter tiers; Engine reads for output filtering.

---

## 10. Belief Derivation (PRD §11)

| Requirement | Current | New |
|-------------|---------|-----|
| Beliefs at query time | Not implemented | Derivation tasks; result includes confidence, timestamp, provenance. |
| User edits preserved | N/A | Engine must not overwrite user-edited belief nodes; store/merge logic outside Engine. |

**Files:**  
- Future: derivation task type; belief store/merge in analytics or dedicated module; Engine only runs inference.

---

## 11. Configuration (PRD §13)

| Item | Current | New |
|------|---------|-----|
| Local config | `config/settings.py` (env-based) | Add engine-related settings: default models per task type, backend preferences, queue limits. |
| Default models per task type | Hardcoded in jobs (e.g. EMO_27_MODEL_PATH, URL_CLASSIFICATION_MODEL) | From registry + config. |
| Queue settings | N/A | Queue size, worker count, optional persistence path. |

**Files:**  
- `topos/config/settings.py` — new fields or separate `engine.yaml` / env vars.  
- Registry + Engine use config for defaults.

---

## 12. Observability (PRD §14)

| Metric | Current | New |
|--------|---------|-----|
| Task counts, latency per model, queue wait, errors, cache usage | `observability/metrics.py` is a stub | Implement recording in Engine: task_completed, task_failed, inference_duration_ms, queue_wait_ms, cache_hit. |

**Files:**  
- `topos/observability/metrics.py` — real implementation or pluggable backend.  
- Engine calls metrics in router, queue, and after inference.

---

## 13. Error Handling (PRD §15)

| Error | Current | New |
|-------|---------|-----|
| Model not found, backend unavailable, inference failure, invalid input, queue overflow | Exceptions propagate; some caught in orchestrator/API | Engine returns structured **ProcessingResult** with status (e.g. failed), error message/code; no uncaught inference errors. |

**Files:**  
- Engine validator and adapters return structured errors; Result formatter sets `result.error`.

---

## 14. Security (PRD §16)

Engine trusted with raw data; outputs respect filtering (e.g. Fisher). No change to current trust model; apply filters in result formatter or when writing to stores.

---

## 15. V1 Scope Checklist

- **Must include:** Engine abstraction, task system, queue + scheduler, model adapters (Ollama + HF), local inference, source-based enrichments, structured outputs.  
- **Not included:** DAG workflows, full agent system, distributed orchestration, plugin marketplace.

See **MIGRATION_AND_GAPS.md** for ordered migration steps and gap closure.
