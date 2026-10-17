# Calibrate truth pack milestone

Calibrate is the owner's human-in-the-loop labeling surface. The first task is a five-question Icebreakers pack, shown one question at a time from the primary sidebar. Answers save immediately, can be skipped, revised, or cleared, and support a private verification preview. The same labels produce signed verdicts for recipient grants and owner-app grants.

## Game API

Use the existing recipient OAuth token and named share:

```http
POST /v1/sharing/shares/{share}/verify-claim
Authorization: Bearer <recipient token>
Content-Type: application/json

{"pack_id":"icebreakers-v1","question_id":"drink","value":"tea"}
```

```json
{"operation":"verify_claim","view_id":"calibration.truth.v1","verdict":"supports","basis":"owner_answer"}
```

`supports` means the claim matches the current owner answer. `contradicts` means a different choice was explicitly selected for that single-choice question. Missing, cleared, rejected, or protected evidence gives `unknown` with `basis: insufficient_evidence`. These are statements about the owner's labels, not an objective truth score. The endpoint returns no answer list, source text, confidence score, or alternate answer.

| Question ID | Accepted values |
| --- | --- |
| `drink` | `coffee`, `tea`, `neither` |
| `time` | `morning`, `evening`, `it-varies` |
| `weekend` | `outdoors`, `at-home`, `a-bit-of-both` |
| `planning` | `plan-ahead`, `be-spontaneous`, `a-bit-of-both` |
| `company` | `alone`, `with-people`, `a-bit-of-both` |

The first milestone accepts this structured claim grammar. It does not parse free-form sentences, negation, compound claims, or claims about another person. The legacy free-form truth routes remain separate and exclude these calibration predicates.

## Consent and release

The existing grant editor and owner-app policy editor offer **Truth-telling games** when the connected node advertises `permissions_v2_truth_version >= 1`. Selecting Icebreakers adds `truth_packs: ["icebreakers-v1"]` to the reviewed draft and signed knowledge policy's `search` declaration. Consent covers current answers and later answers or edits in that versioned pack. Answering a question alone grants nothing. A new pack requires new consent.

This is an optional extension of the existing knowledge policy, alongside its source-data permissions. It is independent of source categories and lookback settings; the review explicitly describes the additional pack access. It does not automatically replace or narrow any source-data permissions on that grant. This milestone does not add a separate truth-only grant profile.

Absent fields retain their previous serialized policy bytes. Older nodes reject the new field; capability discovery prevents offering it to them. Owner-app grants use the existing self-policy binding (`actor = owner`, `client = app`), not unrestricted owner credentials. Both grant types use the same signed request and release path.

The control plane reuses recipient authentication, named-share resolution, native consent, read limits, bulkheads, signed envelopes, and final forwarding. The node verifies the exact actor/client/grant tuple, consumes the request ID, checks explicit pack consent, writes a private receipt, and signs the verdict. It rechecks the grant, protection state, and answer revision before transport send. The control plane rechecks revocation and consent before forwarding. A search response cannot substitute for a truth response or vice versa.

## Storage and owner API

Owner routes use the existing signal infrastructure:

- `GET /v1/signal/calibration`: pack, questions, saved answers and revisions.
- `PUT /v1/signal/calibration/answer`: `pack_id`, `question_id`, `value` (or `null` to clear), and `expected_revision`.
- `POST /v1/signal/calibration/verify-claim`: private owner preview, with the game request grammar.

The hosted UI reaches the equivalent `/v1/topos/{topos_id}/signal/...` proxy routes. Owner-only stamped handlers reject recipient and owner-app grantee credentials. The API returns 409 for concurrent edits; the UI reloads instead of silently overwriting them.

`calibration_answers` is created by registered migration 87 in the node's existing database. The answer writer reuses its idempotent DDL for legacy databases. Its primary key `(pack_id, question_id)` provides the current-answer index. Each answer also goes through `FactStore` with owner attribution, a distinct single-valued `calibration_<question>` predicate, and a calibration source reference. Fact revisions and index changes commit together using the existing write gate. Clearing closes the current fact. Verification checks that the linked fact is still current and unchanged and respects exclusions and record/entity protections.

No LLM, embeddings, recent-fact scan, or background extraction is required to check a claim. Broader ingestion-derived facts and beliefs are future adapters; this milestone only verifies explicitly labeled pack answers. The pack's `kind` and version leave room for other calibration task types without changing the product entry point.

## Rollout and validation

Ship the node, then the control plane, then the web app. Use the existing permissions v2 sharing and message-search release switches. A node advertises truth support only while its message-search switch is enabled. The editor withholds the pack option until the connected node advertises support. Existing grants require an explicit owner review to add the pack.

Automated coverage uses real SQLite labels and the existing signing/ledger infrastructure. It checks recipient and owner-app bindings, edits/clear/restart, optimistic concurrency, missing consent, actor and payload tampering, expiry, replay, revocation, stale responses, protected facts, legacy-route isolation, indexed lookup beyond 500 unrelated facts, owner-only writes, and UI save/skip/error/preview flows. Control-plane tests use real JWT validation and signed synthetic node responses; no live production account or deployment is part of these checks.

A local synthetic benchmark of 100 signed node verifications measured 18.39 ms median and 29.98 ms p95. This includes SQLite, signature verification, receipt persistence, and node signing; it excludes network latency and control-plane OAuth work. It is a development measurement, not an end-to-end service guarantee.

Database schema: registered migration 87 (`calibration_answers_v1`) creates the current-answer index on upgrade. The answer writer reuses the same idempotent DDL for legacy databases that have not run the migration ledger. Existing rows and enrichment versions are unchanged.
