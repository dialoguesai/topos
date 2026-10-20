# Next

## Mission

Show the node's serve percentiles on the home dashboard Activity tab.

## Product goal

On the day the owner selects, the card states the median query, database, and model times, and says time to first token is unavailable until a stream measures it.

## Engineering requirements

1. Missing `serve` leaves the card unavailable and does not blank tokens, requests, callers, or routines.
2. A covered day shows the three duration percentiles.
3. Null `ttfb_p50_ms` renders as unavailable, including when LLM duration is present.

## Sprint index

| Sprint | Theme |
|---|---|
| [sprint-01-latency-card.md](sprint-01-latency-card.md) | Activity-tab serve latency card |

## Goal mapping

NX-S1-01 improves G4 and G5.
