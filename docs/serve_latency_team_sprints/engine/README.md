# Engine

## Mission

Own the serve-span table, the query-path clocks, and the daily percentiles on `daily_measurements`.

## Product goal

The owner can tell a slow query apart from a slow retrieval and a slow model call, for the day they select.

## Engineering requirements

1. A span is numbers and a closed label. A sentence is refused, and the refusal does not repeat the value.
2. `ttfb_ms` is rejected on a database span and is never copied from `duration_ms`.
3. `handle_query` writes one `request` span. Retrieval writes one `db` span. Inference writes one `llm` span with null TTFB. A facts-only turn writes no LLM span. An error still writes the request span.
4. `serve` on the measurements payload uses the same UTC coverage rules as callers and routines.

## Sprint index

| Sprint | Theme |
|---|---|
| [sprint-01-serve-spans.md](sprint-01-serve-spans.md) | Record and summarize query serve spans |

## Goal mapping

EN-S1-01 improves G2 and G4. EN-S1-02 improves G4 and G5. EN-S1-03 improves G4.
