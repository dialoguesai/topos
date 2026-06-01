# Migration Steps and Gap Analysis

Ordered migration path from current codebase to the Topos Engine (V1), plus a concise gap list and V1 scope checklist.

---

## 1. Migration order (high level)

1. **Define task contract** — Add `ProcessingTask` and `ProcessingResult` without changing callers.
2. **Add Engine facade and sync run()** — Single entry point that validates, routes, runs one task, returns result.
3. **Implement BackendAdapter and HuggingFace adapter** — Move current HF usage behind adapter; Engine uses it.
4. **Migrate URL classification to Engine** — Ingest and API build task, call Engine.run(); remove direct `website_classifier` from pipeline.
5. **Extend registry and add Ollama adapter** — Registry supports provider + ollama model; Ollama adapter implements BackendAdapter.
6. **Migrate emo_27 to Engine** — Emo27Job calls Engine for inference; no direct transformers/torch in job.
7. **Add queue and submit()** — Optional async path; TaskHandle for status/result.
8. **Add scheduler and model-aware batching** — Priority and batching per PRD §7.
9. **Observability and config** — Metrics, engine config, optional execution logger.

---

## 2. Detailed migration steps

### Step 1: Task contract (no behavioral change)

- **Add** `topos/engine/tasks.py` (or `topos/enrichment/tasks.py`):
  - `ProcessingTask`: id, type, subtype, source_id, record_ids, input, model_request, execution, options, requested_by, created_at (match PRD §6.1).
  - `ProcessingResult`: task_id, status, output, output_type, confidence, provenance, execution_meta, error (match PRD §6.2).
- Use Pydantic or dataclass; keep JSON-serializable for future transport.
- **No callers yet**; optional helper to build task from (source_id, job_name, record_ids, input).

**Files created:** `engine/tasks.py` (or `enrichment/engine/tasks.py`).

---

### Step 2: Engine facade and sync run()

- **Add** `topos/engine/` package:
  - `engine/__init__.py`: expose `Engine`, `run(task) -> ProcessingResult`.
  - `engine/intake.py`: accept task, normalize (defaults for execution, model_request).
  - `engine/validator.py`: validate required fields; check model_request.provider and model name (stub: always valid for now).
  - `engine/router.py`: return backend adapter by provider (stub: only "huggingface").
  - `engine/result_formatter.py`: build ProcessingResult from raw output + execution_meta.
  - `engine/engine.py`: run = intake → validate → route → load model (next step) → run_inference → format result.
- **Model loading**: temporarily keep “load on first use” inside adapter; Engine calls adapter.run_inference(payload, config) where adapter owns load/cache.
- **No migration of callers yet**; add a single test or script that builds a task and calls Engine.run().

**Files created:** `engine/engine.py`, `engine/intake.py`, `engine/validator.py`, `engine/router.py`, `engine/result_formatter.py`.

---

### Step 3: BackendAdapter and HuggingFace adapter

- **Add** `topos/engine/backends/base.py`: define `BackendAdapter` protocol with `load_model`, `run_inference`, `unload_model`.
- **Add** `topos/engine/backends/huggingface.py`:
  - Implement BackendAdapter.
  - For task subtype `url_classification`: use same logic as current `WebsiteUrlClassifier` (pipeline text-classification, model from task or registry).
  - For task subtype `emotion_classification` (or emo_27): use same logic as current `Emo27Job` (AutoModel, tokenizer, softmax, top-k).
  - Model name from task.model_request.model or registry.get_model_for_task(...).
- **Refactor** Engine to use Router → get BackendAdapter(provider) → adapter.run_inference(task.input, config). No direct HF imports in Engine core.
- **Registry**: extend with `get_model_for_task(task_type, subtype)` returning model id/spec; prefer registry over hardcoded model names in adapter.

**Files created:** `engine/backends/base.py`, `engine/backends/huggingface.py`.  
**Files to touch:** `engine/router.py` (return HF adapter), `enrichment/models/registry.py` (get_model_for_task if needed).

---

### Step 4: Migrate URL classification to Engine

- **ingest_helpers.py**:
  - In `_run_browser_url_classification_enrichment`: build `ProcessingTask` (type=enrichment, subtype=url_classification, source_id, record_ids, input={url, title}, model_request from registry or default).
  - Call `Engine.run(task)` (or await if Engine is async).
  - Map `ProcessingResult.output` to current `write_browser_url_classification` parameters (category, confidence, model_name).
  - Remove direct import of `classify_url` from `website_classifier` for this path.
- **api/enrichment.py**:
  - In `_test_browser_visits_url_classification`: build task, call Engine.run(task), return result in same shape as current API.
  - In `_backfill_browser_visits_url_classification`: for each row, build task, Engine.run(task), write result; remove direct `classify_url`.
- **Deprecate or keep** `website_classifier.py` as thin wrapper that builds task and calls Engine (for any remaining direct callers) until removed.

**Files modified:** `ingestion/ingest_helpers.py`, `api/enrichment.py`.  
**Files optionally modified:** `enrichment/website_classifier.py` (wrapper or deprecation).

---

### Step 5: Registry and Ollama adapter

- **Registry** (`enrichment/models/registry.py`):
  - Add `provider: Literal["ollama", "huggingface"]`, `ollama_model: Optional[str]`.
  - `register_model(..., provider=..., ollama_model=...)`.
  - `get_model_for_task(task_type, subtype, source_id=None)` → preferred or first matching model spec.
