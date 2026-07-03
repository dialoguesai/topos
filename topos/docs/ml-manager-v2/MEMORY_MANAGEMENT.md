# Engine memory management

Engine-owned ML model lifecycle: cache, eviction, flush, and observability. Compatible with co-deployed `topos-node` and split Cloud Run (`topos-database` + `topos-engine`).

## Standard behavior

After each enrichment run, signal derivation, or canonical pipeline, the engine **automatically trims** the model cache to `ENGINE_MAX_RESIDENT_MODELS` (LRU eviction). No configuration required.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `ENGINE_MAX_RESIDENT_MODELS` | `3` | Max Hugging Face model slots resident at once |
| `ENGINE_MODEL_IDLE_TTL_SEC` | `0` | Evict idle slots after N seconds (`0` = disabled) |
| `ENGINE_MEMORY_RSS_SOFT_LIMIT_MB` | `0` | Evict when process RSS exceeds threshold (`0` = disabled) |
| `ENGINE_FLUSH_AFTER_TASK` | `false` | Trim cache after each `Engine.run()` (usually leave off) |
| `ENGINE_ML_DEVICE` | unset | Default torch device (`cpu`, `cuda`, `mps`) |
| `TOPOS_ENGINE_SERVICE_URL` | unset | Remote engine base URL for database-only deployments |
| `PRIVACY_FILTER_DEVICE` | unset | Override device for privacy-filter only |

## Local development tips

Reduce memory pressure while developing in Cursor:

```bash
# Default: ENGINE_MAX_RESIDENT_MODELS=3; pipeline flush is automatic
# Use 2 only under heavy memory pressure (e.g. Cursor integrated terminal):
# export ENGINE_MAX_RESIDENT_MODELS=2
export PRIVACY_FILTER_DEVICE=cpu
# Optional: disable heavy classifiers during UI-only work
export PRIVACY_FILTER_ENABLED=false
export NSFW_CLASSIFIER_ENABLED=false
```

Run `topos-node` in an external terminal (iTerm, Terminal.app) when running full enrichment pipelines — integrated IDE terminals buffer verbose logs and share memory with the editor.

## Verification

1. Monitor Python RSS while ingesting: `ps -o rss= -p $(pgrep -f topos-node | head -1)`
2. Check resident slots: `curl -H "Authorization: Bearer $TOPOS_KEY" http://localhost:9000/v1/engine/memory`
3. After a pipeline completes, resident slot count should drop.

## Split deployment

- **topos-database** (lean image): set `TOPOS_ENGINE_SERVICE_URL` to the engine Cloud Run URL; database plane sends `ProcessingTask` JSON via HTTP.
- **topos-engine** (ML image): default `ENGINE_MAX_RESIDENT_MODELS=3`; pipeline flush is automatic.

See `scripts/gcp/deploy-topos-engine.sh` and `deploy-topos-database.sh` for hosted defaults.
