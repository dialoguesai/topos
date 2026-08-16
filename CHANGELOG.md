# Changelog

All notable changes to `topos-node` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); lane tags
follow `RELEASING.md` (`[S1]`, `[E:…]`, `[D]`, `[O]`, …).

The machine-readable twin of each release is
`topos/upgrades/manifests.json`.

## [Unreleased]

### Fixed

- `[E:query]` **The owner's actual question reaches the query path again.** The
  handler read `intent` before `query`, so home chat's stopword-stripped
  fingerprint became the query text for every downstream stage: "how did I sleep
  this week?" arrived as "sleep week". Measured cost — the planner's `\bthis week\b`
  never matched, so the turn silently lost its time window; vector ranking embedded
  a fragment instead of a sentence; and the scope classifier, trained on questions,
  abstained on keyword soup. Nothing wanted the digest (retrieval's `_query_tokens`
  already strips stopwords where a bag of words is needed), and the clients were
  never at fault: home chat has always sent both fields. One-line precedence flip;
  callers that send only `intent` are unaffected.

- `[O]` **The event loop stops taking the SQLite write gate on the hottest
  paths.** The gate is a blocking OS lock, so acquiring it inside a coroutine
  stalls every other coroutine — including the control-plane keepalive, which
  is the node-side half of the 2026-08-15 relay outage: relayed requests
  (healthcheck, `get_device_info`) answered late or not at all during heavy
  enrichment, and only the `control_plane_client` snapshot fallback kept device
  info alive. The engine's own `[WRITE_GATE] acquired on the event-loop thread`
  guard named the call sites; this fixes the ones that fired most (147, 134 and
  107 times in a single log), in two shapes:
  - **Moved off the loop** (`asyncio.to_thread`, resolving the worker's own
    connection — a `sqlite3.Connection` holds one transaction state, so handing
    the loop's handle to a worker is the corruption `get_db_connection` was made
    thread-local to prevent): the Usage Inbox dedupe lookups and delivery
    record in `app_ingest`/`check_inbox_write`, the whole `install_service`
    surface behind the sources API (install/list/patch/uninstall/rehydrate), the
    six routine write handlers, and both gated sections of the platform privacy
    layer (the `canonical_nsfw_v1` migration and the batched disclosure/NSFW
    write pass). A new `run_db_write` helper next to `run_db_read` makes the
    handler-layer conversions one line each and resolves the connection through
    the hub, so tests that monkeypatch `get_db_connection` still bind the
    worker's handle.
  - **No longer taking the gate at all**: `CanonicalTablesManager._ensure_tables`
    and `source_settings.ensure_table` re-ran idempotent DDL on every
    construction and every settings read respectively. Both now probe first
    (`sqlite_master` / `PRAGMA table_info` — plain reads), so the steady state
    costs no gate. Deliberately not memoized on `id(conn)`: addresses are reused
    once a handle is collected, which reports DDL as done on a brand-new empty
    database.

  `run_privacy_disclosure_layer` accepts `conn=None` (what the ingest pipeline
  now passes) to mean "resolve per worker"; a caller-pinned connection keeps its
  thread affinity and runs inline, because test fixtures open theirs with
  `check_same_thread=True`. Remaining loop-thread acquisitions are lower
  frequency and structural — the biggest is `get_signal_service`, which hands
  the caller's connection to a long-lived `SignalService`, so it needs a
  connection-lifecycle change rather than a `to_thread` wrapper.

- `[E]` **Place references extract again: `PlaceContext` is declared for the
  `places` dimension.** The artifact router has always written `PlaceContext`
  objects for place `EntityRef` artifacts, but `places.json` never declared the
  type, so `SignalObjectStore._validate_object_type` rejected every write and
  `dimension_summary` DEGRADED on any batch containing a place reference
  (observed on grow_data_file, 2026-08-15: every batch queued for retry with
  derived data MISSING). Every other router-written type (RelationshipEdge,
  SkillNode, ExperienceNode, Goal) was declared — places was the one gap. The
  static write-site sweep in test_dimension_definitions was blind to it because
  its regex only matched snake_case object types and only checked
  `signal_objects`; it now matches CamelCase entity types and checks the same
  allowlist the store enforces (entity ids + signal_objects + gate_objects).
  A `rerun-places-dimension-summary` upgrade step re-runs the failed job where
  derived output is missing.

### Changed

- `[E]` **Ingest models are no longer steered by the model pack.** The pack's
  `classify` role selected the models for facts extraction and conversation-context
  tagging — ingest functions that run when data arrives, not when the owner asks
  something, and that already have their own selectors (engine_config, surfaced as
  Node functions). The role is retired repo-wide: `resolve_facts_llm_model` and
  `resolve_context_llm_model` are now device-override → settings default with no
  pack rung, and enrichment usage (`ENRICHMENT_SUBTYPES`) is attributed to the
  active pack with NO role — the old chain fell through to a generic `primary`
  label, billing the pack for a decision it did not make. The engine ROLES mirror
  is (primary, reasoning, tool, scope). Guards inverted: the J-B10 wiring test now
  asserts these two functions make NO pack-resolver call at all.

### Fixed

- `[E]` **The node stopped wedging: torch's thread pool is bounded and every model's
  device is resolved in one place.** Twelve event-loop deadlocks on 2026-08-15, always
  the same stack — `TensorImpl::incref_pyobject → gil_scoped_acquire → take_gil`, with
  26 threads resident in libtorch and the loop blocked behind them. Torch's intra-op
  pool is one thread per core and each can need the GIL to refcount a Python-owned
  tensor; in an asyncio server that already runs DB work and job workers on threads,
  the pool and the loop deadlock. New `topos/engine/torch_runtime.py` bounds intra-op
  to 2 and inter-op to 1 at startup, before the first model loads. It also owns device
  selection for embeddings, NER, rerank, the privacy filter and the scope head, which
  previously each guessed for themselves: sentence-transformers took no device argument
  at all and silently chose MPS on any Mac (and CUDA nowhere), while the privacy filter
  ran its own capability ladder that had drifted from it. One resolver now: explicit
  `TOPOS_ML_DEVICE`/`engine_ml_device` → `auto` (CUDA, else MPS, else CPU) → CPU, so a
  CUDA host uses CUDA without a code change and any host can be pinned by name.
  Device is part of each model's cache key, so switching it reloads instead of serving
  a handle pinned to the old device. **`auto` does not select MPS**: bounding threads
  alone did not stop the wedge — the node hung again on the next burst of MPS
  embedding calls with the pool at two — so while the torch bug stands, MPS is opt-in
  by name (`ENGINE_ML_DEVICE=mps`). These models are small (MiniLM 22M, NER 125M) and
  CPU-fast on Apple silicon; the cost is far smaller than a hung node.


### Changed

- `[E:query]` **Horos ships from Hugging Face, picks its device by capability, and
  warms at boot.** Three changes with one theme — nothing about the scope head is
  hardcoded to this machine any more. (1) `load_head` resolves an explicit
  `TOPOS_SCOPE_HEAD` path, then a staged `~/.topos/models/scope_head`, then falls back
  to fetching `Dialogues/horos` **at a pinned revision**; a node that cannot reach the
  hub degrades to prototype routing rather than failing, and `HF_HUB_OFFLINE` plus the
  local cache make a node network-free after first fetch. (2) New `resolve_device`:
  explicit override → `auto` (CUDA, else MPS, else CPU) → default `cpu`. The head is
  no longer implicitly CPU-by-omission, and training's `auto` no longer preferred MPS
  over CUDA, which would have picked the weaker accelerator on a CUDA box. Inference
  still defaults to CPU on purpose: 17 ms is fast enough, and the accelerator belongs
  to the owner's LLM. (3) Shadow warms **once at startup**, single-threaded, before
  traffic and before the MPS models load — the previous daemon-thread warm put a
  265 MB load in flight beside live Metal work, and this process carries a torch
  GIL/MPS deadlock (synchronous dispatch whose block re-acquires the GIL) that wedged
  the node twelve times on 2026-08-15. A circuit breaker disables observation for the
  process after 3 slow-or-failed turns: telemetry may cost a millisecond, never a turn.


### Added

