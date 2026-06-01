# Sprint 03 — Migrate URL classification to Engine

**Topos Engine V1**

---

## Objective

Switch all **URL classification** flows to the Engine: ingest write-event and API (test + backfill) build a `ProcessingTask`, call `Engine.run(task)`, and map the result to existing storage. Remove direct use of `classify_url` from `website_classifier` in these paths.

**Plan refs:** [../MIGRATION_AND_GAPS.md](../MIGRATION_AND_GAPS.md) Step 4; [../IMPLEMENTATION_MAP.md](../IMPLEMENTATION_MAP.md) §2 (Execution modes), §8 (Source integration).

---

## Scope

- **ingest_helpers.py**
  - In `_run_browser_url_classification_enrichment`: build `ProcessingTask` (type=enrichment, subtype=url_classification, source_id, record_ids, input={url, title}, model_request from registry or default). Call `Engine.run(task)` (sync or await). Map `ProcessingResult.output` to existing `write_browser_url_classification` parameters (category, confidence, model_name). Remove direct import of `classify_url` for this path.
- **api/enrichment.py**
  - `_test_browser_visits_url_classification`: build task from request body (url, title), call Engine.run(task), return API response in same shape as today (status, input, output).
  - `_backfill_browser_visits_url_classification`: for each row, build task, call Engine.run(task), write via `write_browser_url_classification` with result output; remove direct `classify_url`.
- **website_classifier.py**
  - Either deprecate and remove, or turn into a thin wrapper: `classify_url(url, title)` builds task, calls Engine.run(), maps result to current return dict (for any remaining direct callers until removed).

---

## Acceptance criteria

| ID | Criterion | How to verify |
|----|-----------|----------------|
| AC-3.1 | Ingest write-event URL classification uses Engine.run(task) and writes the same schema to browser_url_classification. | Integration test or manual: trigger ingest with browser visit; assert row in browser_url_classification with expected category/confidence; no direct classify_url in call path. |
| AC-3.2 | POST /sources/{source_id}/enrichments/url_classification/test with {url, title} returns same response shape and equivalent classification as before. | API test: same payload as current test endpoint; assert status, output.category, output.confidence; behavior matches or improves on pre-migration. |
| AC-3.3 | Backfill endpoint for browser_visits url_classification uses Engine.run(task) per row and writes same schema. | API test: call backfill with limit; assert rows written to browser_url_classification; no direct classify_url in backfill path. |
| AC-3.4 | No remaining direct import of classify_url in ingest_helpers or in the test/backfill handlers in api/enrichment. | Code search / review: ingest_helpers and api/enrichment do not import from website_classifier for inference. |
| AC-3.5 | Feature flag or config (optional) allows reverting to direct classifier for rollback. | If implemented: toggle off → ingest/API use direct classify_url; toggle on → Engine. Document in MIGRATION_AND_GAPS. |

---

## Implementation notes

- **Task building:** Use a small helper (e.g. in engine/tasks.py or ingest_helpers) to build ProcessingTask from (source_id, record_id, url, title) so ingest and API share the same shape.
- **Async:** If Engine.run is sync, use asyncio.to_thread(Engine.run, task) in async ingest/API to avoid blocking.
- **Errors:** Engine returns ProcessingResult with error; map to existing error handling (log, skip row, or return 500) so behavior is unchanged.

---

## Tests

| Test | Description |
|------|-------------|
| Ingest URL classification via Engine | Run ingest path that triggers _run_browser_url_classification_enrichment; assert task built and Engine.run called; assert write_browser_url_classification called with correct category/confidence from result. |
| Test endpoint uses Engine | POST test with url/title; assert response shape; assert Engine.run invoked with correct task; optional: snapshot or golden output for a known URL. |
| Backfill uses Engine | Call backfill (limit=1); assert Engine.run called per row; assert one row written to browser_url_classification. |
| No direct classify_url in pipeline | Grep or unit test: ingest_helpers and enrichment test/backfill handlers do not call classify_url (or only via wrapper that uses Engine). |
| Error path | Engine returns error result (e.g. invalid input); ingest/API handle gracefully (no uncaught exception; log or return error response). |

---

## Definition of done

- [ ] ingest_helpers._run_browser_url_classification_enrichment builds task and uses Engine.run(); no direct classify_url.
- [ ] api/enrichment test and backfill for url_classification use Engine.run(); response/write behavior unchanged.
- [ ] website_classifier deprecated or thin wrapper; documented.
- [ ] All acceptance criteria met.
- [ ] Tests above added and passing.
