# Plugins

Topos Node supports **optional plugins**: separate Python packages installed alongside
`topos-node` that register handlers, connectors, or other runtime hooks. The core node
does not hardcode plugin names — discovery is entirely via setuptools entry points.

## Contract: `topos.extensions`

| Rule | Detail |
| --- | --- |
| **Entry-point group** | `topos.extensions` |
| **Entry-point target** | A callable, e.g. `my_plugin:register` |
| **When it runs** | At process startup, before the server accepts traffic (`topos/extensions.py`) |
| **Failure mode** | A broken plugin is logged and skipped; the node keeps running |
| **Dependencies** | Your plugin declares `topos-node` (or `topos-node[local]`) in its own `pyproject.toml` |

## Minimal plugin

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

## Install and run

```bash
pip install topos-node my-topos-plugin
topos-node
```

Handlers registered in `register()` are available to the control-plane WebSocket
and local API paths that dispatch through `topos.core.handlers`.

## Guidelines

- Use **unique message type names** (prefix with your project) to avoid colliding with core handlers.
- Keep plugins in **separate repositories** — do not add proprietary logic to this repo.
- Message types your plugin handles do not need to appear in the public engine protocol
  snapshot unless the hosted control plane will send them to all nodes.
