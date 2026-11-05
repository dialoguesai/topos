# Pre-tag load gate (prototype)

Status: **advisory prototype, 5 Nov 2026.** No release check reads it. `select_release_checks.py`,
`node_release_gate.py` and `publish.yml` are unchanged. Whether to require it, and for which changes, is the
owner's decision (see "Decisions for the owner").

**Result.** On the 1.5.43 tree, before topos#263, the gate failed 7 of 7 runs; with the watchdog on, each failed in
under 2 minutes. On topos#263's tree, no run showed the #263 class, and 4 of 5 runs answered every query. The
fifth showed a different stall that has not been diagnosed (see "Open lead").

## Why

Every node failure from late October to early November was a load, scale or concurrency failure. Only the
focused-45 stress run caught them, and that run happens after the release is installed on the owner's live node:

| Release | What failed under load | Fixed in |
|---|---|---|
| 1.5.42 | A failed query-artifact INSERT held SQLite's write lock for about 11.5 minutes | topos#261 (rollback), topos#263 (cause) |
| 1.5.43 | Two session writes failed with `DatabaseError: another row available` (rolled back by #261) | topos#263 |
| 1.5.41 | A 303 s sweep stall: `claim_next_job` lock inversion behind an ungated DELETE | topos#256 |
| 1.5.38 | Three Ollama models thrashing, then a 43-minute Ollama stall | config (all-on-27B) |
| 27 Oct | `get_device_info` dbstat scans piling up with no single-flight, writes parked in the inbox | node fix |

The 1.5.42 and 1.5.43 failures share one cause. Since 8 Sep 2026, query work that `topos/query/pipeline.py`
moved to a pool thread used the event loop's own `sqlite3.Connection` (`_query_work_stays_on_loop()` starts with
`asyncio.get_running_loop()`, which a worker thread does not have, so from a worker it always answered "stay").
A worker wrote through the loop's handle while the loop read on it. topos#263 gave each worker its own connection
and fixed 24 other `to_thread` sites that handed the loop's handle to a worker.

Rig D cannot see this class. It checks sharing privacy on three tiny synthetic owners (about 1.9 MB of packs)
with models stubbed and requests paced for its timing cells. In the 1.5.43 rig, the rollback path from #261
logged nothing on any rig node.

## What the gate runs

`scripts/node_load_gate.py` (`just load-gate`), on the release candidate's clean checkout, before the tag:

1. **Refuse to share the Mac.** It stops if Rig D (`run_any_to_any_release.py`, `scripts.a2a_rig.run`), a stress
   run (`stress_30.py`) or another load gate is running, if less than 10 GiB is free, or if the Mac is on battery
   (`--allow-battery` overrides). It reads the live database's `PRAGMA user_version` read-only, to compare after
   every run, and records the host's state (power, swap, load average, whether the owner's node, Dia, resident
   local models and OrbStack are running) before and after each run.
2. **Check the interpreter.** The candidate's `.venv` (Python 3.12, which `RELEASING.md` already asks for) must
   import `topos` from the candidate tree with the owner-database guard armed.
3. **Seed a large invented database with the candidate's own schema** (`scripts/load_gate/seed.py`). It boots
   the candidate once on an empty scratch database. That start-up runs the migrations and every table manager.
   It then installs seven of the candidate's built-in source definitions through its own `/v1/source-install`
   door and stops the node. The owner socket refuses that door to anything but the signed app, so this one boot
   runs with `TOPOS_OWNER_DOOR_SPLIT=0`; the load runs keep the default. The message tables a connector makes on
   its first sync come from the candidate's own managers (`ensure_all_tables`, `CanonicalTablesManager`). Then it
   bulk-inserts invented rows: about 0.43 M rows (304 MB) at `--scale 1`, built in under a minute. That is 200 k
   messages, 40 k topics, 40 k browser visits, 30 k assistant-chat messages, 30 k query artifacts, 20 k
   transcript segments and 20 k graph edges, plus goals, facts, entities, contacts, calendar events, journal
   entries and 15 k query sessions (half expired). People are syllable pairs; places and projects come from short
   fixed lists. Each insert keeps only the columns the candidate's table has. The seed is cached per (`topos/` tree
   id, generator version, scale, seed), so repeats on the same candidate reuse it.