- `[E:query]` **Horos, the scope classifier, trained to its spec: multi-label with an
  explicit `none` class and a four-branch ladder.** `classify()` now escalates on
  UNCERTAINTY (ambiguity or ignorance), never on cardinality — a confident
  `{availability, schedule}` set is acted on, and a new `reason` field keeps the
  branches measurable apart. The trained head is published as `Dialogues/horos`
  (macro-F1 0.512 on classify-8 vs mistral:7b's 0.495; hybrid-with-escalation 0.550
  at ~1/6th the LLM calls). Pre-`none` artifacts keep the legacy two-threshold path.
  New `scope` pack role (engine mirror): on-device `scope-head` provider, optional
  and engine-defaulted so stored packs stay valid. Dimension definitions gain
  `WorkItem`, `BrowseTrail`, `ProfileSurface` — the classifier's dead zone was
  partly a schema gap. Shadow mode gains a `~/.topos/scope_shadow.on` flag-file
  switch (the app-shell node inherits no env) and an off-thread model warm so a
  cold head never loads inline in the request path.


### Fixed

- `[S1]` **The Lab's "apply preferred model" for entities is applied, not just
  stored.** Overrides are saved per JOB id but looked up per engine SUBTYPE via
  `SUBTYPE_TO_JOB`, and `entities_job` emits `entity_extraction_batch` while the
  map carried only the bare `entity_extraction` — so picking a preferred NER
  model in the Enrichment Lab wrote the override, displayed it, and never used
  it. emo_27 hit the identical hole at its `_batch` rename and was patched by
  hand; entities was missed. Both forms are mapped now, and a new test derives
  the required entries from the Lab's own factory dict and each job module's
  actual `run_engine_task(subtype=...)` literals, so the next subtype rename
  fails a test instead of silently orphaning an override.
  (`topos/enrichment/model_overrides.py`,
  `tests/enrichment/test_model_override_subtypes.py`)

### Added

- `[D]` **A cold labeling model no longer silently discards a whole relabel
  pass.** `apply_llm_cluster_labels` aborts on the first failure so a down model
  costs one timeout rather than k of them — but the budget was a flat 10s, the
  first call of a pass pays the local model's load cost, and labeling order is
  biggest-cluster-first, so the longest prompt always landed on the coldest
  model. On a live node that aborted all 163 clusters in 12 seconds and reported
  `status: completed, relabeled: 0`: a no-op that reads as success. Three
  changes: the first call of a pass gets a warm-up budget
  (`TOPOS_CLUSTER_LABEL_WARMUP_TIMEOUT`, default 90s) while later calls keep the
  ordinary one; a single slow cluster costs that cluster its label rather than
  the remainder of the pass (abort now needs three CONSECUTIVE failures, or a
  model that never answered at all); and the abort is reported —
  `relabel_existing_clusters` returns `status: "aborted"` with a reason instead
  of a completed run that did nothing. Still never raised: `recompute_topic_clusters`
  calls the same labeler, and a down model must cost the labels, not the cluster
  rebuild. (`topos/features/signal/cluster_labels.py`)

- `[S1]` **Declarative canonical field mapping** (`canonical_field_map` on a
  source definition — PLAN_CONNECTOR_CATALOG_ROLLOUT §5a capabilities 2–3). A
  source now DECLARES which piece of its records lands in which canonical
  column, and where extra rows fan out from, instead of that being Python in
  this repo: `{table: {column: rule}}` with a rule vocabulary of `path` (dotted,
  `[*]` expands lists), `first_of`, `template`, `const`, `map`/`default`,
  `transform` (closed catalog), `join`, `when`, plus `fan_out` + `where` for
  one-row-per-item lanes. A declaration overlays the source's code mapper where
  it has one and IS the mapping where it does not — which is what makes the
  activity/journal/documents lanes reachable for a runtime-installed source
  (previously they fell back to the *browser* mapper, the wrong shape for
  anything but browser visits). Validated at definition time, so a typo'd path
  fails the install instead of silently ingesting nothing.
  (`topos/canonicalization/declared_field_map.py`)

- `[S1]` **Recorded derivation debt now waits for its model instead of burning
  its one attempt on it.** A debt whose job needs a provider this machine does
  not have could only be claimed, re-run into the same wall, and parked
  `failed` — after which nothing in the queue moved it, because `requeue_job`
  only releases a live claim and `recover_stale_jobs` only touches `running`.
  The work resumed when a human hit `POST /signal/derivation-debt/retry`, or
  never. Two halves: `run_derivation_retry_job` now asks
  `job_readiness.job_is_ready()` BEFORE reloading the batch's canonical records,
  and holds with `waiting for provider: …` rather than doing the work to defer
  again; and `revive_capability_blocked_debts()` re-queues the parked rows on
  the not-ready → ready EDGE, swept by the pipeline worker every 5 min. Edge
  rather than level so a debt that fails for a real reason gets one fresh
  attempt per time the missing provider actually appears, instead of being
  re-queued forever. Per-job provider is read from the model catalog
  (`MVP_JOB_SPECS`), so a job registered there is classified without a second
  list to maintain; jobs absent from it are assumed runnable rather than
  stalled. (`topos/enrichment/job_readiness.py`,
  `topos/pipeline/job_store.py::requeue_failed_jobs`)

