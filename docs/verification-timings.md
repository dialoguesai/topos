# Standard verification timing attribution

The standard `verify_claim` handler keeps content-free timings in its internal
`_audit` record. The handler removes that record before returning the response.
No evidence, statement text, provider output, or new timing fields are added to
the public verification contract.

Each actual judge invocation has a generated `truth_verdict:<UUID>` task ID.
Engine usage observations deduplicate by task identity: using a constant ID hid
all but the first verification. The generated ID correlates the internal audit
and usage event without encoding the statement or caller identity. Re-delivery
of the same usage observation remains idempotent; separate computations have
separate identities.

| Internal field | Meaning |
| --- | --- |
| `worker_ready` | Dispatch until worker-owned connection is available; executor scheduling plus connection acquisition |
| `worker_run` | Worker closure, including guard construction and verification |
| `worker_return_to_loop` | Worker completion until the coroutine resumes |
| `dispatch_to_result` | Sum of those three intervals; excludes subsequent usage accounting and HTTP serialization |
| `retrieval_and_filter` | Fact lookup and disclosure/Off-limits filtering |
| `engine_roundtrip` | Synchronous Engine client call and result interpretation, including local/remote transport and engine setup |
| `engine_adapter` | Adapter execution duration reported by the Engine, when present |
| `verify_total` | Verification function through stance construction |
| `provider_timings_ms` | Reported `total_ms`, `load_ms`, `prefill_ms`, `decode_ms`, when available |

These spans nest; do not sum them as disjoint stages. Node spans use a monotonic
clock. Only finite nonnegative numeric provider durations up to 24 hours are
retained. Absent measurements remain absent. A no-evidence short circuit has no
invented inference timing.

Provider `load_ms` is not a pure disk/model-weight loading measurement. In Ollama,
the request-start to runner-ready interval includes scheduler acquisition. It
must not be relabelled as measured GPU queue time. Separate scheduling, loading
and execution instrumentation is required to break that interval down further.

Both local and remote Engine clients already return provider usage through
`ProcessingResult.output`. Attribution uses that existing contract and preserves
configured routing, authorization, filtering and model selection. It introduces
no cross-machine database connection, scheduler, or admission policy.
