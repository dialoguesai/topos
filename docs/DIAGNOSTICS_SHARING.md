# Voluntary node diagnostics sharing

## What this build does, and what it does not

The node can build a small diagnostic report and show it to its owner. **It cannot send it.** There is no
collector, no network transport, no credentials and no recipient policy in the node: `available` is false, and both
send doors answer `409 collection_unavailable` even after the owner has confirmed. The only thing that works is the
local preview.

What is here is the node's side of a future feature, built first so that the boundary is fixed before anything can
cross it:

- one serializer that decides what a report may hold (`topos/diagnostics/sharing.py`, `report_bytes`);
- the owner's preview of the exact bytes;
- the consent clock (a one-off send of a previewed report, or one hour of sharing, then off);
- the owner-only controls on the owner socket (`topos/api/diagnostics.py`).

Sharing is off unless the owner turns it on. No setting, environment variable, OpenTelemetry option, hosting
template or request field turns it on, and nothing is remembered across a restart. The disclosure ruling is DR-73 in
`contracts/DISCLOSURE_RULES.md`.

This is separate from the two things the node already does:

1. **Registration, heartbeat and device information** sent to the control plane (see the inventory below). Unchanged.
2. **The owner's own OpenTelemetry export**, to a destination the owner picks (`docs/OBSERVABILITY.md`). Unchanged,
   and not read: no span, attribute, log record, baggage, endpoint, header or sampling decision feeds a report.

## The report

One schema, built from fresh dictionaries with constant keys:

```json
{
  "schema_version": 1,
  "measurements": {
    "http_requests": 0,
    "http_server_errors": 0,
    "http_duration_ms": 0
  },
  "system": {
    "os_family": "Linux",
    "architecture": "x86_64",
    "cpu_count": 8
  }
}
```

- `measurements` are three totals since the node process started, kept in memory only, counted by a small ASGI
  middleware (`DiagnosticsMeasurements`) over every HTTP request the node answers, the diagnostics calls included.
  They are not per-route. `http_server_errors` counts 5xx responses and unhandled request exceptions; no exception
  name, message or stack is kept. Counters stop at 1 billion and the duration at 1 trillion ms.
- `os_family` is `Darwin`, `Linux`, `Windows` or `other`. `architecture` is `arm64`, `aarch64`, `x86_64`, `AMD64` or
  `other`. `cpu_count` is left out when unknown or outside 1..4096.
- A value that is not a finite number in range, a boolean, or a word outside the closed lists is dropped (or
  becomes `other`), never copied.

The report bytes hold no message, document, prompt or answer text, no name, no user or account identifier, no
credential, no path, URL, hostname or computer name, no model name, no request header and no free text.

**The transaction would still not be anonymous.** A recipient necessarily sees network metadata and, once reports
are authenticated, which account and node sent them. Any wording shown to an owner has to say so.

## Owner controls

All five routes are refused unless the request arrives on the owner socket (the 0600 Unix socket) from the owner's
own app. A TCP request is refused whatever key or header it carries. An enrolled coding agent or app is refused on
every route, the status read included; the route checks this itself, so it holds if the socket's agent allowlist or
door split changes. There is no legacy-key, MCP or relay way in. The owner of a hosted node would need a separately
verified remote channel to that node before these controls could be offered there; none exists, and hosting a node
gives nobody consent on its owner's behalf.

| Method and path | What it does | Owner-door census |
|---|---|---|
| `GET /diagnostics/dialogues` | availability, whether sharing is on, seconds left, the policy (none in this build), the label and description | read |
| `POST /diagnostics/dialogues/preview` | builds the report now; returns its exact UTF-8 body, its SHA-256 and a five-minute expiry | `open`: sends nothing |
| `POST /diagnostics/dialogues/send` | body `{"sha256", "policy_revision"}`; sends the stored preview once | `dangerous`: needs the Topos app or a confirmation the owner just gave |
| `POST /diagnostics/dialogues/share-one-hour` | body `{"policy_revision"}`; one hour of sharing | `dangerous` |
| `DELETE /diagnostics/dialogues` | stops sharing, cancels an upload in flight, discards the preview | `open`: it can only reduce what leaves the node |

Unknown request fields are refused (422). To read the status on a development node:
`curl --unix-socket "$HOME/.topos/engine.sock" http://localhost/diagnostics/dialogues`.

### Preview, then send

A preview holds at most one report in process memory for five minutes. `send` names it by hash, and the bytes
handed to the transport are the preview's bytes: nothing is re-sampled or added. A preview is single use. The hash
binds bytes, not identity. `send` also names the policy revision the owner was shown; a different revision is
refused (`policy_changed`).

### One hour

Timed sharing lasts at most one hour on a monotonic clock. A fresh report is built every 30 seconds, one upload at a
time, with a 10-second limit. There is no retry, queue, outbox or Offline Inbox use. A failed upload ends the hour.
Stopping, expiry and failure cancel the timer, the session and any upload in flight, and discard the preview. A
restart is off. Turning it on again starts a new hour. Continuous sharing is not supported.