4. **Start a guarded scratch node** (`scripts/load_gate/node.py`) with the candidate's `python -m topos.cli`:
   - **Scratch home.** HOME is a scratch folder, and every `~/.topos` default is pinned inside it (the same
     overrides as Rig D's node plan). It gets a dummy `TOPOS_KEY`, a dead control-plane port and a non-local model
     URL. Background work is off.
   - **Owner-database guard.** A generated `sitecustomize` arms `tests/live_db_watch` against the *real* home
     before HOME moves. Any read-write open of the owner's database, `.env` or socket is refused and journalled.
     The journal is written every half second as well as at exit, because the node's forced exit (`os._exit`
     after `TOPOS_SHUTDOWN_EXIT_DEADLINE_SECONDS`) skips `atexit`, and an unwritten journal would read as clean.
   - **Stack dump.** The same file registers `faulthandler` on SIGUSR1. It needs no GIL, so it can dump every
     thread's Python stack from a node that is deadlocked.
   - **Egress guard.** An audit hook refuses every outbound connect except the scratch sockets and two loopback
     ports. It also refuses launching `open`, `ollama` or `osascript`.
   - **Database check.** The node's first `Serving database` line, and the `database_path` in `/healthcheck`,
     must name the scratch file, or the node is killed.
   - **Transaction watchdog on** (`TOPOS_DB_TXN_WATCHDOG=1`, `--txn-watchdog off` to turn it off). It installs a
     SQLite trace callback on every connection, and that changes how this bug class shows itself (see "Prototype
     result"): a Python callback runs inside `sqlite3_step` while the connection's mutex is held, so two threads
     on one connection deadlock (one holds the GIL and waits for the mutex, the other holds the mutex and waits
     for the GIL) instead of corrupting each other's statements. With it off, the same bug shows as the owner's
     node saw it: rollbacks and `another row available`.
5. **Drive it** (`scripts/load_gate/drive.py`), all as the owner, with no control plane:
   - **Owner queries.** 12 workers send `query` in `summary` mode through `/api/local/agent/dispatch`, across 12
     scopes. In this mode the session purge, retrieval, the session `put` and the artifact INSERT each hop to a
     worker, while the loop reads the session and the installed sources. No model is called.
   - **Analytics reads.** 2 workers rotate through `complexity_summary`, `complexity_topics_daily`,
     `graph_cypher_query` and `tools_index_status`. These are the CPU-bound reads that #263's audit moved off the
     loop's handle.
   - **Health probe.** `GET /healthcheck` over loopback every 0.5 s. A probe fails on no answer within 15 s or
     on `db_ok: false`. `db_ok: null` is the node's own "unknown" (its `SELECT 1` did not get a pool thread within
     its timeout, `core/db_health.py`); it is counted (`health_db_unknown`), not failed.
   - **Timing.** The load runs 180 s. Requests still in flight get up to 120 s to finish. Then 10 s idle and
     5 recovery probes (a query and a health check each).
   - **Hang detection.** If no health probe has succeeded for 60 s, the driver stops the load and skips recovery.
     The gate then dumps the node's stacks and records each thread's innermost `topos` frame.
6. **Judge it** (`scripts/load_gate/verdict.py`) from the node's own log, the driver's records and the guard
   files. The result is written to `load-gate.json`.

## How long it takes

Measured on this Mac on 5 Nov 2026 (see "Prototype result"):

