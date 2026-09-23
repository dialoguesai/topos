# Policy v2 owner coordination protocol

This beta tranche synchronizes owner-approved policies between the control plane
and a node. It returns signed coordination metadata only. The capability still
advertises `executable_forms: []`; neither natural-language evaluation nor fact,
lineage, message, graph or vector disclosure is enabled by this protocol.

## Trust and wire contract

`topos/permissions_v2/protocol.py` owns the strict shared wire models. Generated
schemas and `protocol-golden-v1.json` are under `fixtures/permissions_v2`.
The CP mirrors the protocol and uses a separate durable coordination store.
Every schema rejects unknown or missing fields; there is no legacy fallback.

Three independent Ed25519 domains use canonical JSON v1:

| Message | Signed domain | Purpose |
| --- | --- | --- |
| `SignedMutation` | `topos-policy-mutation/v2` | Activate or revoke one complete authority binding |
| `SignedStatusRequest` | `topos-policy-status-request/v2` | Read authenticated current state and an optional command receipt |
| `SignedAck` | `topos-policy-ack/v2` | Bind a node observation to the exact signed request |

The signer key, CP issuer, node audience and authorized frontend client come
from private configuration. Mutation authorization binds the owner and approved
frontend client, independently of the grantee actor/client tuple. The full
environment, node, resource, owner, grant, assignment, policy hash, generations,
capability, protection revision and epoch remain correlated. A grantee envelope
cannot mutate policy. Future issuance, expired envelopes and lifetimes above
120 seconds reject; no clock skew allowance is applied.

A command's semantic digest excludes attempt-specific signing key, timestamps
and signature, so a lost ACK can be recovered with a fresh signed retry of the
same command. Reusing its ID for a different semantic command rejects. ACK
`response_to` hashes the entire signed request, preventing response substitution
between attempts. ACK verification reparses even preconstructed models.

## Durable transitions and cancellation

The private SQLite ledger applies compare-and-set mutations under the process
write gate and an immediate transaction. The expected epoch must match and the
resulting authority must use the next epoch. Both grant and assignment
generations advance together. Policy version IDs and grant/assignment bindings
are immutable. Command receipts survive restart.

Revocation can install a tombstone before an initial activation arrives. A later
activation cannot resurrect the canceled generation. If cancellation races an
already applied activation, the CP reconciles signed current state and sends a
new revocation command at the current epoch. It must retain cancellation intent
through delayed activation ACKs. An exact retry returns its historic receipt
alongside current node state; an old activation receipt is never current
authorization after revocation.

Signed status supports lost-ACK recovery and restart reconciliation, including
absent grants and canceled policies that were never installed. Contradictory
ACK receipt/state chronology rejects. Every global epoch change invalidates all
older grantee snapshots. CP must refresh authority before future signing; there
is no stale-epoch exception.

## Actual Off-limits protection

First enrollment installs six SQLite triggers across `owner_only_records` and
`entity_blackholes`. Every insert, update or delete advances a canonical clock
in the same transaction as the protection change. Its random clock identity,
monotonic generation and current protection fingerprint form the revision.
Protecting and then lifting a record still changes the revision even when no
protocol request occurs between the two operations.

The ledger pins the observed clock identity and generation. Missing triggers,
lost clock state, identity replacement or generation rollback fail closed.
Runtime reopening never silently reinstalls a previously observed clock.
Restoring both canonical and ledger databases together remains outside this
local mechanism's detection ability; an external monotonic authority is needed.

Mutation, status, admission and checkpoint synchronize the actual protection
revision before claiming current state. Protection changes conservatively bump
the global node epoch. Relay workers acquire the write gate before reading the
clock, so waiting for a writer cannot extend a signed command's lifetime.
Future data adapters must still recheck protection and authority at their actual
transport release boundary. A coordination ACK is not a disclosure receipt.

## Disabled-by-default runtime

Enable only in an isolated beta environment with
`TOPOS_PERMISSIONS_V2_ENABLED=true` and an absolute
`TOPOS_PERMISSIONS_V2_CONFIG_PATH`. The strict
`topos-policy-node-config/v1` file contains:

```
version, identity, cp_issuer_id, frontend_client_id, trusted_cp_keys,
node_signing_kid, node_signing_key_path, canonical_database_path, ledger_path
```

Identity contains `environment_id`, `node_id`, `resource_id`, and `owner_id`.
The environment must begin with `permissions-beta-`; the canonical path must
equal the engine's active database, whose configured user must match the owner.
Public keys are 32-byte lowercase hex values. The private signing file contains
a 32-byte Ed25519 seed encoded as 64 hex characters. Never use fixture keys.

The ledger and signing key must reside directly inside the canonical database's
`permissions-v2` sibling directory. The directory is private (0700), config and
key files are private (0600), and a lifetime file lock permits one runtime.
Multiworker settings reject. This is a single-process SQLite beta configuration,
not a hosted or clustered deployment design. Key/config changes require restart.

The owner-only relay messages are `permissions_v2_mutate` and
`permissions_v2_status`, each with exactly `{"envelope": ...}` as payload.
Successful responses contain `{"status":"ok","payload":{"ack":...}}`.
The existing verified owner UDS or signed owner relay principal is required in
addition to the v2 signature. TCP/shared-key callers cannot gain owner authority.
Disabled requests fail before configuration loading. Errors expose bounded
codes, never candidate bodies or private configuration.

## Verification and supported boundary

The completed engine gate passed **343 tests**: contract, protocol, runtime,
principal fabric, handler registry, and existing record/entity protection tests.
Tests use synthetic keys, private scratch databases, the live DB tripwire and
offline model settings. They exercise real signatures, strict cross-bindings,
expiry, signer removal/rotation, idempotent retries, tombstone ordering, lost
ACK/restart, stale epochs, protection races, unobserved protect/lift cycles,
clock loss/rollback and actual owner dispatcher authorization. The independent
JavaScript golden verifier checks canonical bytes and all three signature
domains. The missing-clock reopen regression failed before repair.

| Supported now | Still required before recipient data access |
| --- | --- |
| Strict signed owner policy mutation and authenticated reconciliation | Certified first-family evidence qualification and recursive leaf lineage |
| Durable epochs, command receipts, tombstones and replay rejection | Node-owned evaluator and exact output adapter |
| Actual record/entity protection clock with conservative invalidation | Final release checks across node and current CP cancellation state |
| Separate strict grantee envelope and non-executing decision hooks | Source-universe synchronization, NL A/B evaluation and four-recipient testing |

The earlier contract/ledger gates and this protocol gate overlap. These checks
certify this coordination boundary, not global permission-gap closure.
