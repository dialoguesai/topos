# Sprint 05 — Queue and submit()

**Topos Engine V1**

---

## Objective

Add an **in-memory queue** and **Engine.submit(task)** that returns a **TaskHandle** (task_id, optional status/result). A **worker** (same process or background) dequeues tasks and runs them via Engine.run(); results are stored so callers can poll or wait for completion.

**Plan refs:** [../MIGRATION_AND_GAPS.md](../MIGRATION_AND_GAPS.md) Step 7; [../IMPLEMENTATION_MAP.md](../IMPLEMENTATION_MAP.md) §2 (Execution modes), §4 (Queue Manager); [../ARCHITECTURE_MAPPING.md](../ARCHITECTURE_MAPPING.md) (Queue Manager).

---

## Scope

- **Queue manager**
  - Add `topos/engine/queue_manager.py`: in-memory queue (e.g. `asyncio.Queue` or `queue.Queue`). Methods: enqueue(task), dequeue() (blocking or with timeout), optional size limit.
  - Optional: in-memory result store keyed by task_id (task_id → ProcessingResult or status).
- **Engine.submit(task)**
  - Validate task (same as run); enqueue(task); generate task_id if not set; return TaskHandle(task_id, optional future or status getter).
  - TaskHandle allows: get_status() → pending | running | completed | failed; get_result() → ProcessingResult when completed (optional timeout).
- **Worker**
  - Background loop or thread: dequeue task → Engine.run(task) → store result in handle/result store. Run in same process for V1; single worker is acceptable.
  - Start worker with Engine or app startup; document how to run worker.
- **API (optional)**
  - POST endpoint to submit a task (e.g. enrichment) and return task_id; GET endpoint to get task status/result by task_id. If not in this sprint, document as follow-up.

---

## Acceptance criteria

| ID | Criterion | How to verify |
|----|-----------|----------------|
| AC-5.1 | Engine.submit(task) returns a TaskHandle with task_id; task is enqueued. | Unit test: submit task; assert handle.task_id; assert queue size increased (or dequeue returns same task). |
| AC-5.2 | Worker dequeues and runs task; result is stored and retrievable via handle. | Integration test: submit task; run worker (or trigger one iteration); assert handle.get_status() becomes completed; handle.get_result() returns ProcessingResult. |
| AC-5.3 | TaskHandle.get_status() returns pending before run, running during (if supported), completed or failed after. | Unit/integration test: submit; poll status; after worker runs, status is completed or failed. |
| AC-5.4 | Engine.run() still works synchronously; existing callers unchanged. | Regression: call Engine.run(task) directly; behavior unchanged from Sprint 04. |
| AC-5.5 | Queue has a maximum size (configurable); submit when full returns error or blocks (documented). | Unit test: set max size; fill queue; next submit either fails with queue overflow or blocks; document behavior. |

---

## Implementation notes

- **Async vs sync:** Prefer asyncio.Queue if the app is async; otherwise queue.Queue with a dedicated thread for the worker. Engine.run() can stay sync; worker calls it in thread or run_in_executor.
- **Result store:** Simple dict or in-memory cache keyed by task_id; TTL or eviction optional for V1.
- **Persistence:** V1 does not require queue persistence; restart loses queue. Document in [../MIGRATION_AND_GAPS.md](../MIGRATION_AND_GAPS.md).

---

## Tests

| Test | Description |
|------|-------------|
| Submit returns handle | Engine.submit(minimal_task); assert handle has task_id; assert task in queue (dequeue once, compare id). |
| Worker runs submitted task | Submit task; run worker until queue empty; assert handle.get_status() == completed; get_result() returns ProcessingResult with same task_id. |
| Status transitions | Submit; assert status pending; after worker run, assert completed or failed; if failed (e.g. invalid task), result.error set. |
| Engine.run unchanged | Call Engine.run(task) directly; assert same behavior as before (no queue involved). |
| Queue full | Set max size to 1; submit two tasks; assert second submit fails or blocks per design; document. |
| Multiple tasks | Submit several tasks; worker processes all; assert each handle has result; order may not be strict. |

---

## Definition of done

- [ ] Queue manager implemented; Engine.submit(task) enqueues and returns TaskHandle.
- [ ] Worker loop dequeues and runs tasks; results stored and accessible via TaskHandle.
- [ ] TaskHandle exposes get_status() and get_result(); behavior documented.
- [ ] Engine.run() remains the sync path; no regression.
- [ ] All acceptance criteria met.
- [ ] Tests above added and passing.
