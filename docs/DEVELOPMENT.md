# Developing Topos Node locally

## Setup

```bash
uv sync --extra engine
just run
```

For the full contributor setup — including the three required pre-commit hook
stages — see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Tests

```bash
pip install -e ".[dev,engine]"
pytest tests -q
```

The default lane is hermetic — temp databases only. Tests that read your own
`~/.topos` database or drive a running node are deselected unless you ask for
them by marker; see [testing/TEST_LANES.md](testing/TEST_LANES.md).

Naming a file or test id does **not** opt you in; only `-m` does.

## Engine memory

ML models are cached inside the Engine with LRU eviction. For lighter local runs
(especially inside an IDE's integrated terminal), see
[Engine memory management](../topos/docs/ml-manager-v2/MEMORY_MANAGEMENT.md).

```bash
# Default: ENGINE_MAX_RESIDENT_MODELS=3; pipeline flush is automatic
# Override only if needed, e.g. lower RAM: export ENGINE_MAX_RESIDENT_MODELS=2
export PRIVACY_FILTER_DEVICE=cpu
```

## Shared runtime contracts

The `shared/` package in this repo is part of the node runtime contract. It
contains common schema and filtering definitions used by both API and engine
paths — change it with both consumers in mind.

## Security and configuration

- Do not commit `topos/.env` or any real credentials.
- Keep your `TOPOS_KEY` private.
- Review [`env.example`](../env.example) for available configuration options.
