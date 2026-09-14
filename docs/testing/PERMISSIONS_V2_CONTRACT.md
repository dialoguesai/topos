# Policy v2 boundary contract — P2a

This package is isolated from legacy routes. No existing HTTP, relay, MCP, query,
or disclosure handler imports it. The capability document has one **registered**
form, `canonical_record / read / canonical.message_disclosure.v1`, and an empty
`executable_forms` list. The closed message schema supports only
`conversation_messages` and `ai_chat_messages`. Registration is syntax support,
not a claim that lineage, predicates, or Off-limits have been certified.

## Wire contract

The engine owns the models in `topos/permissions_v2/contract.py` and
`signing.py`; generated closed JSON Schemas and a deterministic Ed25519 fixture
are in `fixtures/permissions_v2`. Unknown versions, fields and missing required
fields reject. There is no v2-to-legacy fallback. `natural_language` must be
explicitly null; semantic evaluators, facts, graphs and vectors are unsupported.

Each immutable policy binds environment, node, resource, owner, actor, client,
grant, and assignment. It pins vocabulary, capability, validity and a source
universe. `only: []` is empty; `all` references an exact universe revision with
growth requiring consent. Each rule keeps its evidence selection/predicate and
output predicate/forms together. Empty rule/form lists are valid deny-all
selections. Boolean predicates use three-valued logic: missing classification
remains unknown under negation. This helper is not yet a query classifier.

The signed envelope requires exactly:

```
version, kid, environment_id, node_id, resource_id, owner_id, actor_id,
client_id, grant_id, grant_generation, assignment_id, assignment_generation,
policy_version_id, policy_hash, capability_version, protection_revision,
node_epoch, request_id, request_type, request_hash, issued_at, expires_at,
signature
```

`version` is `topos-grantee-envelope/v2`. Request types are exactly
`permissions.v2.preview` or `permissions.v2.read`; neither is mounted yet.
The pinned key map comes from trusted node configuration, never the envelope.
Ed25519 signs `topos-grantee-envelope/v2\n` followed by canonical JSON of every
envelope field except `signature`. Signature encoding is canonical unpadded
base64url. The request hash is SHA-256 of canonical
`{"request_type": type, "payload": actual_payload}`. The verifier compares
actual authenticated request context and current persisted authority, not just
claims supplied by the requester. Future issuance times reject with no skew;
expiry is exclusive and lifetime is at most 120 seconds.

Topos canonical JSON v1 uses ASCII object keys sorted lexically, compact JSON,
ASCII escaping of verbatim Unicode string values (lowercase UTF-16 `\u` escapes),
and integers within ±(2^53−1). It rejects floats, exponent-form numbers, duplicate
JSON keys, non-ASCII keys, unpaired surrogates, oversized data and excessive
nesting. It performs no Unicode or policy normalization, and array order remains
significant. This is a deliberately narrow custom contract, **not RFC 8785**.
JavaScript must serialize sorted object entries directly: `JSON.stringify` on a
reconstructed object reorders numeric keys. `JSON.parse` alone is not a strict
decoder because it discards duplicate keys. The independent Node verifier
demonstrates the bytes/hash/signature contract, not a complete production parser.

## Node-local state transitions

The ledger requires an explicit private SQLite file and fixed node identity.
It creates no default application database connection. All mutations use the
existing process write gate plus `BEGIN IMMEDIATE`; the node identity and
protection revision must match when reopening. File permissions are 0600.

Only an already verified `OWNER_APP` principal may activate, revoke, update the
protection revision, or read signer coordination snapshots. A matching owner ID,
legacy shared key, routine class, or grantee signature cannot do so. A relay owner
must match the ledger owner ID; the verified owner socket (`uds`) may omit the
acting user, but an explicit different owner or any TCP channel rejects. The
current engine deliberately demotes owner keys on TCP; the local socket and
signed owner relay are the two owner channels. These are
in-process coordination hooks. A CP-approved synchronization protocol needs a
separately reviewed mutation signature domain; the grantee envelope cannot
activate itself. Local mutation results are not authenticated network ACKs.

P2a permits one immutable assignment identity per grant. First activation uses
grant/assignment generations 1/1; every update must advance both. Binding a
grant or assignment to a different actor/client fails. Policy version IDs are
immutable. Revocation increments both generations and retains the tombstone;
reactivation must advance past it. Every authority/protection mutation advances
the global node epoch using compare-and-set. An exact command retry is
idempotent; reusing its ID for different bytes rejects. Retry results describe
the originally applied event, not present authorization.

Admission verifies the signature, actual request binding, current policy,
generations, protection revision and epoch in the same transaction that consumes
the request ID. Replay IDs persist across restarts and are never silently reused
or pruned. A duplicate request is rejected, not executed again. Global epoch
changes conservatively invalidate **all** old request envelopes, including ones
for other grants; CP coordination must refresh current snapshots before signing.

The final `checkpoint_decision` hook rechecks policy validity, envelope expiry,
the signature against the current pinned keys, all current authority fields and
epoch. Removed/replaced keys invalidate admitted work. It binds the decision to its policy and
candidate revision, enforces the closed output shape and a single rule's
source/table tuple, then atomically stores a private receipt. Denied or
indeterminate decisions cannot include output. Candidate bodies and denied
samples are not stored or exported; receipts contain hashes and bounded decision
metadata. Schema failures suppress raw validation exception chains so traceback
logging cannot echo rejected candidate values. Receipts always say
`execution_enabled: false` and are not reusable
disclosure authorization tokens.

## Integration limits

This is contract/ledger scaffolding. The private receipt hook does **not** prove
evidence/output predicates, candidate lineage, source truth, or Off-limits
classification. It accepts a trusted node evaluator's decision for bookkeeping;
an untrusted request must never supply that decision. No evaluator or retrieval
adapter is installed, and no data is returned by the hook. Future adapters must
prove those obligations and check the current epoch immediately before their
actual transport release. A committed receipt does not authorize later replay
or release after revocation.

The protection revision is a ledger coordination input. The existing Off-limits
mutation and receipt/cache routes are not wired to it yet. Integration must
couple real protection changes and the epoch bump transactionally or with an
equally conservative invalidation protocol. CP user/client grant conjunction,
authenticated mutation/ACK transport, source-catalog synchronization, restart
rollback detection, trust-key rotation, receipt retention and hosted PostgreSQL
transactions remain separate work. The node cannot detect restoration of an old
database without a monotonic authority outside that restored database.

## Verification

The focused gate initially passed **128 tests**. The transport follow-up gate
passed **168**, including 130 P2a tests plus 38 existing principal-fabric invariants, with the live
DB tripwire clean. These counts overlap.
Independent review reproduced the owner-binding and removed-signer gaps before
repair; a separate canary traceback test failed before exception suppression and
passed afterward. Two UDS owner controls failed before aligning the local owner
hook with the actual socket transport and passed afterward. These are package-level boundary checks, not full-route or
lineage certification.

Run `tests/permissions_v2` through the live DB tripwire with the scratch/offline
environment described in `PERMISSIONS_BETA_FOUNDATION.md`. The tests use real
Ed25519 signatures and private SQLite files, cover all envelope fields,
cross-bindings, malformed JSON, stale policies/epochs, expiration, replay,
concurrent admission, revocation between admission and final checkpoint, closed
output fields, explicit empty sets and cross-rule source/view mixing.

Run `node fixtures/permissions_v2/verify-golden.mjs` for independent
Python/JavaScript canonical hash and signature compatibility. Synthetic fixture
key material is public test data and must never be installed as a trusted key.
