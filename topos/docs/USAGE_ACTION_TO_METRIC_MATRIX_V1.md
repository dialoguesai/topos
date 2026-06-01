# Engine v1 action-to-metric mapping

This artifact is the EN-S1 source of truth for engine-side usage observation mapping.

| Metric key | Producer action | Code path | Trust class | Billing mode | Status | Follow-up |
|---|---|---|---|---|---|---|
| `llm_tokens` | `llm.generate` | `topos/api/llm.py` | `cp_observed_self_hosted` | billable | active | none |
| `file_transfer_mb` | `ingestion.file_processed` | `topos/ingestion/manager.py` | `cp_observed_self_hosted` | billable (policy-conditioned) | active | align quantity unit with CP writer contract if contract changes |
| `permission_tickets` | `uma.permission_ticket.validated` | `topos/api/uma_data.py` | `observe_only` | observe-only | active | CP-S2 may move canonical producer to control-plane ticket issuance paths |
| `source_installs` | `source.install.completed` | `topos/api/source_install.py` | `observe_only` | observe-only | active | none |
| `third_party_connections` | `contacts.google.connect.started` | `topos/api/ingestion_sources.py` | `observe_only` | observe-only | active | add provider parity rows for non-Google connectors in EN-S2 |

## Idempotency namespace

Idempotency key format:

`<producer>:<metric_key>:<action>:<sha256(canonical_action_identity)>`

The canonical action identity is action-specific structured data (stable keys, sorted JSON, deterministic hash) so retries generate the same key while unrelated events do not collide.
