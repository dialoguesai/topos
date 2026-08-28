# Contributing to Topos Node

Thanks for contributing to `topos-node`.

This repository is intentionally consumer-facing. Keep changes focused on local node runtime, user experience, and public-safe tests.

## Local setup

```bash
uv sync --extra dev --extra local
uvx pre-commit install
uvx pre-commit install --hook-type commit-msg
```

Both `install` lines are required. The first wires the file hooks; the second
wires the commit-message guard. A commit will refuse to run until both are
present, so you cannot end up half-guarded without noticing.

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
text, and skip cleanly where there is no local database, so CI and fresh clones
are unaffected. That last point is the limit worth knowing: **the guard only
runs where the data lives.** A contributor without a node is not protected by
it, which is why the rule above matters more than the tooling.

Person names are deliberately out of scope — every person entity would collide
with ordinary English ("Unknown", "Claude") and a noisy hook gets removed. Names
are on you.
