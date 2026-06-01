# Sanitization field transforms (v1)

## Where this runs

`shared/filtering.py` defines catalog entries.  
`topos/uma_filters.py` applies `field_transforms` on UMA reads (e.g. `uma_get_messages`).

- **`timestamp_to_date`** — local, no LLM.
- **Sanitization IDs** — optional **Ollama** when `sanitization_ollama_enabled` is true (see below).

## Configuration layers (precedence)

1. **Device DB** — JSON in SQLite `engine_config` under key `sanitization_ollama_device` (highest precedence).
2. **`topos.config.settings`** — loaded from `topos/.env` and environment (`SANITIZATION_OLLAMA_*`).

Per **transform** (`pii_redaction`, `nsfw_sanitization`, …), the model used is:

`device.models[transform_id]` → else `settings.sanitization_ollama_model_<transform_id>` → else `sanitization_ollama_default_model` (default **`llama3.2`**).

Host resolution: `device.host` → `sanitization_ollama_host` → `engine_ollama_base_url` → `http://127.0.0.1:11434`.

## Settings / `.env` (defaults)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SANITIZATION_OLLAMA_ENABLED` | `false` | Master switch |
| `SANITIZATION_OLLAMA_HOST` | _(see above)_ | Ollama base URL |
| `SANITIZATION_OLLAMA_DEFAULT_MODEL` | `llama3.2` | Model when no per-transform override |
| `SANITIZATION_OLLAMA_TIMEOUT_SEC` | `120` | HTTP timeout |
| `SANITIZATION_OLLAMA_MAX_INPUT_CHARS` | `8000` | Truncate long fields |
| `SANITIZATION_OLLAMA_MODEL_PII_REDACTION` | _(unset)_ | Override model for PII only |
| `SANITIZATION_OLLAMA_MODEL_NSFW_SANITIZATION` | _(unset)_ | Override model for NSFW only |
| … | | Same pattern for each transform id (see `settings.py`) |

## Device overrides (frontend / local UI)

Use the same `TOPOS_KEY` in `Authorization: Bearer …` as `/healthcheck` and `/v1/ui-config`:

- **Control plane URL** (e.g. `https://cp.example.com`): the CP **proxies** `GET|PUT|DELETE /v1/sanitization-ollama-config` to the connected engine over the engine WebSocket.
- **Engine HTTP URL** (direct): the Topos app exposes the same routes locally.

Endpoints:

- **GET** `/v1/sanitization-ollama-config` — returns `defaults_from_settings`, `device_overrides`, `effective` (merged), and `transform_ids`.
- **PUT** `/v1/sanitization-ollama-config` — body `{ "device_overrides": { ... } }` (partial JSON; omitted keys keep file/env defaults).
- **DELETE** `/v1/sanitization-ollama-config` — clears device row to `{}`.

**`device_overrides` shape** (all fields optional):

```json
{
  "version": 1,
  "enabled": true,
  "host": "http://127.0.0.1:11434",
  "default_model": "llama3.2",
  "timeout_sec": 120,
  "max_input_chars": 8000,
  "models": {
    "pii_redaction": "llama3.2",
    "nsfw_sanitization": "mistral"
  }
}
```

## Ollama setup

```bash
ollama pull llama3.2
```

Enable in `.env` on the engine host, then tune per-transform via env or PUT overrides.

## Implemented transform_ids (Ollama)

| `transform_id` | Params (optional) |
|----------------|-------------------|
| `pii_redaction` | — |
| `nsfw_sanitization` | — |
| `raw_to_summary` | `style`, `max_length` |
| `raw_to_sentiment` | `scale` |
| `third_party_anonymization` | `mode` |
| `name_removal` | — |
| `contact_removal` | — |

Prompts: `topos/sanitization/ollama_transforms.py`.

## Production notes

- **Latency**: one Ollama call per row per transform; consider batching later.
- **Fail-open**: HTTP/parse errors log a warning and keep the original string.
- **Merge implementation**: `topos/config/sanitization_ollama.py` (`resolve_sanitization_ollama_effective`).