| Step | Time |
|---|---|
| Cold seed (first run on a candidate's `topos/` tree): boot, 7 installs, message tables, 0.43 M rows, `ANALYZE` | 54–59 s |
| One run that completes: boot 2–3 s, 180 s of load, drain, 5 recovery probes, stop | 204–210 s |
| One run that hangs: 60 s to call it hung, stack dump, forced stop | 113–114 s |
| The gate, one run, cached seed | about 3.5 min |
| The gate, three runs, cold seed | about 11.5 min |

Rig D takes about 75 minutes.

## Gates

The zero-tolerance gates are the class this gate exists for. The latency limits are provisional. On the fixed
tree's completed runs the query p95 was 10.1–10.8 s and the slowest health probe 1.14 s, on a laptop with load
averages of 2.6–8.0 (the owner's node, Dia and OrbStack running), so each limit has about 5–9 times headroom.

| Gate | Pass | Source |
|---|---|---|
| `isolation` | Served the scratch file; live `user_version` unchanged; guard journal empty | new (safety) |
| `node_hung` | 0: some health probe succeeded in every 60 s window | new; the bad tree's symptom with the watchdog on |
| `write_gate_rollbacks` | 0 `[WRITE_GATE] write at … raised …; rolled back` | new; the 1.5.43 symptom |
| `write_gate_rollback_failures` | 0 | new |
| `sqlite_error_lines` | 0 log lines with `another row available`, `Recursive use of cursors`, `cannot start a transaction within a transaction`, `database is locked` and the like | new |
| `request_sqlite_errors` | 0 responses carrying one of those | new |
| `stuck_transactions` | 0 watchdog lines (`TOPOS_DB_TXN_WATCHDOG=1`, 5 s) | new |
| `write_gate_longest_hold_s` | ≤ 30 s | new; the 1.5.42 symptom (11.5 min) |
| `write_gate_longest_wait_s` | ≤ 60 s | new; the 1.5.41 sweep stall |
| `query_functional_rate` | ≥ 0.98 | adapts `verify_functional_rate` / `homechat_functional_rate` |
| `query_p95_s` | ≤ 60 s | adapts `verify_p95_s` |
| `health_failures`, `health_longest_s` | 0, ≤ 10 s (`db_ok: null` counted apart, not failed) | new; a loop stall (the Sep 8 tray-red class) |
| `transport_errors` | 0 | keeps `moments_transport_errors` |
| `recovery_probes` | 5 of 5 | keeps `recovery_probes_functional` |

**Not kept from the focused-45 report, and why:**

- `models_only_allowed`, `ollama_longest_request_s`, the verify latency gates and `quality_checks_passed` need
  a real model host.
- `sharing_*`, `homechat_*`, `routines_functional_rate` and the Moments gates go through the control plane and its
  grants.
- `background_keeps_up` needs 24 hours and background work, which this version turns off.

## Recording the verdict

- **Evidence.** `load-gate.json` sits beside the run. It has `schema`, `pass`, `failures`, `tree`, `head`,
  `clean`, the seed manifest, the profile and per-run gates and metrics. It is the same shape of claim as Rig D's
  `node-release-gate.json`: a verdict for one exact clean tree.
- **Status.** `--record` posts the status context `release/load-gate` on the candidate's HEAD: `pending` at
  start, then `success` ("Load gate passed; tree=<tree>") or `failure`. It is a separate context, next to
  `release/sharing-rig`, and nothing reads it yet.
- **If the owner makes it required,** mirror `node_release_gate.py`:
  - `--evidence <load-gate.json>` records the same tree on the merged commit after the squash merge, so the
    gate does not run again.
  - `publish.yml --verify` accepts only the newest `release/load-gate` status for the exact tagged tree.
  - The owner waiver works as it does for Rig D.

  None of this is in the prototype.

## When the selector would require it (proposal)

The prototype changes no selection. The proposal is to require the gate whenever `select_release_checks.py`
already sets `sharing_rig`: any change under `topos/` or `shared/`, or to `pyproject.toml` or `uv.lock`, that is not
on the allowlist. In practice that is every runtime release.

A narrower trigger (only `topos/storage`, `topos/query`, `topos/core`) was considered and rejected. The #263 class
was spread across `topos/api`, `topos/core/handlers`, `topos/enrichment` and `topos/query`, and #256 came from a
topic-clustering prune. A path list would have missed some of them.

The gate takes 3.5 to 11.5 minutes, against Rig D's 75. So it would run first on the same candidate, and a
load failure would stop the cut before the rig is spent. On the 1.5.43 candidate it would have failed in under
2 minutes, before the tag, instead of in the post-install stress run on the owner's node.

## What it does not cover

- **Real models.** It runs no Ollama: no thrash, no evictions, no generation latency. That needs a separate lane
  with a private model host (Rig D's `A2A_RIG_REAL_ANSWER_HOST` on 11435 is the pattern), never the owner's 11434.
- **The control plane.** Home chat, routines, sharing grants and Moments go through it.
- **Background work.** The pipeline worker (`claim_next_job`), the graph rebuild subprocess, the share-index
  rebuilds and the sweeps are off in this version, so the #256 class and the O(n²) goal-pair rebuild are not
  exercised. A phase 2 would turn the worker and graph refresh on with stub models.
- **Data shape.** The rows are invented and uniform; the owner's database is about 2.85 GB with real skew.
  `--scale` raises the volume, not the skew.
- **Timing on a busy laptop.** Latency gates move with host load (Spotlight, other projects' containers, the
  owner's node). The zero-tolerance gates do not depend on timing. The host block in the evidence says what was
  running.
- **The model-backed query paths.** Queries run in `summary` mode with `ENABLE_LLM=false`; the paths that wait on
  a model (home chat, `verify_claim`, routines) hold connections and threads differently.

## Prototype result

Runs on 5 Nov 2026, 21:24–22:15Z, on this Mac on AC power, one at a time, after the 1.5.44 stress run. One
seed per tree (seed 7, scale 1: 433,189 rows, 304 MB). 12 query workers, 2 analytics workers, 180 s of load.
"Bad" is `521e6ff0` (release 1.5.43, before topos#263); "fixed" is `8acae997` (topos#263's head).

| Tree | Watchdog | Runs | Failed | What the gate saw |
|---|---|---|---|---|
| bad | on | 5 | 5 | Hung within the first second of load every time: 0 of the first 12 queries answered, no health probe for 60 s |
| bad | off | 2 | 2 | 58 and 46 write-gate rollbacks; 8 of 296 and 11 of 294 queries answered |
| fixed | on | 4 | 1 | 3 runs answered every query (357, 359, 356; p95 10.1–10.6 s; see note); 1 run failed differently (see "Open lead") |
| fixed | off | 1 | 0 | 367 of 367 answered, p95 10.8 s |

The bad tree failed 7 of 7 runs. On the fixed tree, no run hung, logged a write-gate rollback with
`another row available`, or returned that error. 4 of its 5 runs answered every query.

Note: the three fixed-tree runs with the watchdog on were scored with an earlier rule that failed a probe on
`db_ok: null`. They failed `health_failures` on 1, 2 and 1 probes. The node logged exactly that many "database
probe exceeded … reporting unknown, not unhealthy" lines, so all of them were the node's own "unknown". The gate
now counts those apart, and under that rule the three runs pass.

**The bad tree with the watchdog off is the 1.5.43 symptom.** The rollbacks were at `stores.py:1437 in
purge_expired` (27 and 23), `stores.py:1330 in put` (23 and 15) and `stores.py:1372 in append_artifact` (8 and 8).
All but 2 were `DatabaseError` or `OperationalError: another row available`; the 2 were `no more rows available`.
The second site is the exact line from the 1.5.43 stress run. The failed queries returned `another row available`
(197 and 209) or `database is locked` (90 and 72).

**The bad tree with the watchdog on deadlocks.** The stack dumps (3 runs) show a pool worker in `purge_expired`
(`pipeline.py:372`, then `stores.py` `_write` and its commit) using the event loop's connection. On the same
connection, the event loop was reading either the session (`pipeline.py:378`, `stores.py:1255 in get`) or the
installed sources (`resolve_scope_manifest`, `sources/registry.py:869`). The native stacks (macOS `sample`) show
the cycle. The worker is in `sqlite3_step`, then the watchdog's `trace_callback`, waiting for the GIL. The loop
holds the GIL in `pysqlite_cursor_iternext` while it waits for the connection's mutex.

**Open lead on the fixed tree (not diagnosed).** One fixed-tree run (watchdog on, 22:05–22:10Z, host load
average 6.0) failed in a different way:

- **What happened.** About 11 s into the load, after the first 12 queries had answered, something took SQLite's
  write lock outside the write gate and held it until the run ended, about 280 s later. A session `put` on its
  own worker connection waited 31 s and was rolled back with `database is locked`. Four `signal_object_store.py:83
  in upsert_object` writes then each held the gate for 62 s while they waited for SQLite. Every later query timed
  out at 180 s, so 10 of 23 queries answered.
- **What it is not.** The watchdog logged no transaction left open. So the holder is either a connection the
  watchdog does not track or one it believes the gate covers. It is not the #263 class: there was no `another row
  available` and no hang.
- **What else is known.** The next run did not reproduce it in its first 146 s. That run dumped stacks every
  20 s to catch the holder, and the dumps crashed the node (SIGSEGV). `faulthandler`'s all-threads dump is safe on
  a deadlocked node but not on a busy one, so the gate now dumps only on a hang. 1.5.44's post-install stress run
  saw a 22 s write-gate hold, which may be the same thing.

**Isolation held on every run.** Every node, including the seed boots, served its scratch file. The live
`user_version` was 90 before and after every run. The owner-database guard journal was empty, and no outbound
connection or app launch was attempted.

**Host state.** Each run's `host` block records it. The numbers above came from load averages of 2.6–8.0 and
4.1–11.7 GB of swap, with the owner's node, Dia and OrbStack running and 0 or 1 local models resident.

The evidence (`load-gate.json` per series, stack dumps, the stalled run's node log) is in this Mac's shared
session-notes folder under `load-gate-evidence-1105/`. It is not committed: the files are large and are only
useful on this Mac.

## Decisions for the owner

1. **Required or advisory?** If required, use the proposed trigger above or a narrower one.
2. **Thresholds.** The latency limits are provisional, and the zero-tolerance gates are proposed as they are.
3. **Repeats.** One run caught the #263 class every time (7 of 7). The open lead appeared in 1 of 5 fixed-tree
   runs, so three runs (about 11 minutes) would be cheap insurance against rarer stalls.
4. **Watchdog on or off.**
   - **On (the default).** One connection on two threads becomes a hang within a second, and the gate fails in
     under 2 minutes with stack dumps that name both threads.
   - **Off.** The gate sees what the owner's node sees (rollbacks, `another row available`) in about 3.5 minutes.
   - **Option.** Default on, with an off run when a hang needs reading as the production symptom.
5. **The open lead.** A write lock held outside the gate for minutes on the fixed tree, now installed as 1.5.44.
   It needs its own investigation: rerun the fixed tree a few times and capture the holder with a one-shot dump
   or `sample` when a write waits more than 30 s.
6. **Where it runs.** This Mac before the tag (one heavy run at a time), or a dedicated machine.
7. **Phase 2 scope.** Background work with stub models; a private real-model host.
