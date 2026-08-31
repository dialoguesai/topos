"""Stable defaults for a local Topos node.

8676 is TOPO on a phone keypad. Keep this the only Python source of the
listen port so CLI, probes, and the local MCP proxy cannot drift.
"""

DEFAULT_NODE_PORT = 8676
DEFAULT_NODE_HTTP_URL = f"http://127.0.0.1:{DEFAULT_NODE_PORT}"
DEFAULT_NODE_LOCALHOST_URL = f"http://localhost:{DEFAULT_NODE_PORT}"