- `[E:llm]` One-click Ollama install for the quick-start journey (J-B10):
  `ollama_install` runs the official installer on an explicit request —
  Darwin only, single-flight, never from ambient state — and
  `ollama_install_status` reports `idle|installing|started|error` with the
  installer's status lines. A nonzero installer exit with `/Applications/
  Ollama.app` present still ends in `started` (the node launches it and lets
  reachability be the truth), because the script's only sudo is a PATH symlink
  the engine does not need.

- `[E:llm]` `ollama_list_models` now carries per-tag `capabilities`,
  `modified_at`, and size detail so the quick-setup station can prefer a
  tools-capable model and label downloads honestly.

### Changed

- `[S1] [D]` **A commit is the owner's deed, not reliably the owner's words:
  `github_activity` is ambient-posture and no longer fans commits into the
  journal.** The source declared `posture='personal'` ("owner-performed deeds")
  and mapped every PushEvent commit into a `journal_entries` row. But
  `journal_entries` is authored-by-construction in `provenance.roles` — a row
  there IS the owner's own writing, belief-grade, eligible to mint goals and
  self-facts — and commit prose is written by coding agents now. The gate meant
  to protect that lane keyed on a co-author TRAILER, so it correctly demoted
  `Co-Authored-By: Claude` and passed an identical agent-written message with no
  trailer; that blind spot is not fixable by a better regex. Both halves are
  retired: the fan-out is gone (the mapper is single-lane, and the `authorship`
  stamp now routes nothing), and `posture='ambient'` caps any row this source
  produces at `observed`, which the fact store's LLM gate honours ("an
  ambient-flagged source can never mint a belief here"). The lane existed
  because commit messages lived nowhere else — no longer true since they land on
  `activity_events.content`, where the role model already reads them as ambient.
  Retrieval, clustering, interests and attention triage are unaffected: activity
  rows are `ROLE_AMBIENT` by table either way. On the first live node checked,
  485 journal-lane facts existed and **zero** were belief-shaped (all entity
  mentions, no predicates), so this closes the door before anything came
  through it. The owner can still override per connector
  (`storage.source_settings` → `effective_posture`). Empirically, once commit
  text reached the topic layer the corpus surfaced
  `network_bridge :: "Topos (claude)"` as its third-largest cluster (65
  vectors) — the history saying out loud that much of it is "an agent worked on
  this with me".

- `[S1] [E:embeddings]` Ambient activity rows (browser visits) now embed ONE
  vector per distinct page text instead of one per visit: batch-level dedup in
  the embeddings job plus a cross-record `content_hash` check at the vector
  write (`has_duplicate_content`). A page revisited 30 times contributes one
  ANN neighbor, not 30 near-identical ones (August data: 1,034 visit rows →
  360 distinct titles). Message-family rows are exempt — every message keeps
  its own vector row so it stays individually retrievable. Reloaded activity
  batches without a `_table` stamp now classify as `activity_event` via their
  `activity_type`, so backfills get the same policy.

- `[E:llm]` Ollama pull/list hardening from the J-B10 verification rounds:
  monotonic pull progress survives a restarted layer, a garbage tag surfaces
  the Ollama error instead of polling forever, and a 502 answers
  `reachable: false` rather than an ambiguous empty list.

### Removed

- `[D]` **Mega-clusters split; labels read the canonical text.** Two coupled
  changes to the topics pipeline, both from the same live measurement: 23
  clusters of 120+ members held 51% of all members (one 147-member cluster
  mixed T-Mobile spam with housing logistics), and the stored member previews
  are redacted (`[NAME]`/`[ACCOUNT]`) — so the labeler was naming subjectless
  bags from de-specified text, and base-name collisions ("Cameron and
  Danielle" ×13) concentrated in exactly those clusters. (1) A cluster above
  `TOPOS_CLUSTER_SPLIT_SIZE` (default 120) members is re-clustered within
  itself; fragments go straight back through the similarity merge, so a
  genuinely coherent big cluster re-merges and stays whole — the merge is the
  split's veto. The parent id survives on the largest fragment. Rehearsed on a
  live-DB copy: 23 → 14 mega-clusters, mega-member share 51% → 33%. (2) The
  label prompt and distinguishing-term extraction now read the CANONICAL row
  (`_hydrate_label_texts`, in memory only, `TOPOS_CLUSTER_LABEL_RAW_TEXT=off`
  to revert); every persisted surface — embedding previews, member previews,
  centroid previews — keeps redaction exactly where and how it was, pinned by
  a test that persists a hydrated cluster and asserts the raw name never
  reaches disk. The off-limits gate still refuses protected names at the
  label. Owner decision 2026-08-15: labeling is an owner-side surface; the
  redaction that matters stays on the stored tables.
  (`topos/features/signal/topic_clustering.py`)

- `[P]` **The live-engine pressure tests stop failing 401 against a healthy
  node.** `tests/conftest.py` sets `TOPOS_KEY="test-key"` so Settings validation
  passes for the whole suite. `test_live_engine_pressure` guards itself with
  `if not TOPOS_KEY: skip`, which cannot tell that placeholder from a real key —
  so instead of skipping it sent `Bearer test-key` to the running node and got a
  401 on every assertion. Three sessions read those 401s as "engine not
  reachable" and waved them through as environmental, which is how a lane stops
  meaning anything. The module now resolves the key it should actually use:
  an explicit non-placeholder `TOPOS_KEY`, else the node's own `~/.topos/.env`,
  else skip with a reason that says which. Against a live node all four tests
  pass; with no key anywhere they skip rather than fail.
  (`tests/release/iteration4/test_live_engine_pressure.py`)

- `[S1]` **`url_classification` is retired — job, table, engine path and the
  interest tags it wrote.** The job labelled every visited page with a DMOZ
  top-level category. Six months on the node this was measured on produced
  8,616 rows, **73% of them the single label "Reference"** — at an average
  confidence of **0.961, higher than any other bucket**, so no threshold could
  separate it from filler (87% of Reference rows scored ≥0.95, and the lowest
  confidence bucket was `Computers`, the useful one). Labels were not stable per
  site, because the classifier reads the page TITLE and an authenticated app
  title carries almost nothing: `github.com` came back Reference 720 /
  Computers 125 / Business 23 / Kids 5, `google.com` was "Porn" twelve times,
  and `drive.google.com` was *majority* "Home". The skew was structural, not a
  regression — 61% Reference in February, 70–79% every month since.

  It is a taxonomy mismatch rather than a tuning problem. DMOZ categories
  describe where a site files in a web directory; they do not describe what a
  person is doing, and "Reference" is that scheme's catch-all.

  The rows reached `interests` through
  `scope_materializer._materialize_activity_tags`, which upserted each category
  as an `activity_tags` signal object: **458 of the dimension's 828 objects, 349
  of them "Reference"** — roughly 40% of the structured interest layer was that
  one string. The keyword-rule branch beneath it (edtech, ai_research,
  infrastructure, outdoors, privacy) is untouched and becomes the only path.

  Removed with it: `browser_url_classification` and its writer, the three job
  modules, the `website_classifier.py` compat wrapper, `build_url_classification_task`,
  `ModelSlot.URL_PIPELINE` and the backend's two inference paths, the catalog /
  registry / model-override / lab entries, the `/api/enrichment` test+backfill
  endpoints, and two readers that never fired anyway — `topic_clustering`'s ×3
  `url_category` term boost (members never carried the key) and the
  `url_category` fallback in stats grouping.

  Migration `retire_url_classification_v1` drops the table and deletes the tags,
  scoped by `payload_json.source_kind` so the rule-based tags and the personal
  `fact` objects that also live in `interests` survive. Rehearsed against a copy
  of a live node: interests objects 828 → 370, `fact` 6 → 6, other dimensions
  6,580 unchanged.

### Fixed

- `[S1] [E:retrieval]` **Attention-digest movers are date-qualified at read
  time.** The routine narrator reads the last ~10 daily digests, and a movers
  list carried no date of its own — so a one-day spike (28 visits to a new
  host on 07-31) was re-narrated in the present tense for 11 days as if it
  were news. Each movers fragment now carries its day and query-time age
  ("on 2026-07-31, 13 days ago — not current" / "yesterday" / "today"),
  computed when the object is served so the qualifier stays true however
  long it keeps being retrieved. A surprise is an event, not a state.
  (44bb721; entry deferred from that commit because the changelog was held
  by a co-resident session at the time.)

- `[S1] [E:embeddings]` **A journal row's signal dimension follows its origin,
  not its record type.** `signal_dimension` was mapped from record kind alone
  (`journal_entry` → `wellbeing`), and the GitHub connector writes one journal
  row per authored COMMIT — so the entire commit stream was stamped
  `wellbeing`. On one live node that was 123 of 765 journal embeddings, and
  because clustering facets by dimension, **19 of 163 topic clusters** came
  back named `Wellbeing Tracker (…)` over distinguishing terms like
  `merge branch`, `gitignore`, `build`, `layout`, `settings`. The labeler was
  not at fault and no amount of prompt work could fix it: asked to "name the
  state, rhythm or condition" for a cluster of commits, the dimension's own
  noun is the only answer left, and the parenthetical suffix ends up carrying
  all of the real signal. A commit is work; what a person writes in a journal
  is wellbeing. `dimension_for_record` now consults a `(record kind, source)`
  table before the kind-only one, so a single source's journal lane can differ
  from the rest without reclassifying anything per record.
  `journal_origin_dimension_v1` re-stamps the rows already on disk — unlike
  `signal_dimension_backfill_v1`, which only filled rows still at the `memory`
  default, this one matches the wrong value explicitly and leaves a dimension
  set deliberately to anything else alone. Clusters pick the split up at the
  next recompute, which the ingest job runner already triggers; nothing here
  forces a repartition, since a recompute reshuffles every cluster (two passes
  over one corpus agree only to ARI ~0.52).
  (`topos/features/signal/embed_context.py`)

- `[S1]` **A repeated cluster name earns its retry even when it arrives
  suffixed.** Not stacking the suffix stopped the runaway shape, but by the
  time `_disambiguated` sees a label the retry has already been skipped:
  collision was tested on the whole string, so a model answering
  "Social Connections (weekend)" where "Social Connections" was spoken for
  produced a brand-new string, matched nothing in `used`, and was accepted. The
  model was never told it had repeated itself and never got the chance to
  rename — it went straight to a deterministic suffix, which is a worse name
  than the one it was not asked for. And the prompt shows each cluster the
  names its siblings took, so a suffixed sibling is exactly what it copies.
  Collision now tests the base name alongside the whole label (`_collides`),
  and an assigned label claims both identities (`_register_label`) — including
  the term labels still carried by clusters the labeler skipped, which sit on
  the same surface. `_label_rank` uses the same test, or a rename would be
  bought by the retry and then discarded when the two disagreed. The eval gains
  `stacked_suffix_labels` beside `suffixed_labels`: one suffix is the
  disambiguator working, two is it running away, and that difference deserves
  its own number rather than hiding inside a count.
  (`topos/features/signal/cluster_labels.py`)

  How many labels share a base is still deliberately not capped — keeping the
  term label instead ("https / good / here") trades a weak name for a useless
  one, and a model that repeats itself must still get every cluster named.

- `[S1]` **Model readiness stops counting jobs ready because they are not LLM
  jobs.** `_model_readiness()` scored every non-LLM job ready unconditionally —
  "HuggingFace task models and rules jobs run in-process on any supported
  device", which is true only once the weights are on disk. A machine set up
  offline has nothing cached and was still reported at **100% model readiness**
  for every dimension those jobs feed, which is precisely the machine that most
  needs to be told otherwise. Readiness is now per job, from the model catalog:
  ollama jobs need a reachable server (and a model above the size minimum),
  HuggingFace jobs need their snapshot present in the local HF cache, rules jobs
  need nothing. The per-dimension profile gains `model_readiness_jobs` —
  `{job, provider, model, ready, reason}` per job — so a dark dimension can say
  *which* model it is waiting on ("the NER weights are not downloaded yet")
  instead of only scoring low. `data_health._LLM_JOBS`, a literal that
  duplicated what the catalog already states, is now derived from it via
  `jobs_for_provider("ollama")`. (`topos/enrichment/job_readiness.py`,
  `topos/features/signal/data_health.py`)

  Readiness and *holding* are deliberately separate questions:
  `readiness_of().ready` answers "can this run now" and is what a person is
  shown; `.blocking` answers the narrower "should queued work WAIT", and only a
  hard stop sets it. Uncached HuggingFace weights make a job not ready but never
  blocking — the first run downloads them, so holding that debt would strand
  work a networked node completes unaided.

- `[S1]` **A deferred derivation job now records durable debt.** Jobs report an
  unreachable provider by RETURNING `[{"_deferred": True, ...}]` rather than
  raising, and only the raise path called `record_failed_derivation()`. So the
  exact case the debt mechanism exists for — ingest with no model installed,
  install one a week later — wrote nothing to `pipeline_jobs`: the worker and
  `POST /signal/derivation-debt/retry` had nothing to find, and the only durable
  trace was an `ingest_audit` row naming the batch but not the job (the
  orchestrator's `deferred_jobs` list, which does name them, is dropped at end
  of batch). The deferral path now writes the same record as the raise path,
  idempotent per (batch, job) so re-ingesting while still offline re-queues the
  one row instead of stacking. Affects `topics` and `goal_extraction` on
  `ollama_unreachable`, plus the seven canonical jobs that defer on
  `database_unavailable`. (`topos/enrichment/orchestrator.py`)

- `[S1]` **A retry that defers again is no longer reported as recovered.**
  `retry_single_derivation()` checked only `results["errors"]`, which a deferral
  never reaches — so re-running a debt while the provider was still down fell
  through to `outcome: "recovered"` with zero rows created, discharging the debt
  and marking the queue row `done` while the data stayed missing. That made the
  retry claim to have repaired data it never produced, and it under-counted
  `pending_derivation_summary()`. The deferral is now `still_failing`, carrying
  the job's own reason. (`topos/enrichment/derivation_recovery.py`)

- `[D]` **`grand-cypher` pin moved to `>=0.13,<0.14`** (was `>=0.12,<0.13`, in
  both core dependencies and the `engine` extra). The ceiling is load-bearing
  rather than cautious: 1.0.0+ changed `RETURN a` to yield the whole node dict
  instead of the node id, which breaks `_rows_from_columns` and
  `test_entity_cypher.py::test_result_is_json_serialisable`. 0.13.0 is the
  newest release this code works against — the upper bound must not be raised
  without re-running that test. (`pyproject.toml`)

- `[S1] [E:embeddings]` **GitHub push events carry their commit messages.**
  `activity_events.content` was NULL on every push row (451/451 on the first
  live node checked, 11 distinct titles across all 451 — all of the form
  "{owner}/{repo}: pushed N commit"), because the mapper carried only
  repo/actor. Every semantic reader downstream could therefore only ever match
  the repo NAME: goal/interest attachment to engineering work was pure name
  matching, a contrastive term extraction over commit-attached items returned
  nothing but "pushed"/"commit", and the derived "creation" signal counted
  events that said nothing about what was created. `github_activity` now
  declares `activity_events.content ← payload.commits[*].message` (joined for a
  multi-commit push) in its source definition — the first bundled consumer of
  the declarative capability above. Event granularity is unchanged: one
  activity row per push, and — as of the journal-lane retirement below — that
  row is the only place the commit prose lands.

- `[S1] [E:embeddings] [E:entities]` **activity_events writes now persist
  `content` and `hostname`.** The `activity_events_content_v1` migration added
  both columns and the P2.1 browser mapper filled them, but the store's INSERT
  never listed them — so every value was discarded at the write (0 of 4,444
  rows populated on the first live node checked, browser highlight spans
  included). Both columns are in the INSERT and in the conflict update
  (COALESCE, so a blank batch never blanks a stored value), which also means a
  re-ingest or reprocess-from-raw heals rows written before this fix.

- `[S1] [E:embeddings] [E:entities]` The activity signal record and the
  signal-reload query now carry `content`, `hostname` and `metadata_json`.
  Without them an activity row embedded on its title alone, and the declared
  ENTITY mapping (§5a capability 4, `metadata_json.repo` → project +
  `worked_on` edge) was reading fields absent from the record it was handed.

- `[S1] [E:embeddings]` Runtime installs of bundled sources no longer shadow
  the bundled enrichment-lane policy. A 2026-05-29 `source_runtime_installs`
  row for `browser_visits` snapshotted the then-bundled definition
  (`enrichment_trigger='manual'`, no jobs); rehydrated at every boot it
  overrode the 2026-07-09 flip to automatic url_classification+embeddings, and
  once the manual gate landed on the signal lane every live browser push ran
  zero enrichment jobs while reporting "done" (August: 0/1,034 visits embedded
  or url-classified across 2,304 no-op inbox jobs). `browser_events`
  highlights, and the newer signal jobs on `chatgpt_ui_conversation`,
  `imessage`, `signal`, and `gcal_events` (availability_scores), were silently
  dropped the same way. Lane policy (trigger + job lists) now always resolves
  from `BUNDLED_REGISTRY` for bundled source ids; per-source toggles remain in
  `source_enrichment_overrides`.

- `[D]` **Topic-cluster labels are named against their siblings, under their
  dimension's question.** Every cluster was named by one prompt that saw only
  that cluster's own most frequent terms: no idea what the neighbouring
  clusters were already called, and the same generic instruction whether the
  cluster was built under the wellbeing lens or the interests one. For a
  personal corpus the most frequent terms are exactly the words every cluster
  shares, so labels collapsed toward the generic — one live node carried 152
  clusters under 91 distinct labels, "personal projects" on fourteen of them
  spanning five dimensions. Four changes: frequent terms become
  DISTINGUISHING terms (class-based TF-IDF — this cluster's share of a term
  against the rest of the set, so a word the whole corpus uses scores zero
  instead of leading every list); each prompt opens with the question that
  dimension exists to answer, read from its own
  `topos/features/signal/definitions/<dimension>.json`, plus one line saying
  what kind of name answers it; the prompt carries the names the nearest
  siblings already took, with clusters named in a deterministic order
  (dimension, then size) so those names exist when they are needed; and an
  answer that comes back generic, already-taken or as a link earns one bounded
  retry naming the problem, then a deterministic suffix that is checked against
  every name in play — including the term labels still carried by clusters the
  model declined. Measured relabel-only over the same 152 clusters with the
  same local model (`scripts/eval_cluster_labels.py`): **93 → 151 distinct
  labels**, worst duplication **13 → 2**, clusters carrying a duplicated name
  **75 → 2**, names duplicated across more than one dimension **10 → 0**. The
  control arm reproduces the live node to within two labels (93 vs 91 distinct,
  "personal projects" 13 vs 14), which is what makes the comparison the prompt
  and not the run. `TOPOS_CLUSTER_LABEL_CONTRAST=off` restores the isolated
  prompt (it is that control arm); `TOPOS_CLUSTER_LLM_LABELS=off` and the
  deterministic term-label fallback are unchanged.
  **Correction (measured later, same corpus): the 151 counts full labels, and
  a full label can carry the `(term)` suffix the deduplicator appends. Counted
  by BASE name — suffix stripped — the same run holds only 113 distinct names,
  with 41 labels suffixed and one base repeated fifteen times. So the honest
  figure for this change is 91 → 113 distinct base names, not 91 → 151, and
  the suffix inflates the word-count rule below by the same stroke. The eval
  now reports `distinct_base_labels` as its headline number.**
  (`topos/features/signal/cluster_labels.py`)

- `[D]` **A cluster label can no longer be a link.** Distinguishing terms are
  drawn from page titles and links, which invites the model to answer with one:
  a measured relabel returned a full `maps.app.goo.gl/…?g_st=i&utm_…` URL as a
  cluster label. Labels are published — into `top_topics` signal facts and every
  surface that lists topics — and the interests definition bans verbatim URLs
  there outright. URL, bare-domain, path and @handle answers now earn a retry
  and, if they survive it, are dropped in favour of the term label. An
  all-lowercase answer is title-cased deterministically (two thirds of live
  answers came back in prose case).

- `[D]` **A cluster label must be 2-5 words, checked and not just asked for.**
  The contrastive prompt opens with that rule and, measured over a full
  relabel, obeyed it LESS than the prompt it replaced: 46% of answers in range
  against the isolated prompt's 90%, single-word answers going 15 → 81
  ("Met", "America", "Friend", "Internet"). Nothing enforced it — `parse_label`
  capped the top at seven words and had no floor — while every other line
  pushed the other way, since each per-dimension directive asks the model to
  *name a thing* and distinguishing terms arrive as bare tokens. Bare proper
  nouns are unique, so this was invisible in the duplication metrics: the
  labels got more distinct and less informative at the same time. A
  wrong-length answer now earns the same one bounded retry the generic and
  link answers do, naming the specific fault ("\"Austin\" is one word. Keep it
  and add what about it"). Length ranks BELOW duplication and genericness when
  the retry is scored, so a distinct bare noun still beats a repeat, and a
  stubbornly terse answer ships rather than reverting to `https / good / here`.
  Measured on the same 152 clusters: rule compliance **46% → 77%**, single-word
  labels **81 → 34**, with distinctness unharmed (151 → **152 of 152**, worst
  duplication 2 → 1) and banned words still **0%**. Still short of the control
  arm's 90%: the 34 that remain are mostly real proper nouns (`Gmail`,
  `Brooklyn`, `Smithers`, `Dialoguesai`), which the rule above arguably wants,
  alongside a weaker tail of bare common nouns (`Friend`, `Food`, `Account`).
  `scripts/eval_cluster_labels.py` reports `word_rule_share` and
  `single_word_labels` so this cannot regress unnoticed again.

- `[D]` **Repeated term labels are disambiguated against each other, not just
  counted.** The suffix for a repeat was the cluster's member_count, which is
  not unique: two 7-member clusters that both reduced to "hello" both became
  "hello (7)", and a live node carried exactly that pair. Every candidate
  suffix is now checked against the labels already handed out, with the
  occurrence index as the fallback that cannot collide. This was the only
  duplicate the LLM labeler could not resolve, because it inherits the term
  label whenever the model declines a cluster.

- `[D]` **Existing clusters can be relabeled without being re-clustered.**
  `relabel_existing_clusters()` runs the labeler over the clusters already on
  disk and writes back labels only — membership, cluster ids and centroids are
  untouched, and the deterministic term label is preserved in
  `metadata.term_label`. A prompt change owes existing nodes new names, not a
  new partition: two k-means passes over the same corpus agree only to ARI
  ~0.52, so shipping this through a recompute would churn every cluster id,
  `top_topics` fact and UI link with it. Reachable as the `derived_rebuild`
  target `topic_cluster_labels` and as the release step
  `recompute-topic-clusters-split`; `scripts/eval_cluster_labels.py` drives the same
  path against a temp copy of the database to A/B two prompts over one fixed
  partition. **Clusters keep their old names until that step runs**, and this
  build reaches a node by reinstall (frozen uv tool snapshot), not restart.
  (`topos/features/signal/topic_clustering.py`, `topos/upgrades/runner.py`)

- `[D]` **A topic cluster label is now a black-hole surface.** Making labels
  specific made them name people: 33 of 152 labels on a live node came back as
  an entity name (six people, four places) where the old term-soup labels named
  nobody. The black-hole battery had no token in `topic_clusters` at all — an
  unenumerated leak surface — and the withdrawal job's list stopped at the
  derived `top_topics` object, which is minted *from* `topic_clusters.label`, so
  a rebuild's withdrawal only lasted until the next cluster pass. Both
  directions of time are now covered: the labeler refuses to mint a name in the
  owner's off-limits set (one retry that never echoes the rejected name back
  into the prompt, then the deterministic term label), and the rebuild withdraws
  labels, centroid previews and member excerpts written before the entity was
  protected. The cluster, its membership and its counts survive — withdrawal
  takes the prose, not the owner's structure. `rerun-blackhole-rebuilds` applies
  it to entities already marked complete, which `run_pending_rebuilds` skips by
  design: on the live node three completed rebuilds still had **seven member
  excerpts quoting a protected entity** (five for one, two for another).
  Probes: `tests/evals/privacy/blackhole/test_cluster_labels.py` (16), plus the
  surface and its two canary tokens in the battery's corpus.
  (`topos/features/signal/cluster_labels.py`,
  `topos/features/lifecycle/blackhole_rebuild.py`)

## [1.3.15] — 2026-08-13

### Added

- `[O]` Select Topos in the menu-bar/system-tray icon, so machines without the
  desktop shell can switch between the Topoi they hold. The submenu lists every
  Topos with its size, ticks the active one, and offers New Topos. This tray has
  no supervisor — run in-process it IS the node — so a click queues the action
  and stands the server down; the swap runs once the port is free and the
  process re-execs onto the chosen Topos. A tray *attached* to a node it did not
  start is not offered the row at all: stopping a node it cannot restart would
  move a database out from under the machine and leave nothing serving. New
  Topos deliberately does not re-exec, because a node with no key exits within a
  second and would bury the pairing instructions under an error.

## [1.3.14] — 2026-08-12

### Added

- `[O]` `topos-node profile` — multiple Topoi on one machine. `list`/`current`
  show the active Topos (top-level `~/.topos`, unchanged layout) and archived
  ones under `~/.topos/profiles/<slug>/`; `new` archives the active Topos and
  leaves the machine fresh for a new pairing (the zero-click "New Topos"
  primitive — pairing can no longer hit the already-bound refusal); `switch`
  swaps the active Topos for an archived one. Only an explicit allowlist moves
  (`.env`, `database.db` + WAL/SHM sidecars, `ingestion/`, `nightly/`,
  `config.yaml`) — backups and scratch files stay put, and logs stay
  machine-global. Every rename is journalled, so a crash mid-switch rolls back
  to the pre-switch layout on the next profile command. Refuses to run while a
  node answers on the port or a database rebuild lock is present. `adopt`
  copies a legacy (`~/.topos_engine`, Application Support) database into an
  empty active slot for pre-profile machines. No derived data is invalidated —
  a profile switch changes which files sit at the top level, nothing else.

- `[O]` LLM stream protocol v1 (PLAN_HOME_CHAT_STREAMING_SLA P1). Streaming
  `llm_generation` now resolves `think` like the non-stream path (default OFF,
  capability-probed, budget floored to 2048 on explicit think-on) instead of
  leaving reasoning models to burn the whole `num_predict` on chain-of-thought
  the loop then discarded — the 2026-08-11 home-chat stall. Thinking tokens are
  forwarded as typed `kind:"thinking"` chunk frames; the handler emits an
  `ack` frame before model work and phase-aware `heartbeat` frames while the
  stream is silent (all interim traffic stays `status:"chunk"` with an empty
  `delta`, which older control planes provably no-op). An exhausted budget is
  a typed `thinking_budget_exhausted` error, never an empty success; a
  truncated oversized prompt gets a soft `context_truncated` verdict on the
  result. New `llm_cancel` message aborts an in-flight generation (the httpx
  unwind closes the Ollama connection, freeing the decode slot) and the
  original request answers a typed 499. Chat generations set
  `keep_alive=30m` so a follow-up question does not pay a cold reload.
  Registration and heartbeats advertise `llm_stream_protocol_version: 1` for
  control-plane feature detection. No stored data is touched.

- `[O]` `ollama_list_installed` / `ollama_pull` / `ollama_pull_status` message
  handlers, so the control plane can seed model packs from what is actually
  installed on this node and drive a model download with typed progress.
  Registration now reports the installed-model set honestly (installed /
  empty / unknown — never unknown-as-empty), and the node-side pack config
  readers (facts, conversation context, signal extraction, sanitization)
  fall back to the engine default instead of a pack tag that is not
  installed. No stored data is touched.

## [1.3.13] — 2026-08-11

### Fixed

- `[S1] [E:entities]` The People tab's duplicate queue asks each question once.
  `entity_review` holds a row per *sighting*, and the resolver queued one every
  time a surface reappeared: 8,134 pending rows for 99 real decisions on a live
  node, the same four pairs repeating down the page, with the genuine person
  merges sorted below all of it and off the visible page. Everything
  owner-facing now keys on the decision rather than the row — the page limit
  counts questions, resolving one settles every row that asked it, and the count
  reports the backlog instead of the page size. Existing queues collapse by
  migration; every prior answer and unbind guard is preserved.
- `[S1] [E:entities]` Three reasons a duplicate question should never have been
  asked. Graph derivation minted vertices through the same call path as ingest
  and asked about strings the owner never said (97% of the rows). A candidate
  never seen in the data is not something to merge into — contacts included,
  because an address book is not a list of important people, and 35 of 39 open
  contact questions offered a never-mentioned contact. And `"Altman's"`,
  `"Williamsburg-"` and text redacted to `[NAME]` are no longer identities.
- `[E:entities]` Orphan cleanup stops reaping the vertices derivation just
  minted. They carry ordinary `ent_` ids, so neither the type nor the id-prefix
  exemption saw them: every scrub deleted 173 mention-less entities on a live
  node and cascaded away the 206 edges that made them load-bearing, and the next
  derivation run rebuilt them. An edge is reason enough to keep a vertex.
- `[S1]` Search returns one result per thing said, not per time it was stored.
  The same text is embedded once per sighting, and 37% of a live corpus (2,967
  of 8,061 embeddings) is duplicate text, so `"GitHub"` took four of five slots
  for the query `git github`. Results now collapse on content hash, keeping the
  best-scoring copy and reporting how many sightings it stands for in
  `occurrences`. The candidate fetch widens to match, and the collapse runs
  before reranking so the cross-encoder's budget is spent on distinct text.
- `[O]` Five documented environment variables were dead. pydantic v2 ignores
  `Field(env=)`, so `TOPOS_FACTS_LLM_MODEL`, `TOPOS_OLLAMA_QUERY_MODEL`,
  `TOPOS_OLLAMA_EXTRACTION_MODEL`, `TOPOS_DISCLOSURE_MINIMIZER_MODEL` and
  `TOPOS_PRIVACY_JUDGE_MODEL` never reached `Settings` despite being documented
  as the way to set them.

## [1.3.12] — 2026-08-10

### Fixed

- `[E:graph]` The entity graph stops dating relationships by when the machine
  touched them. Four defects, one theme. (1) `rebuild_evidence_edges` deletes
  and re-inserts the whole co-occurrence set and stamped a fresh `now` on every
  row, restarting each edge's belief clock — 492 edges on a live node claimed to
  have begun at whichever rebuild happened to run last, and the prior date was
  gone for good. `valid_from` is belief validity, not an event date, so the
  prior date is now carried across the swap and only genuinely new edges begin
  believing now; it is deliberately NOT clamped to the evidence date, which
  would assert a belief history that never happened. (2) Materialized topic
  edges carried `actor_role: authored` without exception: nobody asserts a topic
  cluster, so they passed no `asserted_by` and the fallback's "owner" default
  claimed the owner wrote them — putting browser and GitHub-feed exposure in the
  owner's own voice and reading 91.5% authored on a window where it was a
  fraction of that. The role now comes from the cluster's member records via the
  same role map the co-occurrence edges use, so a topic edge and an organic edge
  over the same records cannot disagree; on the measured node 34 clusters moved
  from 34 authored to 2 authored / 15 observed / 17 ambient. (3) `entry_at` was
  written from the import clock while the true session time sat in `starts_at`
  (309 rows across two sources, 127 sharing one second), so months-old sessions
  looked like they happened at import and pulled years-old relationships into
  the recent graph window; the canonical store now prefers `starts_at` when
  `entry_at` matches `ingested_at` to the second, and an upgrade step re-dates
  rows already on disk. (4) DATE/TIME/CARDINAL surfaces ("an hour", "this week",
  "four", "Mon-Wed") were minted as first-class topic nodes: the extraction lane
  already drops value labels, but the graph's second minting lane resolves bare
  surfaces with no label attached, so `map_ner_type` never ran. Both lanes now
  consult the labels the model already assigned — a surface counts as a value
  only when value-labelled mentions outnumber identity-labelled ones, so a real
  entity mislabelled once survives — and 51 previously-minted junk nodes are
  purged. Verified end-to-end on a live node: 20 junk nodes to 0.

- `[O]` A failed upgrade step is retried again, and the banner that promises
  the retry stops lying. Three defects stacked. (1) The runner planned purely
  from `steps_between(baseline, shipped)` and filed ledger rows under the
  SHIPPED version rather than the release that declared the step, so a failure
  detached from its step: `backfill-attention-triage-redo` (declared 1.3.7)
  failed under shipped 1.3.9, the baseline went on to 1.3.11, and the step left
  the version window permanently — unreachable by the planner and by
  `POST /v1/upgrade/consent`, which rejects anything not in the current plan.
  Rows are now keyed to the declaring release, the plan carries any step whose
  ledger row never reached `done`, and the baseline stamp is gated on that same
  set, so a release with no steps of its own can no longer stamp straight over
  an outstanding failure. A step with no row at all is still never dragged
  back — fresh installs stamp without running history, and re-running all of it
  would be far worse than the gap. (2) `UpgradeBanner` filtered the ledger on
  `status === "failed"` with no version scope and no check against
  `pending_steps`, so one historical row pinned "Upgrade step failed — will
  retry on next restart" to `/data` forever, and the step counter drew its
  numerator from every `done` row the node had ever written. Both are now
  scoped to the current plan, and the retry sentence only appears when a retry
  is genuinely planned. (3) The step had died on its progress bar, not on any
  data work: `ProgressBar` writes to `sys.stderr`, and upgrade steps run in a
  daemon thread ~20s into boot, where a detached node (`--app --no-tray`) can
  have stderr closed — `__enter__`'s first draw raised
  `ValueError: I/O operation on closed file.` six seconds in, before a single
  source was touched. Display writes are now swallowed and disabled after the
  first failure, which also covers the ingestion manager, the iMessage reader,
  and the emo-27 job.

## [1.3.11] — 2026-08-09

### Fixed

- `[O]` The conversation auto-classifier stops re-asking the same question
  forever. It ran on every enrichment batch, took the newest 20 conversations
  with no `context_tag`, and asked the local model "work or personal" — but
  when the model answered `unclear`, nothing was written. Those rows stayed
  untagged, sat at the top of the same `created_at DESC` window, and were
  re-sent verbatim on the next batch. Measured on a real node: ~20 model calls
  and ~26 seconds burned per derive batch, permanently, for zero progress —
  the token counts repeat in identical order across passes. 64 of the 68 stuck
  conversations were `test-dataset` fixtures with one or two messages, far too
  thin to ever label, so they taxed every real batch. An unclear verdict now
  retires the row with an `unclear:n<k>` marker recording how many excerpts
  were judged; `context_tag` still stays NULL, so nothing is guessed. A retired
  conversation becomes a candidate again only once it has grown new excerpts to
  judge, and since the excerpt read caps at 8 a conversation at that cap can
  never pose a different question and never returns. Retired rows only ever
  fill budget left over by never-asked ones, so a backlog cannot crowd out new
  work. Engine failures are now distinct from an unclear answer and never
  retire anything — an Ollama outage previously looked identical to "the model
  declined", which would have buried 20 rows that were never actually judged.
  Verified against a copy of the live database: 4 passes to drain a 68-row
  backlog, then 0 model calls per batch, down from 20 forever.

## [1.3.10] — 2026-08-09

### Fixed

- `[O]` The control-plane connection no longer drops every 50 seconds. 1.3.9
  restored a pong deadline so a silently dead socket could not wedge the node
  forever, but 20s was far tighter than the engine can meet: measured live,
  pongs round-trip through the tunnel in ~0.2s and the app loop answers
  healthchecks throughout, yet the node still could not always *process* its
  pong inside 20s. The link then died on a metronome — 20s to first ping + 20s
  deadline + 10s close handshake — 18 times in one session, and each drop took
  whatever was in flight with it, including a user's chat message: submitted,
  processed, then gone, with no trace in history. The deadline is now 30s
  interval / 90s timeout: far past any observed local stall, still detecting a
  genuine death inside ~2 minutes, and inside the control plane's own eviction
  (30s + 120s) so the node notices first and reconnects itself. Verified on a
  real node: zero drops and a single stable connection across 15 minutes, where
  the previous build produced a drop every 50 seconds.

- `[O]` In-app "Update to vX" works for app-installed nodes. A Finder-launched
  app inherits `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, where `uv` never lives, so
  `uv tool upgrade` raised `FileNotFoundError`, the worker's `finally` stamped
  `last_result="failed"`, nothing reached the log, and the menu silently
  reverted to offering the update again — indistinguishable from a dead button.
  In-app update had therefore never once worked for a DMG install.
  `resolve_uv_binary()` now checks `TOPOS_UV_BIN` (which the macOS shell 0.2.13
  passes from its bundled copy), then `PATH`, then the usual install locations;
  failures log the reason and the `PATH` that was searched, and `OSError` is
  caught instead of killing the worker thread.

