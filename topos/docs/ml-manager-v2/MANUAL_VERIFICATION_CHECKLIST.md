# Topos Engine (V1) — Manual Verification Checklist

Use this checklist after implementing the Engine sprints to confirm the new behavior end-to-end. References the [sprints](./sprints/) and [implementation docs](./IMPLEMENTATION_MAP.md).

---

## Prerequisites

- [ ] Python env with `topos` package on path (e.g. from `topos-control-plane-test` root).
- [ ] Optional: `torch` and `transformers` installed for real HuggingFace inference (`pip install torch transformers` or project `engine` extra).
- [ ] Optional: Ollama installed and running (`ollama serve`) for Ollama adapter tests.
- [ ] Database available if testing ingest/API (e.g. `TOPOS_KEY` set for API).

---

## 1. Engine and task contract (Sprint 01)

- [ ] **Import Engine**  
  `from topos.engine import Engine, ProcessingTask, ProcessingResult, ModelRequest` runs without error.
- [ ] **Run sync**  
  Build a minimal `ProcessingTask` (id, type=`enrichment`, input=`{}`, model_request=ModelRequest(provider=`stub`)). Call `Engine().run(task)`. Result is `ProcessingResult` with `status="completed"` and `output["status"]=="stub"`.
- [ ] **Invalid task**  
  Task with empty `id` or empty `type` → `Engine.run()` returns result with `status="failed"` and `error` set (no exception).

---

## 2. HuggingFace adapter and URL classification (Sprints 02–03)

- [ ] **URL classification via Engine**  
  With `torch`/`transformers` installed: build task with `subtype="url_classification"`, `input={"url": "https://www.nytimes.com", "title": "NYT"}`, `model_request=ModelRequest(provider="huggingface")`. `Engine().run(task)` returns `status="completed"` and `output` has `category` and `confidence`.
- [ ] **Ingest path**  
  Trigger browser visit ingest so that `_run_browser_url_classification_enrichment` runs. Confirm a row is written to `browser_url_classification` (or equivalent) with category/confidence. No direct use of `classify_url` in that path (see [SPRINT_03](./sprints/SPRINT_03_MIGRATE_URL_CLASSIFICATION_TO_ENGINE.md)).
- [ ] **Test endpoint**  
  `POST /sources/browser_visits/enrichments/url_classification/test` with body `{"url": "https://example.com", "title": "Example"}`. Response has `status`, `input`, `output` with `category` and `confidence`.
- [ ] **Backfill**  
  Call backfill for browser_visits url_classification (with limit). Rows written to URL classification table; no errors in logs.
- [ ] **website_classifier wrapper**  
  `from topos.enrichment.website_classifier import classify_url` then `classify_url("https://example.com")` returns dict with `category`, `confidence`, `model` (same shape as before).

---

## 3. Registry, Ollama, and emo_27 (Sprint 04)

- [ ] **Registry provider**  
  Register a model with `provider="ollama"`, `ollama_model="llama3.1"`. `get_model_for_task("enrichment", "emotion_classification")` returns spec with `provider` and `ollama_model`.
- [ ] **Router**  
  Task with `model_request=ModelRequest(provider="ollama")` → router returns Ollama adapter; provider `huggingface` → HuggingFace adapter.
- [ ] **Emo27Job**  
  `topos.enrichment.jobs.canonical.emo_27_job` has no `import transformers` or `import torch`; it uses `Engine().run(task)` for inference.
- [ ] **Canonical enrichment**  
  Run canonical enrichment (orchestrator) with at least Emo27Job on a few messages. Table `message_emotions` gets rows with `message_id`, `emotion_label`, `confidence`, `all_emotions`, `model`. (Requires torch/transformers for HF path.)
- [ ] **Config**  
  `topos.config.settings.settings` has `engine_ollama_base_url` and `engine_default_provider` (env or defaults).

---

## 4. Queue and submit (Sprint 05)

- [ ] **Submit**  
  `handle = Engine().submit(valid_task)`. `handle` is not None; `handle.task_id` matches task id; `handle.get_status()` is `"pending"`.
- [ ] **Worker**  
  After `engine.run_worker_once()`, same handle’s `get_status()` is `"completed"` and `get_result()` returns a `ProcessingResult`.
- [ ] **Run unchanged**  
  `Engine().run(task)` still works synchronously and returns the same shape as before (no regression).

---

## 5. Observability (Sprint 07)

- [ ] **Metrics**  
  After a successful `Engine().run(task)`, `topos.observability.metrics.get_metric("engine.task_completed")` is incremented (e.g. ≥ 1). After a failed run, `engine.task_failed` is incremented.
- [ ] **Duration**  
  After a completed run, `engine.inference_duration_ms` has been incremented by the run’s duration.

---

## 6. Automated tests

From repo root (e.g. `topos-control-plane-test`):

- [ ] **Engine tests**  
  `pytest tests/topos/test_engine_tasks.py tests/topos/test_engine_facade.py tests/topos/test_engine_sprint03_url_migration.py tests/topos/test_engine_sprint04_registry_ollama_emo27.py -v` — all pass (Sprint 02 HF tests may be skipped if torch not installed).
- [ ] **No regressions**  
  Other existing topos tests that you care about still pass (e.g. ingestion, API, enrichment orchestrator if env has deps).

---

## 7. Optional / future

- [ ] **Ollama**  
  With Ollama running: task with `provider="ollama"`, `subtype="emotion_classification"` → Engine returns a result (may be completed or failed depending on model/prompt).
- [ ] **Scheduler (Sprint 06)**  
  If implemented: tasks with different priorities are processed in priority order; model-aware batching groups by model.
- [ ] **API submit/status**  
  If endpoints were added: POST to submit returns `task_id`; GET status/result by `task_id` returns expected status and result.

---

## Sign-off

- [ ] All items above that apply to your deployment are verified.
- [ ] Any failures or gaps are documented (e.g. missing torch, Ollama not installed).

**Reference:** [Implementation map](./IMPLEMENTATION_MAP.md), [Architecture](./ARCHITECTURE_MAPPING.md), [Migration and gaps](./MIGRATION_AND_GAPS.md), [Sprints](./sprints/README.md).
