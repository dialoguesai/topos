# Sprint 04 — Registry extension, Ollama adapter, and emo_27 migration

**Topos Engine V1**

---

## Objective

Extend the **model registry** for multiple providers (HuggingFace + Ollama), add the **Ollama backend adapter**, and migrate **Emo27Job** to use the Engine for inference so that no direct `transformers`/`torch` imports remain in the job.

**Plan refs:** [../MIGRATION_AND_GAPS.md](../MIGRATION_AND_GAPS.md) Steps 5–6; [../IMPLEMENTATION_MAP.md](../IMPLEMENTATION_MAP.md) §7 (Model runtime), §2 (Task types); [../ARCHITECTURE_MAPPING.md](../ARCHITECTURE_MAPPING.md) (Backend Adapters).

---

## Scope

- **Registry**
  - Add `provider: Literal["ollama", "huggingface"]` and `ollama_model: Optional[str]` to model registration.
  - `register_model(..., provider=..., ollama_model=...)`; backward compatibility: existing callers without provider default to hugginface.
  - `get_model_for_task(task_type, subtype, source_id=None)` returns full spec (provider, model id / huggingface_path / ollama_model).
- **Config**
  - Add to `config/settings.py`: `engine_ollama_base_url` (default e.g. http://localhost:11434), `engine_default_provider`, optional `engine_default_models` (task → model id).
- **Ollama adapter**
  - Add `topos/engine/backends/ollama.py` implementing BackendAdapter. Use Ollama HTTP API (e.g. /api/generate or /api/chat) for inference. run_inference maps task input (e.g. text for emotion) to prompt/format; parse response into same output shape as HF adapter where applicable (e.g. emotion labels).
  - Model name from task.model_request.model or registry.
- **Router**
  - When model_request.provider == "ollama" (or registry returns ollama), return Ollama adapter; else HuggingFace.
- **Emo27Job**
  - Remove _load_model, _classify_emotion, and all direct transformers/torch imports.
  - In enrich(): for each message (or batch), build ProcessingTask (enrichment, subtype=emotion_classification or emo_27, record_ids=[message_id], input={text: content}, model_request from registry). Call Engine.run(task); map ProcessingResult to current result dict (message_id, emotion_label, confidence, all_emotions, model). Keep same derived table name and orchestrator flow.

---

## Acceptance criteria

| ID | Criterion | How to verify |
|----|-----------|----------------|
| AC-4.1 | Registry supports provider and ollama_model; get_model_for_task returns provider and model spec. | Unit test: register model with provider=ollama, ollama_model=llama3.1; get_model_for_task returns that spec. |
| AC-4.2 | Ollama adapter run_inference calls Ollama API and returns a result dict (e.g. for emotion or generic text). | Unit/integration test: with Ollama running (or mock), task with provider=ollama; Engine.run() returns result; optional: mock HTTP to avoid real Ollama. |
| AC-4.3 | Router returns Ollama adapter when provider is ollama; HF adapter when provider is hugginface. | Unit test: Engine with task.model_request.provider=ollama → router returns Ollama adapter; same for hugginface. |
| AC-4.4 | Emo27Job has no direct import of transformers or torch. | Grep/code review: emo_27_job.py does not import transformers/torch. |
| AC-4.5 | Emo27Job.enrich() uses Engine.run(task) per message (or batched) and produces the same derived table rows (message_id, emotion_label, confidence, all_emotions, model). | Integration test: run orchestrator with Emo27Job on a few canonical messages; assert message_emotions rows match expected shape and content (or within tolerance vs current behavior). |
| AC-4.6 | Config engine_ollama_base_url and engine_default_provider are read by adapter/router. | Unit test or config load test: settings contain new fields; adapter uses base URL when calling Ollama. |

---

## Implementation notes

- **Ollama prompt:** For emotion_classification, design a short prompt (e.g. “Classify the emotion of this text. Reply with a JSON object: {\"label\": \"...\", \"confidence\": 0.9}.”) and parse response; or use a dedicated Ollama model that returns structured output. Document in adapter or docs.
- **Optional:** Run emotion_classification tests with HF only first (no Ollama required); add Ollama tests as integration tests that skip if Ollama not available.
- **Backward compatibility:** Existing registry entries without provider continue to use HuggingFace; default model for emo_27 remains the current HF model unless overridden.

---

## Tests

| Test | Description |
|------|-------------|
| Registry provider and ollama_model | Register with provider=ollama, ollama_model=...; get_model_for_task returns correct spec; list_models includes provider. |
| Ollama adapter run_inference | Mock Ollama HTTP or use real instance; task with provider=ollama; Engine.run() returns ProcessingResult with output; assert no unhandled exception. |
| Router provider selection | Task with provider=ollama → router returns Ollama adapter; provider=hugginface → HF adapter. |
| Emo27Job no transformers/torch | Import emo_27_job; assert no transformers/torch in module; enrich() calls Engine.run. |
| Emo27Job enrich output shape | Run enrich() with 1–2 messages; assert list of dicts with message_id, emotion_label, confidence, all_emotions, model; optional: compare to previous HF-only output. |
| Orchestrator + Emo27Job | Run canonical enrichment with Emo27Job only; assert message_emotions table has expected rows. |
| Config loading | Load settings; assert engine_ollama_base_url and engine_default_provider present (default or env). |

---

## Definition of done

- [ ] Registry extended with provider and ollama_model; get_model_for_task returns full spec.
- [ ] Ollama adapter implemented and wired in router; Engine.run(ollama_task) works (with or without mock).
- [ ] Config includes engine_ollama_base_url and engine_default_provider.
- [ ] Emo27Job uses Engine for inference only; no transformers/torch in job.
- [ ] All acceptance criteria met.
- [ ] Tests above added and passing.
