# Fact Pack processing measurements

`derivation_processing_v1` (migration 90) starts node-local call measurements from
installation onward. It does not reconstruct past latency. The owner's existing
`GET /derivation/packs` response now carries `processing_30d`; the CP forwards it
through the same authorized catalog response. No new public/grantee read surface.

Each actual Ollama extraction or verification call records pack ID, requested
resolved model ID, phase, ingest/catchup/trial/backfill mode, UTC start, elapsed
client wall time, failure flag, and available tokens/load/prefill/decode timing.
No source text, prompts, output, source identifiers, or fact contents are stored.
The training ledger still describes assertion acceptance; a rejected assertion is
not a failed model call. Availability probes are excluded. Each verifier vote is
one call. Metrics join the enclosing derivation transaction, without committing
it or modifying fact freshness, confidence, decay, or permission state. A rolled
back/crashed transaction may lose its measurements along with its work. Telemetry
write failure is logged and never changes the derivation outcome.

The catalog computes 30-day aggregates separately for all packs and each pack,
by model and phase. P95 is nearest-rank across actual call durations, including
failed attempts, never an average of per-pack percentiles. Average model loading
and token totals retain sample counts because older/stubbed adapters can omit
those fields; missing samples aren't zero. Total model time sums calls and isn't
elapsed job time. Disabled trialed packs can appear in the All packs summary.

The React processing card supports pack selection and separates extraction from
verification. An older node shows unavailable. A migrated node with no calls
shows no measured calls, and includes the tracking start once available.

Local app preview uses the existing remote node. The tracking migration and
instrumentation need a node release before live measurements appear. Do not
present fixture values as real owner data.

The processing card offers Today, This week, and Last 30 days. The web client
forwards its IANA timezone via the existing catalog route. The node returns
`processing_30d.windows` summaries computed from raw calls, not scaled estimates:
Today begins at local midnight, This week at Monday local midnight (DST-aware),
and Last 30 days is rolling. Invalid timezones fall back to UTC. Future-dated
calls are excluded. Legacy 30-day fields remain compatible.