### Changed

- `[O]` Tray parity with the macOS shell, for the Windows port that wraps it:
  "Quit Topos Node" stops the node whether this process started it or merely
  attached (previously an attached tray offered only "Close Tray (node keeps
  running)", stranding anyone whose tray attached after a crash or an update
  restart with no way to ever stop the node); the tray-only exit survives as an
  explicit second item shown only when attached; a failed update says so
  instead of rendering nothing; the badge spins while the node is starting; and
  the bound Topos's own name appears in the menu.

## [1.3.9] — 2026-08-09

### Fixed

- `[S1]` `public_bio:read` and `work_context:read` can reach profile data
  again. The 2026-07-03 registry sync (`bee78d5`) replaced the engine scope
  registry wholesale from the control plane and dropped `profile_records` from
  both scopes, leaving no scope in the registry able to reach ingested
  resume/profile data. `public_bio:read` was left with no readable lane at all
  — its only declared surface was the summary object `public_bio`, a name no
  migration creates, and retrieval never reads `summary_objects` (they declare
  which access modes a scope supports, nothing more), so it answered every
  query with `{"summaries": []}` while advertising
  `implementation_status: "live"`. `profile_records` is restored to both scopes
  upstream in `control_plane/uma/scope_registry.json` and re-synced; the
  generated `scopeCatalog.ts` is unchanged, because the frontend's view of both
  scopes stays summary-only. It is declared as `canonical_tables`
  rather than `raw_tables` deliberately: both manifest builders read
  `raw_tables or canonical_tables` for the content lane, but only `raw_tables`
  feeds `_max_supported_access_mode`, so using it would have lifted both
  scopes from their declared `summary` ceiling to `raw` and un-denied the
  inference and raw requests `TestQueryPipelineDenyPressure` exists to reject.
  The engine's retrieval already carried the `profile_records` handling this
  depends on — the certification and prior-employer filters name the table and
  the `work_context:read` scope directly. Harness quality on the seeded gate
  DB goes 35/70 → 42/70 with no regressions and no change to any denial, and a
  new `tests/query/test_scope_registry_surfaces.py` fails any live scope that
  declares no lane retrieval can read.
