# Sprint 06 — Scheduler and model-aware batching

**Topos Engine V1**

---

## Objective

Implement **priority ordering** (sync user > async user > write-event > batch > background) and **model-aware batching** (group tasks by model/batch_key so the same model runs consecutively to reduce load overhead). Ensure **fairness**: long batches do not indefinitely block user-triggered tasks.

**Plan refs:** [../MIGRATION_AND_GAPS.md](../MIGRATION_AND_GAPS.md) Step 8; [../IMPLEMENTATION_MAP.md](../IMPLEMENTATION_MAP.md) §7 (Queueing and scheduling); [../ARCHITECTURE_MAPPING.md](../ARCHITECTURE_MAPPING.md) (Scheduler).

---

## Scope

- **Scheduler**
  - Add `topos/engine/scheduler.py` (or extend queue_manager): logic to select which task(s) to run next.
  - **Priority:** Tasks have execution.priority or requested_by.origin (e.g. sync_user, async_user, write_event, batch, background). Order: 1) sync user, 2) async user, 3) write-event, 4) batch, 5) background (PRD §7.1).
  - **Model-aware batching:** When selecting from queue, group by (provider, model) or task.execution.batch_key; run all tasks for the same model in a batch before switching model (PRD §7.2).
  - **Fairness:** Cap batch size or batch duration so that a burst of batch tasks does not starve user tasks; after N batch tasks or T seconds, re-evaluate priority (PRD §7.3).
- **Integration**
  - Worker (or Engine.run path when pulling from queue) uses scheduler to get next task(s) instead of pure FIFO. Queue manager may expose “get_next()” that applies scheduler logic.
  - ProcessingTask must include execution.priority and/or origin and optional batch_key (from PRD §6.1); ensure intake sets defaults.

---

## Acceptance criteria

| ID | Criterion | How to verify |
|----|-----------|----------------|
| AC-6.1 | Scheduler returns tasks in priority order when no batching is applied (sync > async > write_event > batch > background). | Unit test: enqueue tasks with different priorities/origins; scheduler get_next in loop returns in correct order. |
| AC-6.2 | When model-aware batching is enabled, tasks with the same model/batch_key are returned consecutively. | Unit test: enqueue mix of tasks (model A, model B, model A); scheduler returns A, A, then B (or A batch then B batch). |
| AC-6.3 | Fairness: after processing a capped number of batch tasks (or time slice), a pending sync/async user task is selected next. | Unit test: enqueue many batch tasks and one sync user task; after batch cap, next selected is sync user task. |
| AC-6.4 | Worker uses scheduler to select next task; integration test shows priority and batching in effect. | Integration test: submit mix of priorities and models; assert order of completion respects priority and batching (within observable limits). |
| AC-6.5 | ProcessingTask supports execution.mode, execution.priority, execution.batch_key; intake sets defaults when missing. | Unit test: build task without execution; after intake, execution has default priority and batch_key or equivalent. |

---

## Implementation notes

- **Priority field:** Use execution.priority (integer; lower = higher priority) or requested_by.origin (string); map origin to priority in scheduler. PRD suggests priority 100 for async; sync can be 0 or 50.
- **Batch key:** From task (execution.batch_key) or derived from (provider, model); scheduler groups by this.
- **Fairness cap:** Configurable (e.g. max_batch_tasks_per_round=50 or max_batch_seconds=30); after cap, scheduler re-sorts by priority.

---

## Tests

| Test | Description |
|------|-------------|
| Priority order | Enqueue 5 tasks with priorities 0, 50, 100, 100, 200; get_next repeatedly; assert order 0, 50, 100, 100, 200. |
| Model batching | Enqueue A, B, A, B, A with same priority; scheduler with batching returns A, A, A, B, B (or equivalent). |
| Fairness | Enqueue 100 batch tasks (priority 200) and 1 sync task (priority 0); set batch cap 10; after 10 batch tasks, next is sync task. |
| Worker uses scheduler | Start worker with scheduler; submit 3 tasks (2 same model, 1 different); assert completion order shows batching (e.g. 2 same model complete before the other). |
| Default execution | Task without execution dict; after intake, task has execution with priority and batch_key defaults. |
| Empty queue | Scheduler get_next when queue empty returns None or blocks; no crash. |

---

## Definition of done

- [ ] Scheduler module implements priority ordering and model-aware batching; fairness cap implemented.
- [ ] Worker (or queue consumer) uses scheduler to select next task(s).
- [ ] ProcessingTask/intake support execution.priority, execution.batch_key, and requested_by.origin.
- [ ] All acceptance criteria met.
- [ ] Tests above added and passing.