During the hour the reports are new snapshots of the same six fields, so they are not the bytes of an earlier
preview. Whether that is acceptable, or the hour should be dropped in favour of one-off sends only, is an open
question (below).

## Before a recipient is added

A recipient is added in code, by passing a sender and a `RecipientPolicy` to the node's `Sharing` instance together.
Neither can come from the environment, a setting or a request. `tests/diagnostics/test_sharing.py` fails on that
change on purpose (the instance has a sender; a network import appeared; something else builds a `Sharing`), so the
same change has to update those pins and DR-73.

A transport must honor task cancellation and must not keep a private queue, retry, follow redirects, add fields or
flush at shutdown. Bytes a server already accepted cannot be recalled by cancelling.

None of the following exists today. All of it is required before `available` may be true:

1. **Consent the recipient can check.** Authenticate the specific node and its current account binding. The shared
   engine key alone is not evidence of a person's consent. Use a short-lived capability bound to a separately
   authenticated owner decision on that node, or consent proofs signed by an independently enrolled node key. A body
   field saying consent was given, a hash, an email address or an OpenTelemetry flag is not authority.
2. **Ingress.** Refuse missing, expired or revoked grants, the wrong account, node or audience, replayed report ids,
   unknown fields, wrong types, non-finite values and oversized bodies before storage. Bound the size before parsing,
   rate-limit, authenticate before expensive work, and keep request bodies and raw errors out of logs.
3. **Retention.** Private storage with a server-set receipt time and expiry; expired reports hidden from queries
   before a purge runs; the same rule for replicas, exports, queues, access logs and backups.
4. **Readers.** Named reader accounts resolved to stable identity-provider subjects by an administrative enrolment,
   authorized on verified issuer and subject with revocation checks. Never a request-supplied email or a broad
   admin key. No personal email allowlist is committed to this repository.
5. **Owner deletion.** Authorized against the stored account and node linkage, erasing at once and from retained
   copies, with a receipt that holds no report content. Stopping sharing and deleting received reports are separate
   actions, and stopping does not delete what was already received.
6. **Integration.** Verified issuer, host and TLS; no redirects; no payload logging; in-flight requests cancelled on
   stop and expiry; never the Offline Inbox; never the general OpenTelemetry, logging or fleet pipeline. The whole
   node-to-store path tested locally before any live destination is configured.

### Intent recorded so far, not enforced by anything

The thread that designed this recorded, on 1 Nov 2026, the intent that received reports be kept 30 days from
receipt, that an owner can delete them sooner, and that only two named Dialogues reader accounts may read them.
Those are preferences for the recipient to implement. They are not in the node's code and the node shows no such
promise to an owner, because nothing enforces one yet.

Companion work on the settings page and a control-plane contract that answers "unavailable" exists on unmerged
local branches. It is not part of this change and nothing is deployed.

## Open questions for the owner

1. Who receives a report, over what endpoint and with what proof of consent (items 1 to 6 above).
2. Whether the three `system` fields belong in the report at all, or only the three totals.
3. Whether the one-hour mode stays, given that its reports are not individually previewed.
4. Whether the settings page should show this at all while collection is unavailable.

## Existing operational metadata (not this feature)

Read at node main `ce1c165e` on 12 Nov 2026. This describes current code; this change does not alter it.

- `topos/engine/registration.py` sends, on registration and heartbeat: connection status, transport mode, the
  capabilities object, engine mode and whether the LLM is enabled; heartbeats add resident memory and local-model
  cache counters.
- `topos/core/state.py` `get_system_info` holds hostname, the macOS computer name, OS and version, platform string,
  architecture, CPU count, memory and root-disk totals and the Python version. The control plane requests device
  information when a node connects and stores the system dictionary with the node's runtime record.

So the control plane already receives operational metadata, including a hostname, without this feature. A sentence
such as "only minimal health is collected unless you opt in" would not be accurate. Reducing that metadata is
separate work and is not bundled here. The diagnostic report reuses none of it: it reads the platform module
directly and passes every value through the closed lists above.

## Verification

`tests/diagnostics/test_sharing.py`, with synthetic data and in-process sender functions only. It covers: the
node's instance cannot send; the package imports no network, file or node module and reads no environment variable
or setting; what a report can hold; exact preview bytes, single use and expiry; policy binding; stop and expiry
during an upload; no retries; concurrent starts; the socket, principal and confirmation checks; and the real app
through the socket wrapper. No live collector, model, account or database is used.

Rollback is removing the router, the middleware line and the package. There is no schema migration and nothing is
persisted.

## Provenance

Lifted on 12 Nov 2026 from an uncommitted working tree (branch `codex/node-diagnostics-sharing`, based on node
1.5.35) onto current main. Compared with that tree: the node's instance no longer carries a recipient policy, the
description names the six fields exactly, and the tests that pin "this build cannot send" were added.
