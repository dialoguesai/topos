# Permissions beta foundation

This is an isolated beta branch, based on released 1.3.57. It has not been
published or deployed to the owner's running node. It preserves the existing
record Off-limits experiment and released resource, owner, device, stream, and
disclosure authority checks.

## Executable contract

An absent optional filter is unrestricted for that dimension. A present
`source_filter.source_ids: []` or `column_allowlist.fields: []` permits no rows.
Missing or null required list parameters are invalid. Intersecting restrictions
preserves the empty set, including after an intermediate disjoint intersection.
The existing ceiling order remains `summary < inference < raw`; this change
does not widen or reinterpret historical consent.

The typed manifest preserves `access_mode_ceiling` and
`scope_table_allowlist: {scope_id: [table_id, ...]}`. A scope's explicit empty
table selection cannot be erased by another scope for an unrelated table.
UMA HTTP, relay message readers, and generic rows enforce persisted raw view
and table projections themselves, in addition to control-plane checks.

Column allowlists apply to sender enrichments too. No implicit exception adds
names, owner flags, or other unselected fields back into a projected row.
Generic source predicates execute before SQL pagination; empty selections
cannot reveal `has_more` or `next_offset` from inaccessible records.

The query disclosure adapter decodes the actual outer grant envelope. It
consumes canonical singular field transforms for timestamp-to-date and the
existing deterministic PII/NSFW transforms. Invalid, unsupported, or wrongly
typed mandatory transform targets fail closed; legacy plural deterministic
transform fixtures remain supported.

## Supported and withheld capabilities

| Path | Beta behavior | Limit |
| --- | --- | --- |
| UMA raw message and generic row reads | Existing released resource checks plus explicit empty source/column handling, scope-correlated table selection, raw ceiling, and source-aware generic pagination | This is not certification of every historical raw filter combination or hosted PostgreSQL deployment |
| Raw query output | Saved nested manifests and deterministic field transforms reach the real disclosure pipeline | Unsupported transforms reject; contributor and output metadata are not a new universal lineage certificate |
| Shared inference | Only exact `availability:read`, with the existing closed free/busy output vocabulary | Other inference views return `inference_view_unsupported` before retrieval, regardless of forged owner IDs or raw tiers |
| Derived queries with typed filters | Explicitly withheld before evidence retrieval | No typed derived filter is certified yet; defaults such as rolling windows and row caps also return `derived_filter_lineage_unavailable` |
| Derived queries with table projections or field transforms | Explicitly withheld before evidence retrieval | Input lineage/recomputation is required before enabling these combinations |
| Unconstrained availability inference | Remains available through the closed output contract | Does not imply a grant with a default window/cap has been enforced; such a grant is withheld |
| Owner inference | Verified owner application channel retains its owner view | Matching string IDs or requesting `owner_raw` is insufficient owner authority |
| Semantic inference evidence | A positive schema admits only IDs/source/similarity/dimension/time/type; raw indexed `search_text`, previews, and future fields are excluded | Raw owner retrieval and authorized derived facts are separate products |
| Record Off-limits | Existing exact record protection, owner controls, output recheck, cache invalidation, and conservative derived withholding retained | No selective descendant recomputation is claimed |
| Record protection capability | HTTP and relay list only present native canonical tables with selectable IDs | Unknown or malformed table schemas are not advertised |

Denials advertise `supported_inference_scopes: ["availability:read"]` and
`supported_derived_filter_ids: []`. Absence of a typed filter is different from
having a filter that happens not to change the final prose. An availability
query-date hint does not enforce a granted time window: the current timing
evaluator never receives the manifest. Similarly, capping result objects does
not prove a cap on their contributing records.

## Verification and remaining work

New tests drive saved-manifest merge algebra, actual SQLite UMA readers,
pagination side outputs, real query handler/disclosure flow, semantic retrieval,
model input, trusted-owner exceptions, and pre-retrieval capability denials.
Positive controls retain authorized rows, availability answers, semantic scores,
owner protected content, and owner fact inference.

The expanded gate passed **2,005 tests**, with 17 skips, two deliberately
deselected live cases, and 17 expected failures (including the two previously
known privacy exceptions). The final focused gate passed **175 tests** after
the additional empty-selection contact-sidecar short circuit. These suites
overlap; their counts must not be added together. All runs used the live DB
tripwire and scratch databases, with no production runtime changes.

Before repair, 12 initial boundary cases failed. Three additional generic SQL
pagination cases failed (two message-reader controls passed). A further 12
derived-default probes reproduced ignored time, cap, topic, and emotion
obligations. The test fixtures for owner controls now supply authenticated
owner principals; truncation fixtures now use admissible score fields instead
of forbidden rows or unknown extensions. Their original behavioral assertions
remain in place.

Run tests with scratch `TOPOS_DATABASE_PATH` and `TOPOS_BACKUP_DIR`, a synthetic
`TOPOS_KEY`, `LOG_FORMAT` and `TOPOS_SKIP_UPDATE_CHECK` unset,
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`,
`ENGINE_OLLAMA_BASE_URL=http://[::ffff:127.0.0.1]:9`,
`SANITIZATION_PREWARM_ON_STARTUP=false`, and `TOPOS_CLUSTER_LLM_LABELS=off`.
Invoke pytest through `scripts/live_db_tripwire.py --command` using this
checkout's locked virtual environment. The broader gate covers `tests/query`,
`tests/evals/privacy`, the released authority tests, record/entity Off-limits,
shared filters, snapshot isolation, dimension parity, and bounded inference.

Next work remains contributor-level predicate and lineage enforcement, certified
derived projection schemas, fully correlated grants, natural-language policy
evaluation, and the isolated four-recipient A/B campaign. This foundation must
not be reported as completion of those capabilities.
