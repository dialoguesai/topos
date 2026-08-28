# Contributing to Topos Node

Thanks for contributing to `topos-node`.

This repository is intentionally consumer-facing. Keep changes focused on local node runtime, user experience, and public-safe tests.

## Local setup

```bash
uv sync --extra dev --extra local
uvx pre-commit install
uvx pre-commit install --hook-type commit-msg
uvx pre-commit install --hook-type pre-push
```

All three `install` lines are required: file hooks, the commit-message guard,
and a last check before anything leaves the machine. A commit refuses to run
until all three are present, so you cannot end up half-guarded without noticing.

`git commit --no-verify` skips the commit hooks, and nothing inside a hook can
prevent that — it is a git built-in, and GitHub allows no custom pre-receive
hook outside Enterprise. The pre-push stage exists for that case: skipping
commit hooks is often reflexive, while pushing is deliberate, so a `--no-verify`
commit is caught while it is still local and fixable without rewriting history.
`git push --no-verify` still bypasses it — but that is two deliberate refusals
rather than one habit.

## Test lanes

Full reference: [docs/testing/TEST_LANES.md](docs/testing/TEST_LANES.md).

- Default lane — hermetic, temp databases only, nothing on the network:

```bash
pytest tests -q
```

- Public lane (OSS checks; `just test`) adds the `public` marker:

```bash
pytest tests -m "public and not e2e and not live and not qq_eval" -q
```

- Internal/private lane (architecture-heavy, hosted flows, migration/sprint suites):
  - Marked with `@pytest.mark.private`
  - Not part of default public checks

- Lanes that reach real data are **opt-in by marker**, and the default filter in
  `pyproject.toml` deselects all of them:
  - `@pytest.mark.qq_eval` — query quality/latency eval against the owner
    database. Run it against a disposable copy with `just test-owner-db-eval`,
    never against `~/.topos` directly.
  - `@pytest.mark.live` — needs a real database, and for
    `tests/release/iteration4` a node running on `:9000` (`just test-live-node`).
  - `@pytest.mark.e2e` — requires external services and explicit environment setup.

  Naming a file or test id does **not** opt you in; only `-m` does. A test that
  opens a real `~/.topos` database read-write without a marker fails the run —
  see `tests/live_db_watch.py`.

## Deployment assets ownership

- `topos` (this repo) stays consumer-facing and package-focused.
- Cloud Run deployment/build assets for Topos runtime live in:
  - `topos-control-plane/scripts/gcp/topos-node/`
- Operational maintenance scripts that expose internal deployment details live in:
  - `topos-control-plane/scripts/topos-node-maintenance/`

## Optional extensions

Plugin authoring uses the **`topos.extensions`** entry-point contract documented in
[README.md — Plugins](README.md#plugins). Use
[topos-plugin-template](https://github.com/dialoguesai/topos-plugin-template) as a
starting point.

Keep proprietary or hosted-only plugins in separate packages — not in this repo.

## Scope guidance

Good fit for this repo:

- CLI and node runtime behavior (`topos-node`)
- local database/engine behavior
- user-facing docs and onboarding
- public-safe tests and fixtures

Keep out of this repo:

- internal cloud rollout scripts
- environment-specific production operations
- private-only diagnostics and runbooks

## Security expectations

- Never commit secrets, keys, or real credentials.
- Use placeholders in examples and fixtures.

### Never commit the owner's own data — in code OR in a commit message

This repo is public, and a node's database holds one real person's life. The
product's privacy machinery operates on that database and never reads source or
git history, so nothing downstream will catch a name that reaches a docstring, a
fixture, or a commit message.

**The rule is: describe the shape, not the value.** Write "a home address", not
the address. "Two real goals", not the goals. This applies to prose you write
about a measurement as much as to test data — a docstring citing what you
measured is the most common way this happens, because it does not feel like
handling data at all.

**Check a draft before you write it**, which is cheaper than being blocked:

```bash
uv run python scripts/scan_repo_for_owner_data.py --text "your draft message"
uv run python scripts/scan_repo_for_owner_data.py --all   # whole tree
```

Two hooks enforce it at commit time — one on staged files, one on the message.
They compare against the local node's black-holed entities, places and goal
text, and skip cleanly where there is no local database.

That skip is the design, not a gap: **the guard runs exactly where the risk is.**
The only machine that can leak a node's data is the machine holding it, and that
is the machine the hook runs on. A contributor without a node has nothing of the
owner's to leak, and CI could only check by being handed the very data it would
be checking for. Each person's hook covers each person's own node.

Person names are covered as FULL names only — a first name and a surname
together. Single first names are most of a contact book and collide with
ordinary English ("Unknown", "May", "Porter"); a hook that fires on those gets
removed, and a removed hook protects nobody.

### Names the database does not know

The scans read your local node, so they only cover people it has seen. A
landlord, a doctor, a child's school, a relative you have never messaged from
this machine — none of those are in any table, and they are exactly what someone
pastes into a docstring while debugging.

Put those in an on-device file, one term per line, `#` for comments:

```
~/.topos/private-terms.txt
```

It lives **outside the repository on purpose**, and the scanner refuses a path
inside the working tree. A list of the names you are hiding is the worst thing
to commit by accident, and `.gitignore` is one `git add -f` away from failing.
Terms you write by hand bypass the length floor — a short name you chose
deliberately is not the hook's business to second-guess.

Override the location with `TOPOS_PRIVATE_TERMS` if you keep it elsewhere. The
scanner's summary line says whether a local list was loaded, so an inactive
guard is visible rather than silent.
