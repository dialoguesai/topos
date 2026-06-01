# Engine Architecture → Code Mapping

Maps the PRD’s high-level Engine architecture to **current** modules and **proposed** new modules under `topos/`.

---

## PRD Diagram (from §4.1)

```text
Topos Engine
├── Task Intake
├── Task Validator
├── Task Router
├── Queue Manager
├── Scheduler
├── Model Loader / Cache
├── Backend Adapters
│   ├── Ollama Adapter
│   └── HuggingFace Adapter
├── Result Formatter
├── Execution Logger
└── Transport Layer (future remote mode)
```

---

## Current layout (relevant to Engine)

```text
topos/
├── config/
│   └── settings.py              # Env-based config; add engine defaults
├── enrichment/
│   ├── derived_tables.py        # DerivedTablesManager — writes enrichment tables
│   ├── jobs/
│   │   ├── __init__.py          # CANONICAL_JOBS, RAW_JOBS
│   │   ├── base.py              # BaseEnrichmentJob, EnrichmentResult
│   │   ├── canonical/           # Emo27Job, EntitiesJob, TopicsJob, SentimentJob, EmbeddingsJob
│   │   └── raw/                 # AttachmentsJob, ToolCallsJob, LanguageJob, TimeNormalizationJob
│   ├── models/
│   │   ├── manager.py           # ModelManager stub (_loaded)
│   │   ├── registry.py          # ModelRegistry (task_name, huggingface_path, get_preferred_model)
│   │   └── versioning.py
│   ├── orchestrator.py          # EnrichmentOrchestrator — runs raw/canonical jobs
│   ├── processor.py             # EnrichmentProcessor — thin wrapper over orchestrator
│   ├── progress_bar.py
│   └── website_classifier.py    # WebsiteUrlClassifier — direct HF pipeline
├── ingestion/
│   └── ingest_helpers.py        # _run_browser_url_classification_enrichment (write-event)
├── api/
│   └── enrichment.py            # process_enrichment, backfill, test, status
├── sources/
│   └── definitions.py           # DataSourceDefinition (raw/canonical_enrichment_jobs)
├── storage/
│   └── enrichment/              # raw_enrichment_store, canonical_enrichment_store
├── observability/
│   └── metrics.py               # Stub
├── lineage/
│   └── provenance.py            # Stub
└── services/
    └── llm/                     # LLMService protocol; openai impl
```

---

## Proposed layout (Engine as central facade)

Two options: **A)** dedicated `engine/` package, **B)** Engine under `enrichment/` with submodules. Option A keeps “Engine” as the product boundary and makes remote Engine easier later.

### Option A: Dedicated `topos/engine/`

```text
topos/
├── engine/
│   ├── __init__.py              # Engine, run(), submit()
│   ├── tasks.py                 # ProcessingTask, ProcessingResult (Pydantic/dataclass)
│   ├── intake.py                # Task Intake — accept task, normalize
│   ├── validator.py             # Task Validator — schema, model exists
│   ├── router.py                # Task Router — type + model_request → backend
│   ├── queue_manager.py         # Queue Manager — enqueue, dequeue, optional persistence
│   ├── scheduler.py             # Scheduler — priority, model-aware batching
│   ├── model_loader.py          # Model Loader / Cache — uses registry + backends
│   ├── result_formatter.py      # Result Formatter — to ProcessingResult
│   ├── execution_logger.py     # Optional structured log
│   └── backends/
│       ├── __init__.py          # BackendAdapter protocol, get_adapter(provider)
│       ├── base.py              # BackendAdapter protocol
│       ├── ollama.py            # Ollama adapter
│       └── huggingface.py       # HuggingFace adapter
├── enrichment/
│   ├── orchestrator.py         # Uses Engine.run() for each job (or jobs emit tasks)
│   ├── jobs/                    # Jobs build ProcessingTask and call Engine.run(), or Engine calls jobs
│   ├── models/
│   │   ├── registry.py          # Extended: provider, ollama_model, task→model resolution
│   │   └── manager.py           # Uses engine backends + registry
│   └── website_classifier.py    # Deprecated or thin wrapper → Engine
├── ingestion/
│   └── ingest_helpers.py        # Builds ProcessingTask for url_classification, Engine.run() or submit()
├── api/
│   └── enrichment.py            # Builds tasks from source config, calls Engine.run() / submit()
```

