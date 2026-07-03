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
topos-node --host 0.0.0.0 --port 9000
```

### 4) Verify

```bash
curl http://localhost:9000/health
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
pytest tests -m "public and not e2e" -q
```

## Contributing

See `CONTRIBUTING.md` for:

- public vs private test lanes
- where deployment scripts now live
- contribution scope and security expectations

## License

Apache License 2.0. See `LICENSE`.
