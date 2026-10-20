# PRD: Query serve latency

## Problem

The owner can see how many MCP calls a day contained, and not how long the node took to serve a query. Existing clocks do not answer that. `mcp_request_log` has no duration. `query_artifacts.duration_ms` is one blended number on a content row. Engine usage rows drop calls that produced no tokens and must not copy duration into time-to-first-token. Home-chat timings are the browser's clock.

## Goals

- The owner sees, for a UTC day, the median time to serve a query, the median database retrieval time, and the median LLM duration.
- Time to first token is shown only when a stream observed a first token. This sprint does not make `Engine.run` stream, so that value stays unavailable.
- A span stores numbers and closed labels. It does not store the question, the SQL, the prompt, or the answer.

## Goal Alignment Check

- Change: The owner can see how long their node took to serve a query, split into database response time and LLM duration, without storing the question or the answer.
- Goal score: G1=1, G2=2, G3=1, G4=2, G5=2, Total=8
- Risks to goals: A span that copies query text, SQL, or a prompt would turn a performance log into a second copy of private data. Filling `ttfb_ms` from `duration_ms` would repeat the usage-row bug.
- Required adjustments before merge: Closed labels and numbers only. `ttfb_ms` stays null unless a first token was observed. Timeouts and errors are rows.
- Validation plan: Unit tests refuse a sentence in any field, assert a facts-only turn has no LLM span, and assert a non-streaming inference span has duration and null TTFB. Dashboard tests show the percentiles for a covered day and stay unavailable before the first span.

## Persistence

Table `serve_spans`, migration 89 `serve_spans_v1`.

- `span_id`, `parent_span_id`, `recorded_at`, `kind`, `name`, `duration_ms`, `ttfb_ms`, `outcome`
- `kind` is `request`, `db`, or `llm`
- `name` is `query`, `retrieval`, or `query_inference`
- `outcome` is `ok`, `timeout`, or `error`
- `ttfb_ms` is allowed only on `llm`, and only when it is less than or equal to `duration_ms`

`handle_query` writes the request span. The query pipeline publishes retrieval and, when inference ran, the LLM span. Children point at the request span.

## Read path

`daily_measurements` adds `serve`. Days before the first span are unavailable. The first retained day and the as-of day are partial. A covered day with no spans of a kind has a count of zero and a null percentile. `ttfb_p50_ms` is null unless at least one LLM span that day recorded a first token.

The control plane already returns the node payload. It does not change.

The Activity tab shows request, database, and LLM p50. TTFB stays unavailable while null. An old node that omits `serve` leaves that card unavailable.

## Out of scope

- Streaming `Engine.run`
- Timing every MCP tool
- Answer-job writer and judge lines
- Duration on `mcp_request_log` or usage rows
- Home-chat `ttft_ms`

## Engineering

Pack: [serve_latency_team_sprints](serve_latency_team_sprints/README.md).

| Team | Sprint | Ticket | Acceptance | Tests |
|---|---|---|---|---|
| Engine | 01 | EN-S1-01 | Closed span rows for request, db, and llm | `tests/topos/test_serve_spans.py` |
| Engine | 01 | EN-S1-02 | Query turns write those rows, including errors and facts-only turns | `tests/topos/test_serve_spans.py` |
| Engine | 01 | EN-S1-03 | Daily p50 follows the retained-day contract | `tests/storage/test_dashboard_measurements.py` |
| Next | 01 | NX-S1-01 | Activity card shows the three p50s and unavailable TTFB | `measurements.test.ts`, `DailyDashboard.test.tsx` |