- **Config** (`config/settings.py`): add `engine_ollama_base_url`, `engine_default_provider`, optional `engine_default_models` (task_type → model id).
- **Add** `topos/engine/backends/ollama.py`: implement BackendAdapter for Ollama (HTTP API: load/generate); run_inference maps task input to Ollama prompt/format.
- **Router**: when `model_request.provider == "ollama"` (or from registry), return Ollama adapter; else HuggingFace.

**Files created:** `engine/backends/ollama.py`.  
**Files modified:** `enrichment/models/registry.py`, `config/settings.py`, `engine/router.py`.

---

### Step 6: Migrate emo_27 to Engine

- **Emo27Job** (`enrichment/jobs/canonical/emo_27_job.py`):
  - Remove `_load_model`, `_classify_emotion`, and direct `transformers`/`torch` imports.
  - In `enrich()`: for each message (or batch), build `ProcessingTask` (enrichment, subtype=emotion_classification or emo_27, record_ids=[message_id], input={text: content}, model_request from registry).
  - Call `Engine.run(task)` (or batch of tasks if Engine supports it); map ProcessingResult to current result dict (message_id, emotion_label, confidence, all_emotions, model).
  - Keep same derived table and orchestrator flow; only inference path goes through Engine.
- **Registry**: ensure emo_27 or emotion_classification has a default model (current HF model) and optional Ollama override.

**Files modified:** `enrichment/jobs/canonical/emo_27_job.py`.  
**Files touched:** `enrichment/models/registry.py` (default for emotion_classification).

---

### Step 7: Queue and submit()

- **Add** `engine/queue_manager.py`: in-memory queue (e.g. asyncio.Queue or queue.Queue); enqueue(task), dequeue() for worker.
- **Engine.submit(task)** → validate → enqueue(task) → return TaskHandle (task_id, optional status future).
- **Worker** (same process or background thread): dequeue → run(task) → store result in handle or in-memory result store.
- **API**: optional endpoint “submit enrichment task” that returns task_id; optional “get task status/result” by task_id.
- **V1**: persistence optional; restart loses queue.

**Files created:** `engine/queue_manager.py`.  
**Files modified:** `engine/engine.py` (submit), `engine/tasks.py` (TaskHandle if needed), optionally `api/enrichment.py`.

---

### Step 8: Scheduler and model-aware batching

- **Add** `engine/scheduler.py`:
  - Priority: sync user > async user > write-event > batch > background (PRD §7.1).
  - Model-aware batching: group dequeued tasks by (provider, model) or batch_key; run same model in a batch to reduce load overhead (PRD §7.2).
  - Fairness: cap batch size or time so user tasks can be scheduled soon (PRD §7.3).
- **Queue manager** or Engine: when dequeueing, use scheduler to select next task(s) instead of FIFO.
- **Integration**: orchestrator or API can submit multiple tasks; scheduler reorders and batches.

**Files created:** `engine/scheduler.py`.  
**Files modified:** `engine/queue_manager.py` and/or `engine/engine.py`.

---

### Step 9: Observability and config

- **Observability** (`observability/metrics.py`): implement or pluggable backend; record task_completed, task_failed, inference_duration_ms, queue_wait_ms, cache_hit.
- **Engine**: after run/submit and after inference, call metrics; optional execution_meta in result.
- **Config**: consolidate engine defaults (queue size, worker count, default models per task type, backend preferences) in `config/settings.py` or loaded YAML.

**Files modified:** `observability/metrics.py`, `engine/engine.py`, `config/settings.py`.

---

## 3. Gap summary

| Gap | Severity | Addressed by |
|-----|----------|--------------|
| No ProcessingTask / ProcessingResult | High | Step 1 |
| No single Engine entry point | High | Step 2 |
| Direct HF in website_classifier and emo_27 | High | Steps 3, 4, 6 |
| No Ollama support | Medium | Step 5 |
| No async queue or submit() | Medium | Step 7 |
| No priority or model-aware batching | Medium | Step 8 |
| No structured observability | Low | Step 9 |
| Fisher as transformation | Low | Post-V1; Result formatter or transformation task |
| Belief derivation | Future | Derivation task type + store |
| Remote Engine (transport) | Future | Same run/submit contract over network |

---

## 4. V1 scope checklist (PRD §17)

**Must include**

- [ ] Engine abstraction (run, optional submit)
- [ ] Task system (ProcessingTask, ProcessingResult)
- [ ] Queue + scheduler (priority, model-aware batching)
- [ ] Model adapters (Ollama + HuggingFace)
- [ ] Local inference only
- [ ] Source-based enrichments (write-event + canonical batch via Engine)
- [ ] Structured outputs (ProcessingResult)

**Not included in V1**

- DAG workflows
- Full agent system
- Distributed compute orchestration
- Plugin marketplace

---

## 5. Rollback and compatibility

- **Feature flag**: e.g. `USE_ENGINE=true` so ingest and API can switch between “direct classifier/job” and “Engine.run(task)”. Allows gradual rollout and rollback.
- **Dual-write**: not required if Engine path is tested; optional compare result of Engine vs direct path for one sprint.
- **Registry**: extend without breaking existing `register_model` callers; add optional provider/ollama_model; default provider=huggingface for existing registrations.

This gives a clear sequence to implement the PRD in the existing codebase and close the main gaps.
