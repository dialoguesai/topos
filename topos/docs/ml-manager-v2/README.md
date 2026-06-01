# Topos Engine (Processing Model Manager) — Implementation Docs

This directory holds implementation mapping documents for the **Topos Engine** PRD: the core processing system for ML/LLM-powered computation (enrichments, transformations, derivations, queries, future agent workflows).

Code lives under `topos/` (enrichment, ingestion, API, sources, storage, services).

| Document | Purpose |
|----------|---------|
| **[IMPLEMENTATION_MAP.md](./IMPLEMENTATION_MAP.md)** | PRD section → codebase mapping: where each PRD concept (task types, execution modes, interfaces, contracts) maps to existing or new code. |
| **[ARCHITECTURE_MAPPING.md](./ARCHITECTURE_MAPPING.md)** | PRD architecture (Task Intake, Validator, Router, Queue, Scheduler, Adapters, etc.) → current modules and proposed new modules. |
| **[MIGRATION_AND_GAPS.md](./MIGRATION_AND_GAPS.md)** | Migration steps (website_classifier, emo_27, registry), gap analysis (queue, scheduler, task contract), and V1 scope checklist. |
| **[sprints/](./sprints/)** | Sprints to complete the plan: 01–07 with acceptance criteria and tests for each. |
| **[MANUAL_VERIFICATION_CHECKLIST.md](./MANUAL_VERIFICATION_CHECKLIST.md)** | Post-implementation checklist to verify Engine behavior manually. |
| **[DECOUPLING_AND_REMOTE_ENGINE.md](./DECOUPLING_AND_REMOTE_ENGINE.md)** | Whether the Engine can run on another machine; what’s in place vs missing for remote inference. |

## Relation to existing ML Manager docs

- **`../ml-manager/`** — Original ML/LLM Manager PRD (what we're building, why, registry-driven execution). The Engine PRD refines this into a **task-based runtime** with queue, scheduler, and a stable `run`/`submit` interface.
- **`ml-manager-v2/`** — Implementation mapping of the **Topos Engine** PRD onto the current `topos/` codebase: what exists, what to add, and how to migrate.

## Quick reference: primary code paths

| Area | Current location |
|------|-------------------|
| Enrichment orchestration | `topos/enrichment/orchestrator.py` |
| Model registry (stub) | `topos/enrichment/models/registry.py` |
| Model manager (stub) | `topos/enrichment/models/manager.py` |
| URL classification (HF direct) | `topos/enrichment/website_classifier.py` |
| Emotion classification (HF direct) | `topos/enrichment/jobs/canonical/emo_27_job.py` |
| Source-defined enrichments | `topos/sources/definitions.py` (`raw_enrichment_jobs`, `canonical_enrichment_jobs`) |
| Write-event raw enrichment | `topos/ingestion/ingest_helpers.py` → `_run_browser_url_classification_enrichment` |
| Canonical enrichment API | `topos/api/enrichment.py` |
| Derived tables / storage | `topos/enrichment/derived_tables.py`, `topos/storage/enrichment/` |
| Config | `topos/config/settings.py` |
