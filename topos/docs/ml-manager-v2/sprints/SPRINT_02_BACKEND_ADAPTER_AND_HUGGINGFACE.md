# Sprint 02 — BackendAdapter and HuggingFace adapter

**Topos Engine V1**

---

## Objective

Define the **BackendAdapter** protocol and implement the **HuggingFace adapter** so the Engine can run real inference for `url_classification` and `emotion_classification`. The Engine routes tasks to the adapter; no callers (ingest/API) are migrated yet.

**Plan refs:** [../MIGRATION_AND_GAPS.md](../MIGRATION_AND_GAPS.md) Step 3; [../IMPLEMENTATION_MAP.md](../IMPLEMENTATION_MAP.md) §7 (Model runtime); [../ARCHITECTURE_MAPPING.md](../ARCHITECTURE_MAPPING.md) (Backend Adapters).

---

## Scope

- **BackendAdapter protocol**
  - Add `topos/engine/backends/base.py`: `load_model(model_name, config)`, `run_inference(payload, config)`, `unload_model(model_name)`. Protocol or ABC.
- **HuggingFace adapter**
  - Add `topos/engine/backends/huggingface.py` implementing BackendAdapter.
  - **url_classification:** same behavior as current `WebsiteUrlClassifier` (text-classification pipeline, model from config/task or default KnutJaegersberg/website-classifier).
  - **emotion_classification / emo_27:** same behavior as current `Emo27Job` (AutoModel + tokenizer, softmax, top-k labels); model from config/task or default SamLowe/roberta-base-go_emotions.
  - Adapter receives task subtype and input; dispatches to the correct pipeline/model; returns a dict that the result formatter can map to ProcessingResult.output.
- **Registry**
  - Extend `enrichment/models/registry.py` with `get_model_for_task(task_type, subtype)` returning model spec (e.g. huggingface_path); use in adapter when task does not override model.
- **Engine**
  - Router returns HuggingFace adapter for provider `huggingface`; Engine calls adapter.run_inference(task.input, config) and passes result to formatter.
  - Model loading/caching stays inside the adapter (load on first use per subtype/model).

---

## Acceptance criteria

| ID | Criterion | How to verify |
|----|-----------|----------------|
| AC-2.1 | BackendAdapter protocol is defined with load_model, run_inference, unload_model. | Unit test: assert protocol methods exist; mock adapter implements them. |
| AC-2.2 | HF adapter run_inference for url_classification returns category and confidence for a given url/title input. | Unit/integration test: task with subtype url_classification, input {url, title}; Engine.run() → result.output has category, confidence; values match current website_classifier for same input (or within tolerance). |
| AC-2.3 | HF adapter run_inference for emotion_classification returns emotion labels and confidences for a given text input. | Unit/integration test: task with subtype emotion_classification, input {text}; Engine.run() → result.output has labels/confidences; behavior matches current Emo27Job for same text (or within tolerance). |
| AC-2.4 | Registry.get_model_for_task(task_type, subtype) returns a model spec when a model is registered for that task/subtype. | Unit test: register model for task; get_model_for_task returns spec (e.g. huggingface_path). |
| AC-2.5 | Engine.run() with provider=huggingface and valid task runs through adapter and returns ProcessingResult with execution_meta (e.g. provider, model, duration_ms). | Integration test: full run; assert result.status completed, result.execution_meta populated. |

---

## Implementation notes

- **Extract logic:** Move pipeline/model loading and inference from `website_classifier.py` and `emo_27_job.py` into the HF adapter so behavior is preserved; do not duplicate logic.
- **Optional:** Keep `website_classifier.py` and `emo_27_job.py` as thin wrappers that build a task and call Engine.run() for backward compatibility during migration (Sprint 03/04).
- **Config:** Adapter config can include model name override, max_length, top_k, etc.; pass from task or registry.

---

## Tests

| Test | Description |
|------|-------------|
| BackendAdapter protocol | Mock adapter implements load_model, run_inference, unload_model; Engine uses it without error. |
| HF url_classification | Build task (enrichment, url_classification, input={url, title}); Engine.run(); assert result.output.category, result.output.confidence; optional: compare to current classify_url(url, title) for same input. |
| HF emotion_classification | Build task (enrichment, emotion_classification, input={text}); Engine.run(); assert result.output has top emotion and confidences; optional: compare to current Emo27Job for same text. |
| Registry get_model_for_task | Register model with task_name/subtype; call get_model_for_task; assert returned spec; call with unknown task → returns None or default. |
| Engine execution_meta | After successful run, assert result.execution_meta contains provider, model, and duration_ms (or equivalent). |
| Invalid input handling | Task with empty url or missing input; Engine returns structured error result (validator or adapter). |

---

## Definition of done

- [ ] BackendAdapter protocol in `engine/backends/base.py`.
- [ ] HuggingFace adapter in `engine/backends/huggingface.py` for url_classification and emotion_classification.
- [ ] Registry extended with get_model_for_task; used by adapter when task does not override model.
- [ ] Engine router returns HF adapter for provider hugginface; Engine.run() performs real inference and formats result.
- [ ] All acceptance criteria met.
- [ ] Tests above added and passing (may require transformers/torch in test env).