### Option B: Engine under enrichment

Same responsibilities, but under `topos/enrichment/engine/` (e.g. `enrichment/engine/engine.py`, `enrichment/engine/tasks.py`, `enrichment/engine/backends/`). Orchestrator and API call `enrichment.engine.Engine.run()`.

---

## Component → file responsibility

| PRD component | Proposed file(s) | Responsibility |
|---------------|-------------------|------------------|
| **Task Intake** | `engine/intake.py` | Accept ProcessingTask from API, ingest, or internal; normalize and pass to validator. |
| **Task Validator** | `engine/validator.py` | Validate task schema (id, type, model_request, execution); check model available in registry/backend. |
| **Task Router** | `engine/router.py` | Resolve task type + model_request → BackendAdapter; apply fallback. |
| **Queue Manager** | `engine/queue_manager.py` | In-memory queue (V1); enqueue(submit), dequeue(worker); optional persistence later. |
| **Scheduler** | `engine/scheduler.py` | Priority ordering; model-aware batching (group by batch_key/model); fairness (don’t starve user tasks). |
| **Model Loader / Cache** | `engine/model_loader.py` + `enrichment/models/manager.py` | Load model via backend adapter; cache by (provider, model_id); unload on memory pressure or config. |
| **Backend Adapters** | `engine/backends/base.py`, `ollama.py`, `huggingface.py` | load_model, run_inference, unload_model; HF wraps current pipeline/model load in website_classifier and emo_27. |
| **Result Formatter** | `engine/result_formatter.py` | Convert backend output + execution_meta → ProcessingResult (status, output, confidence, provenance, execution_meta, error). |
| **Execution Logger** | `engine/execution_logger.py` or inline in Engine | Log task_id, model, duration_ms, cache_hit to observability or file. |
| **Transport Layer** | Future | Client in engine; server implements same run/submit contract over WebSocket/HTTP. |

---

## Data flow (V1)

1. **Write-event enrichment**  
   `ingest_helpers` → build ProcessingTask (enrichment, url_classification, source_id, record_ids, input={url, title}) → **Engine.run(task)** (sync) or **Engine.submit(task)** → Router → HF adapter (or Ollama if configured) → Result Formatter → return result; ingest_helpers writes to `browser_url_classification` as today.

2. **Canonical batch enrichment**  
   API `_process_enrichment_core` → for each job in source’s canonical_enrichment_jobs, build one or more ProcessingTasks (enrichment, subtype=emo_27, record_ids=message_ids, input=canonical batch) → **Engine.run(task)** or submit to queue; orchestrator or a worker runs tasks; results written via DerivedTablesManager as today.

3. **User query (future)**  
   API or MCP → build ProcessingTask (type=query, …) → Engine.run() or submit() → same pipeline.

---

## Integration points

| Caller | Current | After Engine |
|--------|---------|--------------|
| `ingest_helpers._run_browser_url_classification_enrichment` | Calls `classify_url(url, title)`, then `write_browser_url_classification` | Build ProcessingTask; call Engine.run(task); map result to existing write_browser_url_classification args. |
| `EnrichmentOrchestrator.run_canonical` | Iterates jobs, calls `job.enrich(messages)` | Either (a) orchestrator builds tasks per job and calls Engine.run(task) per batch, or (b) jobs remain but internally call Engine.run() for inference only. |
| `api/enrichment.py` backfill/test | Direct `classify_url` or job usage | Build task, Engine.run(task), return result. |
| `Emo27Job.enrich` | Loads HF model, runs inference in loop | Emo27Job builds one task per message (or batched task) and calls Engine.run(); no direct transformers/torch. |
| `WebsiteUrlClassifier` | Direct pipeline | Replaced by Engine task (enrichment, url_classification) → HF adapter. |

---

## Registry and config

- **Registry** (`enrichment/models/registry.py`): Add `provider` ("ollama" | "huggingface"), `ollama_model` (when provider=ollama); `get_model_for_task(task_type, subtype, source_id?)` for resolution; keep `get_preferred_model` for override.
- **Config** (`config/settings.py` or engine config): `engine_default_backend`, `engine_queue_max_size`, `engine_ollama_base_url`, default task→model mapping; optional `engine_queue_persistence_path`.

This keeps the Engine architecture from the PRD clearly mapped to files and callers in the existing codebase.
