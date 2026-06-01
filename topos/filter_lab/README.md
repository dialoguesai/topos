# Filter Lab (engine)

SQLite tables `filter_lab_*`, preset bundles, background job worker, REST under `/v1/filter-lab/*` (see `topos/api/filter_lab.py`). Control plane proxies the same paths via WebSocket message types in `topos/core/handlers.py` (`get_filter_lab_bundles`, `post_filter_lab_job_group`, …).

**Auth:** `Authorization: Bearer <TOPOS_KEY>` (same as sanitization config).

**Cleanup:** After a job group finishes, ephemeral Ollama pulls (policy B) are deleted unless the tag is in the baseline snapshot or listed in resolved sanitization `default_model` / per-transform `models`.
