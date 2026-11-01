# Routine run quota

The default allowance is **100 runs per owner and engine in a rolling 24 hours**.
All run statuses count, using each row's last-updated timestamp. This is a usage
policy, separate from compute admission, deadlines and concurrent execution.

Operators can configure the node process with:

```dotenv
TOPOS_ROUTINE_MAX_RUNS_PER_OWNER_24H=100
TOPOS_ROUTINE_DEVELOPMENT_UNLIMITED_RUNS=true
```

The second setting explicitly disables this rolling daily quota for a development
or benchmark node. It defaults to false and applies node-wide. Routine/request
payloads cannot enable it. Put the settings in the node's operator environment
(the standard launcher also loads its configured `.env`) and restart the node to
apply them. A positive integer is required for the configured finite limit, even
when development mode is enabled. Turning development mode off restores that limit.

Development mode preserves run history, owner/engine scoping, active-run exclusion,
idempotency checks, compute limits, request deadlines and delivery configuration.
It does not change the database/compute routing boundary. Scheduled benchmark
automation does not change this setting or restart the node.

`GET /v1/shell/status` reports `routine_run_policy` from the same configuration
used by admission: contract version 1, effective `limit` (null when unlimited),
configured finite limit, explicit development flag, window, scope and timestamp
basis. Benchmarks must record this policy and distinguish it from historic
releases that enforced 50. Never interpret missing policy as unlimited.

This feature requires a release containing these changes. Published 1.5.37 and
earlier do not read these settings and retain their existing quota.
