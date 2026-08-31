# Topos (node)

This package runs the **Topos node**: a single process that today bundles **Topos Database** (data plane: storage, ingestion, sync) and **Topos Engine** (compute: LLMs, filtering/sanitization workers). It connects to the Control Plane over WebSocket; MCP tools for remote assistants are provided by the Control Plane gateway at `/mcp`, not by local MCP alone.

> Vocabulary: [`../../topos-control-plane/docs/TOPOS_DATABASE_AND_ENGINE_TERMINOLOGY.md`](../../topos-control-plane/docs/TOPOS_DATABASE_AND_ENGINE_TERMINOLOGY.md) (canonical names vs legacy `ENGINE_*` identifiers).

---

## One Topos node, Claude + ChatGPT (via VM Control Plane)

If you run the **Topos node** with the **VM Control Plane** (e.g. `cp.logu3s.com`), a **single configuration** supports both **Claude Desktop** and **ChatGPT** integration. No extra setup is required beyond connecting the node (database + engine ship together in this process).

1. **Start the node** (e.g. `topos run` or `uv run topos.cli run`) with:
   - **TOPOS_KEY** — A key that is **already registered** on that Control Plane with your **Dialogues user_id** (Keycloak `sub`) in its metadata. The frontend typically does this when you connect (e.g. `/register_engine_public` with `user_id`). Without `user_id` on the key, ChatGPT OAuth cannot resolve you to this node.
   - *(Optional override)* **TOPOS_CONTROL_PLANE_URL** — defaults to `wss://cp.logu3s.com/ws/engine` (legacy path name). Set only when targeting a different control plane.

2. The node connects to the Control Plane over WebSocket. The CP already has your key in `allowed_keys` with `user_id` (from Supabase `engine_keys` or registration).

3. **Claude Desktop:** Use the npx adapter with `URI=https://cp.logu3s.com/mcp/` and `BEARER_TOKEN=<same TOPOS_KEY>`. The adapter talks to the CP; the CP forwards to your connected **node**.

4. **ChatGPT:** Add the connector with URL `https://cp.logu3s.com/mcp/` and sign in with Dialogues. The CP validates the OAuth token, resolves your user (`sub`) to a node API key via `get_engine_key_for_user(sub)`, and forwards to that node—yours, if the key has your `user_id`.

So: one node process, one API key registered with your `user_id` on the VM CP. Both Claude (Bearer key) and ChatGPT (OAuth) then use the same node through the same Control Plane.

---

## Engine deployment profiles (Wave 3 parity)

Use the same compute contract (`compute_invoke` envelope + scoped token validation) in both local and cloud profiles:

- **Local/VM websocket profile**
  - `TOPOS_KEY=<registered key>`
  - `TOPOS_CONTROL_PLANE_URL=wss://<control-plane>/ws/engine`
  - `ENGINE_TRANSPORT_MODE=ws`
- **Cloud endpoint profile**
  - `ENGINE_TRANSPORT_MODE=endpoint`
  - `TOPOS_CONTROL_PLANE_URL=https://<control-plane>`
  - `TOPOS_KEY=<registered key>`

Both profiles should surface the same routing metadata (`engine_instance_id`, `policy_hash`, `request_id`, `fallback_reason`) so Next app and control-plane observability remain mode-agnostic.

---

## Use Topos with Claude Desktop

You can use Claude Desktop (or Claude Code) with Topos so Claude can call tools like `list_database_tables`, `get_analytics`, and `get_messages` against your **Topos node**. You **do not need to run the Control Plane locally** — use the remote Control Plane URL in the config.

### Quick setup

1. **Copy the example config** from this repo into Claude’s config directory, then replace `YOUR_TOPOS_KEY` with your node API key (the same key you use in the Topos app).

   **macOS:**

   ```bash
   mkdir -p ~/Library/Application\ Support/Claude
   cp docs/claude_desktop_config.json.example ~/Library/Application\ Support/Claude/claude_desktop_config.json
   ```

   Then edit `~/Library/Application Support/Claude/claude_desktop_config.json` and set `BEARER_TOKEN` to your node API key.

2. **Restart Claude Desktop** (quit fully, then open again).

3. In a new chat, Topos should appear as a tool. Ask e.g. “List my database tables.” (If your node isn’t connected to the Control Plane, you’ll get “Engine not connected” — connect the node with that API key to get data.)

**Prerequisites:** Node.js (for `npx`). The config uses the [stdio → Streamable HTTP adapter](https://www.npmjs.com/package/@pyroprompts/mcp-stdio-to-streamable-http-adapter) so Claude can talk to the Control Plane at `https://cp.logu3s.com/mcp/`.

**Direct to local node (no Control Plane):** If the node and Claude run on the same machine, you can use the repo’s stdio proxy so Claude talks straight to the node. In Claude config, set `command` to the full path to `scripts/run_local_mcp_proxy.sh` (uses repo **.venv**; run `uv sync --extra engine` at repo root), `args` to `["--url", "http://localhost:8676"]`, and `env.BEARER_TOKEN` to your node API key. Only **list_database_tables** and **get_table_schema** are available this way; for all tools use the remote Control Plane URL above.

**Full guide and troubleshooting:** [docs/SETUP_CLAUDE_DESKTOP.md](../docs/SETUP_CLAUDE_DESKTOP.md)
