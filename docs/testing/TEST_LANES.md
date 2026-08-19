# Test lanes

**The default lane is hermetic. Everything that touches your real data is opt-in
by marker.**

```bash
pytest tests -q          # safe: temp databases only, no network to :9000
```

You cannot reach your own data by naming a file or a test id — only by naming a
marker. That is deliberate; see [Why the marker is the only key](#why-the-marker-is-the-only-key).

## The lanes

| Lane | Command | Touches |
| --- | --- | --- |
| Default | `pytest tests -q` | temp databases only |
| Public / CI | `just test` | temp databases only |
| Release gate | `just gate` | temp databases only |
| Owner-database eval | `just test-owner-db-eval` | a **snapshot** of `~/.topos/database.db` |
| Live node | `just test-live-node` | the node running on `:9000`, and whatever database it has open |
| End-to-end | `pytest tests -m e2e -q` | live Keycloak + Control Plane |

`pyproject.toml` sets `addopts = ["-m", "not live and not e2e and not qq_eval"]`,
so the three data-touching markers are deselected unless you ask for them.
`-m` is last-one-wins: any explicit `-m` on the command line replaces that
filter wholesale, which is how the opt-in lanes get in.

## Markers that reach real data

| Marker | Means | Redirectable with `TOPOS_DATABASE_PATH`? |
| --- | --- | --- |
| `qq_eval` | query quality/latency eval against the owner database | yes |
| `live` | needs a real database, and for `tests/release/iteration4` a node on `:9000` | yes, except the `:9000` module |
| `e2e` | needs live Keycloak + Control Plane | n/a |

`live` currently spans two different needs — in-process modules that resolve the
database themselves, and one module that drives the running node over HTTP.
`just test-owner-db-eval` therefore `--ignore=tests/release/iteration4`: an env
var set in the pytest process cannot redirect a database opened by a different
process.

## Running the owner-database eval without writing to your database

These cases are only meaningful against real data — a seeded fixture cannot tell
you whether retrieval finds *your* notes — but every turn they run persists a
`query_artifacts` row. `just test-owner-db-eval` takes a point-in-time online
backup first and points the lane at it, so the reads are real and the writes are
thrown away:

```bash
just test-owner-db-eval
```

Manually, the same thing:

```bash
snap=$(python scripts/snapshot_owner_db.py)
TOPOS_DATABASE_PATH="$snap" pytest tests -m qq_eval -q
rm -f "$snap" "$snap"-wal "$snap"-shm
```

The backup uses SQLite's online-backup API, not `cp`: the node is normally
running with WAL enabled, and a byte copy can miss committed pages still in the
`-wal`. A torn snapshot reads as missing records, which in an eval reads as a
retrieval regression.

## The two guards, and what each one can see

**`tests/conftest.py::_no_live_db_guard`** (autouse fixture) pins
`TOPOS_DATABASE_PATH` to a per-test tmp file. It protects exactly one resolution
path: code that asks *settings* where the database is.

It cannot see a module that resolves the path itself:

```python
LIVE_DB_PATH = Path(os.environ.get("TOPOS_DATABASE_PATH",
                                   Path.home() / ".topos" / "database.db"))
adapters = AdapterFactory.create("local_database", db_path=LIVE_DB_PATH)
```

That constant is evaluated at **collection** time, when the env var is still
unset, so it freezes to the real home path before any fixture runs — and the
explicit `db_path=` then walks straight past the guard into
`sqlite3.connect(str(db_path))`. Five modules do this.

**`tests/live_db_watch.py`** closes that hole from the other end. It wraps
`sqlite3.connect` for the whole session and records every connect that opens
owner data **read-write**, attributed to the test that caused it. `mode=ro` and
`immutable=1` URIs are not recorded — reading changes nothing on disk.

A read-write open by a test that did not opt in **fails the run**
(`pytest_sessionfinish` sets a non-zero exit status) and prints the test id, the
path, and the source line that opened it. Opt-in lanes are reported but not
failed.

### The third shape: import time

A module does not have to name `~/.topos` to reach it. This was at module scope
in `tests/features/test_p3_entity_spine.py`:

```python
MESSAGES_MANIFEST = resolve_scope_manifest("messages:read")
```

Resolving a manifest reads as a registry lookup, but `manifest_from_scope_entry`
asks `get_sources_by_scope` which installed sources back that scope, and that
reads `source_runtime_installs` through `core.state.get_db_connection()`. At
module scope it runs during **collection** — and collection has no fixtures, so
`_no_live_db_guard` has not pinned anything yet and the unset path resolves to
the developer's own database. Every plain `pytest tests/` opened it read-write
before the first test started.

It also hid whatever came after it: once `core.state.db_conn` is cached, later
calls reuse the handle and open nothing. One recorded open is not evidence of
one offender.

`test_no_test_module_reaches_the_database_at_import_time` now walks each test
module's AST for database-reaching calls at import scope — module body, class
bodies, and decorator arguments, since a `parametrize` argument is evaluated
during collection like any other module-scope expression.

### What the connect guard cannot see

It watches `sqlite3.connect` **in this process**. Two things are therefore out of
its reach, and both are handled by deselection instead:

- **Writes made by the node.** `tests/release/iteration4` drives the engine on
  `:9000` over HTTP; that process opens its own database, and no env var set in
  the pytest process redirects it. `just test-live-node` is the only way in.
- **Writes made by a subprocess.** Anything that shells out carries its own
  interpreter and its own unwrapped `sqlite3`.

There is also one blind spot worth naming rather than hiding: a symlink inside a
tmp directory pointing into `~/.topos` resolves to a watched path but does not
match the substring pre-filter that keeps the guard cheap. Nothing in this suite
does that.

`tests/test_owner_database_hermeticity.py` proves the detector is armed and
classifies correctly, and pins the selection rule. It cannot be the enforcement:
a test that asserts "nothing has written to `~/.topos`" passes happily while a
test scheduled *after* it does exactly that. Only a session hook sees the whole
run.

## Why the marker is the only key

Naming a path does not opt you in:

```bash
pytest tests/gap/qq/engine/test_en_qq_eval_queries.py -q   # collected, then deselected
```

The rule is "carry the marker or you are deselected", with no exception for
having typed the filename. An exception there would mean an agent, a script, or
a muscle-memory `pytest <path>` could still reach your database — which is how
this got missed for three weeks. To run it anyway, say so:

```bash
pytest tests/gap/qq/engine/test_en_qq_eval_queries.py -m qq_eval -q
```

## Adding a test that needs real data

1. Mark it `live` or `qq_eval`.
2. Resolve the path through `TOPOS_DATABASE_PATH` so the snapshot lane can
   redirect it — `Path(os.environ.get("TOPOS_DATABASE_PATH", <default>))`, which
   is what the existing modules do.
3. Read with `mode=ro` if you only read. The guard will not record it, and the
   next person can run your test against their own database without consequence.
4. Add it to the named list in `tests/test_owner_database_hermeticity.py` so
   dropping the marker later fails loudly.

## History

- **2026-07-27** — the public lane opened `~/.topos/database.db` on 16 occasions
  across 14 tests. Fixed by `_no_live_db_guard` plus `live`-marking the
  adversarial modules (`main` 3e61c47).
- **2026-08-19** — a plain `pytest tests/ -q` inserted 71 rows into the owner's
  `query_artifacts` and reported two environment-dependent 500s from the live
  node as suite failures. The markers from July were doing half a job: they
  waived the guard, but nothing deselected them, so the ordinary gate ran the
  whole set. Fixed by the `addopts` filter, the connect-level guard, and the
  snapshot lane above. The connect guard's first full run then found a SIXTH
  path nobody had listed — the import-time one above, which no marker and no
  fixture could have covered.
  At that point 97% of the rows in the owner's `query_artifacts` — and **100% of
  the rows carrying a `duration_ms`** — were harness sessions, so
  `scripts/query_latency_percentiles.py` was reporting the test suite's latency
  as the owner's.