- `[S1]` Derivation-retry jobs actually run. `record_failed_derivation()`
  enqueues work of kind `signal_derive_retry` so a lost derivation can be
  re-derived, but no executor was ever registered for that kind: the worker
  claimed each row and immediately failed it with `Unknown job kind:
  signal_derive_retry`. Every recorded derivation debt was therefore
  self-cancelling — the retry that was supposed to repair the gap destroyed
  the record of it instead. `run_derivation_retry_job` is added and wired into
  `EXECUTORS`, and `tests/pipeline` now asserts the registration the way
  `topic_consolidation` already did (the assertion that would have caught
  this). Workers also claim only kinds they can execute, so a row written by a
  newer node version stays queued and visible instead of being claimed and
  failed as unknown, and an executor may return `status: "requeue"` to hand a
  claim back untouched when it declines to run — a deferral must not read as a
  failed attempt. Registering the executor does not revive rows already parked
  `failed`, so this release also carries an `auto` upgrade step
  (`retry-recorded-derivation-debt`) that sweeps them once: nothing else would
  — a failed row is only re-queued by the next organic failure of the same
  (batch, job), and `recover_stale_jobs` resets `running` rows, never `failed`
  ones. Nodes with no recorded debt run it as a no-op.

### Changed

- `[O]` `just harness-gate` scores answer quality against a `QUALITY_FLOOR`
  ratchet instead of a flat 70/70. Permission and Signal stay hard 70/70 —
  those are disclosure and retrieval-liveness checks and do not depend on how
  derived the database is. The 70/70 quality baseline was recorded on a fully
  derived node; the gate seeds a throwaway DB where the vector and cluster
  layers are switched off by design, so a share of the catalog is
  unanswerable there by construction and the gate could never go green. The
  floor is the high-water mark on that environment (now 42/70, measured on
  main at `39bf20b`) and the recipe says so when a run beats it.

