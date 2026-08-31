# Topos Node

<p align="center">
  <img src="https://dialogues.ai/static/images/topos_logo.png" alt="Topos Logo" width="170" />
</p>

Topos Node is your personal AI node that runs on your own device.
It helps you process and organize your data locally, then connect to Topos and 3rd-party services with explicit user-controlled access.

- Product: [topos.dialogues.ai](https://topos.dialogues.ai)
- Company: [dialogues.ai](https://dialogues.ai)
- Topos Apps/Sources: [sheaf.dialogues.ai](https://sheaf.dialogues.ai)

## Why Topos Node

- Runs locally on your machine
- Keeps your node data under your control
- Supports source ingestion, local processing, and controlled sharing flows
- Installs as a single command-line tool via `uv`

## How Topos Works (in product terms)

Topos Node has two core parts that work together to give you control:

```text
Your Apps/Data -> Topos Database -> Topos Engine -> Safe Responses
```

| Part | What it is | What it does for you |
| --- | --- | --- |
| 🗂️ **Topos Database** | Your private memory layer on your device | Stores your records, keeps your history, and makes your personal context searchable |
| 🧠 **Topos Engine** | Your decision and processing layer | Understands requests, runs AI workflows, and returns the right answer based on your permissions |

### 🛡️ Cognitive Firewall (user control first)

The **Topos Engine** acts as a Cognitive Firewall: it helps ensure only the right information is used and shared.

| Cognitive Firewall principle | What that means in practice |
| --- | --- |
| 🔒 Permission-aware | Requests are evaluated against your access rules before data is returned |
| 🎯 Precision over data dumps | The engine is designed to return only what is needed, not your entire history |
| 👁️ Transparent behavior | Boundaries and limits are explicit so sharing stays understandable and controllable |

### 🤖 Core ML tools in the Engine

Topos Engine uses both local and model-hub paths so users can choose flexibility and control:

| ML tool | Role in the workflow | User-facing benefit |
| --- | --- | --- |
| 🦙 **Ollama** | Local model execution path | Keep more processing on-device and reduce external dependency |
| 🤗 **Hugging Face** | Model and backend integration path | Access broad model capabilities for enrichment and analysis tasks |

### Why this split matters

- 🏠 Your data lives in one durable place (Database)
- ⚙️ Intelligence and policy decisions happen in a separate runtime (Engine)
- 🛡️ The Cognitive Firewall model helps protect context while still enabling useful AI actions
- 🔄 You can evolve processing/model strategy without changing your core stored memory

### Shared runtime contracts

The `shared/` package in this repo is part of the node runtime contract. It contains common schema and filtering definitions used by both API and engine paths.

### Architecture notes (where truth lives)

This README is orientation, not the architecture ledger. Prefer:

- **As-built / why:** sibling `topos-research-wiki` → `20_ARCHITECTURE/` (`As_Built_Node`, firewall, query, MCP, graph, …)
- **As-is invariants / checks:** sibling `topos-ops-wiki` → `25_SYSTEMS/` (`SYS-node`, `SYS-cognitive-firewall`, …)
- **Target design:** `topos-data-wiki` (`status: target`) — do not implement from it without checking as-built

## Quick Start

### 1) Install

```bash
uv tool install topos-node
```

### 2) Configure

Topos Node requires a `TOPOS_KEY`.

```bash
topos-node --set-topos-key "<YOUR_TOPOS_KEY>"
```

This stores your key in:

- `~/.topos/.env`

Optional:

- `TOPOS_CONTROL_PLANE_URL` if you need a non-default endpoint

### 3) Run

```bash
topos-node --host 0.0.0.0 --port 8676
```

### 4) Verify

```bash
curl http://localhost:8676/health
```

## Common Commands

```bash
# Start node
topos-node

# Save your TOPOS_KEY for future runs
topos-node --set-topos-key "<YOUR_TOPOS_KEY>"

# Discover available local databases and exit
topos-node --discover

# Use custom database path
topos-node --db-path /path/to/topos.sqlite

# Bind custom host and port
topos-node --host 127.0.0.1 --port 9100
```

## Upgrade or Uninstall

```bash
# Upgrade to latest
uv tool upgrade topos-node

# Remove
uv tool uninstall topos-node
```

## Security and Privacy Notes

- Do not commit `topos/.env` or any real credentials.
- Keep your `TOPOS_KEY` private.
- Review `env.example` for available configuration options.

## Developing Locally

```bash
uv sync --extra engine
just run
```

### Engine memory (local dev)

ML models are cached inside the Engine with LRU eviction. For lighter local runs (especially inside Cursor's integrated terminal), see [Engine memory management](topos/docs/ml-manager-v2/MEMORY_MANAGEMENT.md).

```bash
# Default: ENGINE_MAX_RESIDENT_MODELS=3; pipeline flush is automatic
# Override only if needed, e.g. lower RAM: export ENGINE_MAX_RESIDENT_MODELS=2
export PRIVACY_FILTER_DEVICE=cpu
```

Run tests:

```bash
pip install -e ".[dev,engine]"
pytest tests -q
```

The default lane is hermetic — temp databases only. Tests that read your own
`~/.topos` database or drive a running node are deselected unless you ask for
them by marker; see [docs/testing/TEST_LANES.md](docs/testing/TEST_LANES.md).

## Plugins

Topos Node supports **optional plugins**: separate Python packages installed alongside
`topos-node` that register handlers, connectors, or other runtime hooks. The core node
does not hardcode plugin names — discovery is entirely via setuptools entry points.

### Contract: `topos.extensions`

| Rule | Detail |
| --- | --- |
| **Entry-point group** | `topos.extensions` |
| **Entry-point target** | A callable, e.g. `my_plugin:register` |
| **When it runs** | At process startup, before the server accepts traffic (`topos/extensions.py`) |
| **Failure mode** | A broken plugin is logged and skipped; the node keeps running |
| **Dependencies** | Your plugin declares `topos-node` (or `topos-node[local]`) in its own `pyproject.toml` |

**Minimal plugin**

`pyproject.toml`:

```toml
[project]
name = "my-topos-plugin"
dependencies = ["topos-node[local]"]

[project.entry-points."topos.extensions"]
my_plugin = "my_topos_plugin:register"
```

`my_topos_plugin/__init__.py`:

```python
def register() -> None:
    from my_topos_plugin.handlers import example  # noqa: F401 — registers @handles
```

`my_topos_plugin/handlers/example.py`:

```python
from typing import Any, Dict, Optional
from topos.core.handlers.registry import handles

@handles("my_message_type")
async def handle_my_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return {"status": "ok", "payload": {"received": message.get("type")}}
```

**Install and run**

```bash
pip install topos-node my-topos-plugin
topos-node
```

Handlers registered in `register()` are available to the control-plane WebSocket
and local API paths that dispatch through `topos.core.handlers`.

### Starter template

Fork **[dialoguesai/topos-plugin-template](https://github.com/dialoguesai/topos-plugin-template)**
for a working package with tests and CI. It registers a sample `plugin_template_ping`
handler you can copy and rename.

### Guidelines

- Use **unique message type names** (prefix with your project) to avoid colliding with core handlers.
- Keep plugins in **separate repositories** — do not add proprietary logic to this repo.
- Message types your plugin handles do not need to appear in the public engine protocol
  snapshot unless the hosted control plane will send them to all nodes.

## Contributing

See `CONTRIBUTING.md` for:

- public vs private test lanes
- where deployment scripts now live
- contribution scope and security expectations

## License

Apache License 2.0. See `LICENSE`.
