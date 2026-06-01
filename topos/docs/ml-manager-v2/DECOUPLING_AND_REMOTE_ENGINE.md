# Decoupling and Remote Engine

Can the Engine run on a separate machine and be used by Topos for inference?

---

## Short answer

**Designed for it, not implemented yet.** The Engine has a stable, JSON-friendly contract (`ProcessingTask` → `ProcessingResult`) and a single entry point (`run` / `submit`), so it *can* be moved behind a network boundary. There is **no transport layer** yet: no remote server and no client that calls it. All current callers use `Engine()` in-process.

---

## What’s in place (decoupling-ready)

| Aspect | Status |
|--------|--------|
| **Stable interface** | `run(task) -> ProcessingResult` and `submit(task) -> TaskHandle` are the only entry points; callers don’t depend on internals. |
| **Serializable contract** | `ProcessingTask` and `ProcessingResult` are Pydantic models with `model_dump(mode="json")` / `model_dump_json()` and round-trip from JSON (see `engine/tasks.py`). Safe to send over HTTP/WebSocket. |
| **Stateless execution** | A single task in → single result out; no server-side session required. Fits “stateless task execution” (PRD §12.1). |
| **No direct DB in core** | Engine does not open the Topos DB; it only receives task input and returns result. Data is passed in the task; the “full access to raw data” requirement is about what Topos sends in the task, not the Engine opening DBs. |

So: the **contract and interface** are suitable for a remote Engine node. What’s missing is the **transport** and **deployment** story.

---

## What’s missing for remote execution

1. **Engine as a service**
   - A separate process (or container) that:
     - Exposes an HTTP or WebSocket API that accepts a JSON `ProcessingTask` and returns a JSON `ProcessingResult` (and, for `submit`, a way to poll or stream status/result).
   - Could be a small FastAPI app: `POST /run` with `task` body, response `result`; optional `POST /submit` and `GET /tasks/{id}`.

2. **Client in Topos**
   - An implementation of the same logical interface that sends the task to the remote service and returns the result (e.g. `RemoteEngineClient.run(task)` → HTTP POST, then parse `ProcessingResult`).
   - Topos would then use this client instead of `Engine()` when configured for “remote Engine” (e.g. `ENGINE_REMOTE_URL` set).

3. **Configuration**
   - e.g. `ENGINE_REMOTE_URL` (or `ENGINE_MODE=remote` + URL). When set, Topos uses the remote client; when unset, uses in-process `Engine()` as today.

4. **Auth / security**
   - Remote Engine is “trusted” with the payload (PRD §12.1). Auth (API key, mTLS, etc.) and TLS for the Topos ↔ Engine link need to be defined when you add the transport.

---

## How callers use the Engine today

All of these are in-process only:

- `ingestion/ingest_helpers.py` — `Engine().run(task)` for URL classification on write.
- `api/enrichment.py` — `Engine().run(task)` for test and backfill.
- `enrichment/website_classifier.py` — `Engine().run(task)` in `classify_url()`.
- `enrichment/jobs/canonical/emo_27_job.py` — `Engine().run(task)` per message.

To support a remote Engine without changing each caller, introduce an **abstraction** that Topos uses everywhere instead of `Engine()`:

- **Option A:** Factory that returns either a local `Engine` or a `RemoteEngineClient` based on config (e.g. `get_engine()` → `Engine()` or `RemoteEngineClient(url=settings.engine_remote_url)`).
- **Option B:** Single `Engine` class that takes an optional “backend”: if `remote_url` is set, it delegates to an internal HTTP client; otherwise runs in-process as now.

Then the same `run(task)` / `submit(task)` calls work locally or remotely depending on configuration.

---

## PRD reference

PRD §12 (Remote Processing) requires:

- Engine deployable as a **separate node**.
- **WebSocket-compatible** task contract (we have JSON-serializable task/result; WebSocket can carry JSON).
- **Stateless** task execution (we have that).
- **Full access to raw data** in the task (caller puts data in `task.input`; no change needed on Engine side).

So the **product requirement** for decoupling is already stated; the **implementation** is “future” (see [IMPLEMENTATION_MAP.md](./IMPLEMENTATION_MAP.md) §4, [ARCHITECTURE_MAPPING.md](./ARCHITECTURE_MAPPING.md) Transport Layer, [MIGRATION_AND_GAPS.md](./MIGRATION_AND_GAPS.md) gap table).

---

## Summary

| Question | Answer |
|----------|--------|
| Is the Engine *designed* so it can be decoupled and run on another machine? | **Yes.** Stable interface, JSON-serializable contract, stateless execution. |
| Can Topos *today* use an Engine on another machine for inference? | **No.** There is no remote server and no client; everything uses `Engine()` in-process. |
| What’s needed to get there? | A remote Engine service (HTTP/WebSocket), a client that implements the same interface, config to switch between local and remote, and optional auth/TLS. |