## [1.3.8] — 2026-08-09

### Added

- `[O]` Open-transaction watchdog behind `TOPOS_DB_TXN_WATCHDOG=1`
  (threshold via `TOPOS_DB_TXN_WATCHDOG_SECONDS`, default 5s). The existing
  ungated-write diagnostic only fires at commit time, so a caller that
  writes and NEVER commits was invisible — while still holding SQLite's
  RESERVED lock, which makes every other connection's first write wait out
  the full 30s `busy_timeout` and fail `database is locked`. That blind
  spot hid `StatisticsJob._should_promote` (an ungated, never-committed
  `UPDATE stat_state`) wedging the whole signal lane on repeat ingests, and
  diagnosing it took a throwaway `sqlite3.Connection` subclass plus a
  polling thread. Now a daemon thread reports any connection in-transaction
  past the threshold outside the gate, naming the call site that opened it,
  one rate-limited WARNING per site. Default off: it costs +1.6us per
  statement on Python 3.10 (83% of that sqlite3's own trace-callback
  trampoline), and a diagnostic that taxes the hot path becomes its own
  outage. Off, it is one boolean test per connection open.

### Fixed

- `[O]` The graph refresher no longer bumps the dirty generation on a
  connection another thread owns. `mark_graph_dirty` persists from a
  default-executor thread on the stated promise that the thread "fetches its
  own connection" — true for a file-backed database, and impossible
  otherwise: an in-memory database hands out the OWNER's connection, because
  a per-thread copy would be empty, and tests inject a single handle of their
  own. Writing anyway raced whoever already held it. A `sqlite3.Connection`
  carries exactly ONE transaction state, and `with_db_write` serializes
  writers, not readers, so nothing excluded the other thread; on CPython 3.12
  that took the whole test lane down with SIGSEGV — a crash, not an
  exception, with no failing test to point at. The ownership check now lives
  where the SQL is: the write proceeds on this thread's own connection, or on
  the owner thread, and is skipped otherwise. A file-backed node is always
  the first case, so the persist stays off the event loop exactly as before.
  This had been latent since the refresher landed; moving ingest and
  enrichment writes onto worker threads changed the timing enough to make it
  land nearly every run.

