# Next — Sprint 1: Serve latency card

| Field | Value |
|---|---|
| Sprint ID | NX-S1 |
| PRD | [PRD_SERVE_LATENCY.md](../../PRD_SERVE_LATENCY.md) |
| Depends on | Engine sprint 01 |
| Unblocks | Manual check on a signed-in owner dashboard |

## Sprint goal

The Activity tab shows the day's serve percentiles from `serve`.

## Product goal

The owner can read how long queries, retrieval, and the model took, and can see that time to first token is not measured yet.

## Gap

| Area | Before | After |
|---|---|---|
| Activity tab | Call list, token breakdown, grants | Those, plus serve p50 |
| Old node | No `serve` key | The new card stays unavailable |

## In scope

`measurements.ts` parser and the Activity-tab card.

## Out of scope

A chart of every span, streaming TTFB, and any control-plane change.

## Tickets

### NX-S1-01 — Latency card

**Acceptance criteria**

- A covered day renders request, database, and LLM p50 in milliseconds.
- TTFB renders unavailable when `ttfb_p50_ms` is null.
- Omitting `serve` leaves the card unavailable. The request count still parses.

**Gap closed:** The series is visible on the dashboard the owner already uses.

**Tests:** `features/home-dashboard/daily/measurements.test.ts`, `features/home-dashboard/daily/DailyDashboard.test.tsx`

**Goal mapping:** G4, G5

## Risks

A strict parser that throws on a missing `serve` would blank the rest of the dashboard. Missing means empty, same as callers.

## Test plan summary

`npx vitest run features/home-dashboard/daily/measurements.test.ts features/home-dashboard/daily/DailyDashboard.test.tsx`

## Definition of Done

- Both test files pass.
- The card copy does not invent a first-token time.
