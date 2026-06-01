# Topos Engine (V1) — Sprints

Sprints to implement the **Topos Engine** (Processing Model Manager) per the implementation plan. Execute in order; each sprint has acceptance criteria and tests.

---

## Plan references

| Document | Purpose |
|----------|---------|
| [../IMPLEMENTATION_MAP.md](../IMPLEMENTATION_MAP.md) | PRD section → codebase mapping (task types, execution modes, interfaces, components). |
| [../ARCHITECTURE_MAPPING.md](../ARCHITECTURE_MAPPING.md) | Engine components → files; proposed `topos/engine/` layout and integration points. |
| [../MIGRATION_AND_GAPS.md](../MIGRATION_AND_GAPS.md) | Ordered migration steps (1–9), gap summary, V1 scope checklist, rollback notes. |

---

## Sprint index

| # | Sprint | Migration steps | Purpose |
|---|--------|-----------------|---------|
| 01 | [Task contract and Engine facade](./SPRINT_01_TASK_CONTRACT_AND_ENGINE_FACADE.md) | Steps 1–2 | ProcessingTask/ProcessingResult; Engine.run() with intake, validator, router, result formatter (stubs where needed). |
| 02 | [BackendAdapter and HuggingFace adapter](./SPRINT_02_BACKEND_ADAPTER_AND_HUGGINGFACE.md) | Step 3 | BackendAdapter protocol; HF adapter for url_classification and emotion_classification; Engine routes to adapter. |
| 03 | [Migrate URL classification to Engine](./SPRINT_03_MIGRATE_URL_CLASSIFICATION_TO_ENGINE.md) | Step 4 | Ingest and API build task, call Engine.run(); remove direct website_classifier from pipeline. |
| 04 | [Registry extension, Ollama adapter, and emo_27 migration](./SPRINT_04_REGISTRY_OLLAMA_AND_EMO27_MIGRATION.md) | Steps 5–6 | Registry provider/ollama_model; Ollama adapter; Emo27Job uses Engine for inference. |
| 05 | [Queue and submit()](./SPRINT_05_QUEUE_AND_SUBMIT.md) | Step 7 | In-memory queue; Engine.submit() returns TaskHandle; worker runs tasks; optional API endpoints. |
| 06 | [Scheduler and model-aware batching](./SPRINT_06_SCHEDULER_AND_BATCHING.md) | Step 8 | Priority ordering; model-aware batching; fairness so user tasks are not starved. |
| 07 | [Observability and configuration](./SPRINT_07_OBSERVABILITY_AND_CONFIG.md) | Step 9 | Metrics (task counts, latency, queue wait, errors, cache); engine config in settings. |

---

## Execution order

Run sprints **01 → 07** in sequence. Dependencies:

- **01** must be done before any caller uses the Engine.
- **02** completes the sync run path so **03** can switch URL classification to Engine.
- **04** adds Ollama and migrates emo_27; **03** can be done before or in parallel with 04 if URL classification uses only HF.
- **05** and **06** build on the same Engine; **07** is independent and can overlap with 05/06.

---

## Test and acceptance

Each sprint doc includes:

- **Acceptance criteria** — Table with ID, criterion, and how to verify.
- **Tests** — Concrete tests (unit/integration) to add or run for that sprint.
- **Definition of done** — Checklist to close the sprint.

Existing test layout: prefer `topos/tests/` or project-level `tests/` for engine tests (e.g. `tests/engine/` or `topos/tests/engine/`). Fixtures and env (e.g. test DB, mock Ollama) should be documented in the sprint that introduces them.
