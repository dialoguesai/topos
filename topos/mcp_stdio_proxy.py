"""
Stdio MCP proxy: Claude Desktop → local Topos engine (no Control Plane).

Run this so Claude talks MCP over stdio to the proxy; the proxy forwards tool calls
to the engine's /api/local/* HTTP endpoints. Use when the engine and Claude run on
the same machine.

Usage:
  ENGINE_URL=http://localhost:8676 BEARER_TOKEN=your_key python -m topos.mcp_stdio_proxy
  # or with args:
  python -m topos.mcp_stdio_proxy --url http://localhost:8676

Claude Desktop config (direct to local engine): use scripts/run_local_mcp_proxy.sh
with full path, args ["--url", "http://localhost:8676"], and env BEARER_TOKEN.

Only list_database_tables and get_table_schema are exposed (engine's /api/local/*).
For full tools (get_analytics, get_messages, get_oplog) use the Control Plane.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

from topos.defaults import DEFAULT_NODE_LOCALHOST_URL

# Engine URL and token; set in main() before FastMCP runs.
_engine_url: str = ""
_bearer_token: str = ""


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer_token}", "Content-Type": "application/json"}


async def _call_engine(path: str, json_body: dict | None = None) -> dict:
    url = f"{_engine_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, headers=_headers(), json=json_body or {})
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise ValueError(data.get("error", "engine error"))
    return data


def main() -> int:
    global _engine_url, _bearer_token
    parser = argparse.ArgumentParser(
        description="MCP stdio proxy to local Topos engine (/api/local/*). No Control Plane."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("ENGINE_URL", DEFAULT_NODE_LOCALHOST_URL),
        help=f"Engine base URL (default: ENGINE_URL or {DEFAULT_NODE_LOCALHOST_URL})",
    )
    args = parser.parse_args()
    _engine_url = args.url.rstrip("/")
    _bearer_token = (os.environ.get("BEARER_TOKEN") or "").strip()
    if not _bearer_token:
        print("Error: BEARER_TOKEN env var required.", file=sys.stderr)
        return 1

    mcp = FastMCP("Topos (local engine)", port=0)

    @mcp.tool()
    async def list_database_tables() -> dict:
        """List all database tables grouped by layer with row counts."""
        return await _call_engine("/api/local/list_database_tables")

    @mcp.tool()
    async def get_table_schema(table_name: str) -> dict:
        """Get column info (schema) for a table. Use list_database_tables to see available tables."""
        return await _call_engine("/api/local/get_table_schema", {"table_name": table_name})

    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
