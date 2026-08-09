# Changelog

All notable changes to `topos-node` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); lane tags
follow `RELEASING.md` (`[S1]`, `[E:…]`, `[D]`, `[O]`, …).

The machine-readable twin of each release is
`topos/upgrades/manifests.json`.

## [Unreleased]

### Fixed

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
  failed attempt.

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
