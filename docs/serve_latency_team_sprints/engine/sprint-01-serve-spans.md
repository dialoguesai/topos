# Engine — Sprint 1: Query serve spans

| Field | Value |
|---|---|
| Sprint ID | EN-S1 |
| PRD | [PRD_SERVE_LATENCY.md](../../PRD_SERVE_LATENCY.md) |
| Depends on | Existing `StageTimings.retrieval_ms` and `inference_ms` |
| Unblocks | Next sprint 01 |

## Sprint goal

A query turn leaves content-free duration rows the dashboard can summarize.

## Product goal

The owner can see median serve, retrieval, and model time for a day.

## Gap

| Area | Before | After |
|---|---|---|
| Request log | Time, tool, source. No duration | `request` span with `duration_ms` |
| Retrieval | `retrieval_ms` inside the decision record | `db` span |
| Inference | Wall time around non-streaming `Engine.run` | `llm` span, `ttfb_ms` null |
| Dashboard payload | No serve series | `serve` percentiles with the retained-day contract |

## In scope

`serve_spans`, the validator, `handle_query`, the query pipeline publish, and `daily_measurements`.

## Out of scope

Streaming first-token measurement, other MCP tools, writer/judge lines, usage rows, home-chat timings.

## Tickets

### EN-S1-01 — Closed span store

**Acceptance criteria**

- Migration 89 creates `serve_spans`.
- A name outside `query`, `retrieval`, `query_inference` is refused, and the exception text does not contain the rejected value.
- `ttfb_ms` on a `db` span is refused.

**Gap closed:** The table exists and cannot hold a question.

**Tests:** `tests/topos/test_serve_spans.py`

**Goal mapping:** G2, G4

### EN-S1-02 — Query clocks

**Acceptance criteria**

- A turn with retrieval and inference writes `request`, `db`, and `llm` durations. The LLM row's `ttfb_ms` is null.
- A turn with retrieval and no inference writes no `llm` row.
- A failed `handle_query` still stores a `request` row with outcome `error`.

**Gap closed:** The serve path records the three categories.

**Tests:** `tests/topos/test_serve_spans.py`

**Goal mapping:** G4, G5

### EN-S1-03 — Daily percentiles

**Acceptance criteria**

- Days before the first span are unavailable.
- A later covered day returns nearest-rank p50 for each kind that has rows, and a zero count with a null percentile for a kind that has none.
- `ttfb_p50_ms` stays null when no span recorded a first token.

**Gap closed:** The measurements payload can feed the card.

**Tests:** `tests/storage/test_dashboard_measurements.py`

**Goal mapping:** G4

## Risks

A span write must not fail the query. The writer swallows its own errors.

## Test plan summary

`pytest tests/topos/test_serve_spans.py tests/storage/test_dashboard_measurements.py`

## Definition of Done

- The three tickets' tests pass.
- No query text is written to `serve_spans`.
- `ttfb_ms` is null on every span this sprint writes.