- `[O]` In-app update works for DMG installs. It never had: a GUI-launched
  app inherits `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, `uv` lives in
  `~/.local/bin`, so the update subprocess raised `FileNotFoundError`, the
  worker stamped `last_result="failed"`, nothing reached the log, and the
  tray silently reverted to "Update to vX" — reported as "I click it and
  nothing happens". `resolve_uv_binary()` now checks `TOPOS_UV_BIN` (set by
  the macOS shell from 0.2.13), then PATH, then the usual install
  locations, and finally the app's own bundled uv at
  `/Applications/Topos.app/Contents/Resources/uv` — the only copy a user
  who never installed uv by hand actually has. Failures log the reason and
  the PATH searched instead of vanishing, and the tray offers "Update
  failed — click to retry".
- `[O]` Restore the control-plane pong deadline (20s interval, 20s
  deadline). `ping_timeout=None` was a workaround for handlers blocking the
  client loop; giving the client its own thread removed that reason, but
  the workaround stayed and was strictly worse — with no deadline a
  silently dead socket is never noticed, so the node reports "connected"
  indefinitely while the control plane holds no socket for it. Observed
  live 2026-08-08: wedged 25 minutes, every proxied request 503, no
  recovery without a restart. The deadline sits inside the control plane's
  own 120s eviction, so the node notices first and reconnects itself.
- `[O]` The `statistics` signal job no longer fails repeat ingests with
  `always-run migration 'wiki_mvp_phase1' failed: database is locked`. Two
  faults compounded. Its debounce counter (`_should_promote`) issued an
  `UPDATE stat_state` outside the write gate and never committed it, so the
  event loop's connection took SQLite's write lock at execute time and held
  it for the rest of the pipeline; every other thread's first write then
  waited out the full 30s `busy_timeout` and failed. And
  `ensure_migrations_applied` re-applied every `always_run` step on each
  call — it runs from `AdapterFactory.create`, i.e. per batch, per worker
  thread, per connection — so an ingest issued hundreds of gated write
  transactions to re-assert schema that was already there, giving that
  stuck lock the widest possible blast radius. The counter update is now
  gated and committed, and the runner memoizes connections already at the
  registry head, keyed on `PRAGMA schema_version` so that any DDL re-arms the
  `always_run` steps — they are not just re-assertions, they ALTER tables
  that legacy DDL creates *after* the first run (`CanonicalTablesManager`
  builds `ai_chat_conversations` without the provenance columns and
  `wiki_mvp_phase1` adds them on the next pass). Measured on a three-ingest
  dev node: 14 `database is locked` → 0, two failed signal batches → 0, and
  ingests 2–3 went from 0 signal jobs in 308s/171s to all 10 in 30s/21s.

### Changed

- `[O]` CI no longer runs the Home chat Wave A retrieval-quality harness:
  it drove scripts under the gitignored `demo/`, so it failed on every run
  from 2026-07-05 and masked the privacy-eval, package-metadata and
  fresh-install smoke steps behind it. The harness moves to
  `just harness-gate`; no test coverage was lost (its pytest files are in
  the public lane).

## [1.3.7] — 2026-08-08

### Fixed

- `[O]` The write-gate / event-loop deadlock class is gone. The control-plane
  websocket client now runs on its own thread and event loop, so a busy app
  loop can never starve the keepalive and make a healthy node look offline;
  `get_device_info` is answered from a snapshot when the app loop is
  saturated. Previously one long write hold could freeze the node until
  SIGKILL (observed 2026-08-07).
- `[O]` The entity-graph rebuild runs in a subprocess. The 100–160s rebuild
  used to starve the event loop through the GIL even with the gate released —
  healthchecks went dark for ~100s per rebuild. Verified live: a full 120s
  rebuild with zero missed healthchecks. The rebuild also folds edges in
  memory and swaps them under one short gate hold, and the rebuild endpoint
  itself runs off the loop.
- `[O]` Every SQLite write+commit path in the tree (73 files, including the
  migration runner and all enrichment/feature writers) now holds the process
  write gate around its statements *and* its commit. Ungated writes inverted
  lock order against gate holders and stretched writes into 30s busy-timeout
  standoffs ("database is locked" batches). Diagnostics now warn on gate
  acquisition from the event-loop thread and on commits whose statements ran
  ungated, so regressions name their call site.
- `[O]` Startup DB work (stage9 column renames + source-install rehydration)
  runs on one dedicated worker thread, cancellation-safe, with pipeline
  recovery armed only after it completes — startup no longer blocks the loop.
  The owner-connection validate/evict/swap in `core.state` is serialized
  behind a lock, fixing a cross-thread use-after-close that could crash the
  process (SIGBUS) when the database path changed at runtime.
- `[O]` Enricher gate holds shrunk: goal-embedding compute moved outside the
  gate hold, dossier refresh chunked per entity, and slow holds are named in
  the log instead of appearing as anonymous multi-second stalls.
- `[O]` Dev tray (`topos/cli/tray.py`): quit means quit — quitting stops the
  node it attached to instead of leaving it running; the node row shows the
  Topos's own name.
- `[O]` The 1.3.0 `backfill-attention-triage` upgrade step was a guaranteed
  no-op on every node: `attention_triage` lives in `SIGNAL_DERIVATION_JOBS`,
  but the runner routed it through `run_canonical()`, which filters
  `job_names` against `CANONICAL_JOBS` — so the step ledgered `done` with
  `jobs_run=0` and wrote zero `triage_verdicts`. The runner now splits
  manifest-declared jobs by owning registry and routes signal-registry jobs
  through the narrow signal lane (`signal_job_names` →
  `run_post_canonical_pipeline(job_names=..., run_enrichment=False)`),
  without the full `include_signal` LLM fan-out. Unknown job names are
  logged and recorded in the ledger detail instead of silently no-opping.
  A `backfill-attention-triage-redo` manifest step heals nodes that already
  upgraded through 1.3.0–1.3.6 with the bogus green ledger row (measured
  ~0.7s per day of history; no LLM, no network; idempotent upserts). The
  upgrade-matrix CI job now asserts `triage_verdicts > 0` instead of
  carrying the step in `KNOWN_NO_OP_STEPS`.

## [1.3.6] — 2026-08-06

### Fixed

- `[O]` Never leave a SQLite transaction open after 0-row UPDATEs in legacy
  isolation mode (derivation retry, privacy layer, contact-name resolve, PII
  redaction, startup app_id migration). An open transaction was poisoning the
  writer so topic clustering `BEGIN IMMEDIATE` failed every batch and derived
  topics never landed.
- `[O]` Coalesce queued `inbox_deferred_enrichment` jobs for the same source
  into one derive batch (cap 100), so a post-downtime backlog pays per-batch
  work once.
- `[O]` Run `TopicClusterJob.enrich` off the event loop so control-plane
  keepalive stays responsive during k-means / label-pool work.

### Added

- `[O] [P]` Sanitization prewarm reports download/load progress (`phase`,
  `cached_bytes`, `first_run`, per-model status) on `/v1/upgrade/status` and
  `/v1/shell/status`, so a first install's ~2.9GB Hugging Face fetch is visible
  instead of looking hung.
- `[O] [P]` `system.computer_name` from macOS `ComputerName` (cached once per
  process) so fleet/UI can name a node after the machine the owner recognises.

## [1.3.5] — 2026-08-06

### Fixed

- `[D]` Pin `torch<2.13`. torch 2.13.0 — a fresh resolve inside the previous
  `>=2.8,<3` range — segfaults (`SIGSEGV` in `mps copy_cast_kernel`) when the
  embedder, privacy filter and NSFW classifier touch MPS concurrently, which is
  exactly what a node does while answering its first query. Every new install on
  Apple silicon crashed on its first chat message; nothing before that point
  failed, so boot and control-plane checks all passed first. Reproduced with two
  encode threads plus a pipeline load (exit 139 on 2.13.0, clean on 2.11.0) and
  guarded by the `a8b` check in the `topos-install-qa` skill.

## [1.3.4] — 2026-07-29

- `[S1] [O]` Coverage `spec_version` columns + catalog `JOB_SPEC_VERSIONS`
  (PLAN_NODE_RELEASE_MIGRATIONS M3); writers stamp; anti-join treats NULL as 0.
- `[O]` Upgrade step kinds `canonical_reprocess` / `derived_rebuild` /
  `reembed`; `consent: prompt` → `pending_consent` + `/v1/upgrade/consent`.
- `[O]` Upgrade-matrix CI fixture builder + catch-up runner (M4); seeded-DB
  release smoke; device_info upgrade summary fields for fleet (M5).

## [1.3.3] — 2026-07-29

### Added

- `[S1] [O]` Single migration registry (`MIGRATIONS`), `PRAGMA user_version`
  fast-path, fail-loud schema migrations, pre-migration backup under
  `~/.topos/backups/`, downgrade guard when `user_version` is ahead of this
  build (PLAN_NODE_RELEASE_MIGRATIONS M0–M2).
- `[O]` `scripts/cut_release.py`, `check_release_artifacts.py`,
  `sync_migration_checksums.py`, `RELEASING.md`.
- `[P] [O]` Attention triage: `signal_pin_intent` / `signal_retire_intent`
  control-plane handlers.

## [1.3.2] — 2026-07-29

### Added

- `[S1] [O]` Entity black hole owner controls + turn-level taint.
- `[S1] [O]` Complexity data-page engine (summary / timeline / topics / influence).
- `[O]` App-mode shell contract (attach-don't-double-start, file logs, tray over HTTP).
- `[O]` Unified timeline daily reads; scope-registry sync.

## [1.3.1] — 2026-07-29

### Fixed

- `[O]` Prioritize UI bootstrap over upgrade reprocess (patch anchor; no
  derived-layer invalidation).

## [1.3.0] — 2026-07-29

### Added

- `[S1] [E:attention_triage]` Attention triage (daily 2×2 verdicts, badges/ranks, dashboard).
- `[O]` Negotiable time-signal availability (flex / rhythm / commitments).
- `[S1]` Conversation context tags (work / personal).
- `[O]` Claim verification / fact verdicts; facts-LLM think gating.

## [1.2.7] — 2026-07-22

### Fixed

- `[O]` Temporal coherence and client-local now for relative dates.

## [1.2.0] — 2026-07-10

### Changed

- `[E:entities] [D]` NER wordpiece stitcher — re-extract entities + rebuild
  entity graph on upgrade (see manifests.json `1.2.0`).
- `[S1]` Temporal entity graph layers, provenance roles, Louvain neighborhoods.

## [1.1.0] — 2026-06-01

### Added

- `[S1] [D]` Dense intelligence upgrade (stats engine, entity spine, fact
  store, incremental clustering, query planner). Anchors the upgrade ladder.
