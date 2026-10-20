# Serve latency runbook

## Architecture

| Layer | Path | Role |
|---|---|---|
| Schema | `topos/storage/db/migrations/serve_spans_v1.py` | Table and migration 89 |
| Writer | `topos/serve/spans.py` | Validate and store content-free spans |
| Request clock | `topos/core/handlers/query.py` | `handle_query` wall time |
| Stage clocks | `topos/query/pipeline.py` | Publish retrieval and inference durations |
| Read | `topos/dashboard/measurements.py` | Daily p50 on `serve` |
| Proxy | `control_plane/routes/dashboard.py` | Unchanged. Returns the node payload |
| Card | `topos-react-app/next/features/home-dashboard/daily/` | Activity tab |

## Execution waves

1. Engine store, clocks, and `serve` percentiles.
2. Next parser and card. Depends on the `serve` shape from wave 1.

Goal check after wave 1: a test turn's rows contain durations and null `ttfb_ms`, and no query text. Goal check after wave 2: the card shows those percentiles and unavailable TTFB.

## Commands

From the node checkout:

```bash
pytest tests/topos/test_serve_spans.py tests/storage/test_dashboard_measurements.py
```

From `topos-react-app/next`:

```bash
npx vitest run features/home-dashboard/daily/measurements.test.ts features/home-dashboard/daily/DailyDashboard.test.tsx
```

## Manual verification

- Sign in as the owner of PersonalDB on the local dashboard.
- Run one query that reaches the model and one that answers from facts.
- Open Activity for that UTC day.
- The model query shows request, database, and LLM p50. TTFB says unavailable.
- The facts query does not add an LLM span.
- Fact releases, active recipients, and What was shared stay unavailable.

## Implementation final outcome

An owner query on the node produces a request span and a database span, plus an LLM span only when inference ran. The dashboard day shows those medians. Time to first token stays unavailable until a later sprint streams the model call.

### In practical terms

1. I can run a query and find `request` and `db` rows in `serve_spans`, with an `llm` row only when the model ran.
2. I can open the Activity tab and read the three p50 values for that UTC day.
3. I can see TTFB unavailable, and I can see that the span row does not contain the question.
