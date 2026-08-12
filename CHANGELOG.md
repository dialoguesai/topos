# Changelog

All notable changes to `topos-node` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); lane tags
follow `RELEASING.md` (`[S1]`, `[E:…]`, `[D]`, `[O]`, …).

The machine-readable twin of each release is
`topos/upgrades/manifests.json`.

## [Unreleased]

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
