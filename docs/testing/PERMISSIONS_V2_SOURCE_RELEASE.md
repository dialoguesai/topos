# First bounded source-message release

This adapter completes a narrow data path through the existing P2a grammar. A
request identifies one fact with `{"query":"fact:<fact-id>"}`; its output is the
complete set of terminal canonical source messages for that fact. It does not
return a fact projection, generated answer, summary, graph, vector or model
response. There is no search, SQL, fallback query backend, or partial redaction.

The default remains disabled. The dedicated relay requires
`TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED=true`, the paired Policy v2 runtime,
and an existing private owner evidence-review store. It never creates that
store as a recipient. The CP has its own independent release switch and final
forwarding gate. Both must be deliberately configured in the isolated beta.
The historical `capability_document()` remains a metadata registry; its empty
executable list is not a claim that an operator enabled the new dedicated route.

## Authorization and vocabulary

The actual relay door verifies the existing CP principal stamp as `third_party`
and binds its actor and authorized client to the separately signed Policy v2
request. Owner-mode, legacy relay deferral, generic local HTTP, forged headers
and unsigned payload identities cannot enter this path. The complete grant
binding is an owner-approved actor/client conjunction; scopes from different
grants or clauses are never combined.

Policies must explicitly name `owner-review-vocabulary/v1`. It maps each current
reviewed record to `domain` (its exact owner-entered domain identifiers),
`actor_role=["authored"]`, `subject=["owner"]`, and the reviewed `sensitivity`.
Native author and subject validation independently checks those two fixed
values. The vocabulary is local owner-reviewed classification, not a certified
automatic classification of all ingested data. Another vocabulary is withheld,
even if it happens to use the same strings.

The trusted resolver loads the current review and recursively resolves every
fact and source. Existing `owner_only` disclosure, selected record protection,
entity-protection uncertainty, stale review, ambiguous identity, unknown
classification, and incomplete lineage all withhold before serialization.
Ordinary recipient input cannot provide classifications or a qualification.

One complete permit clause must cover **every** terminal source and table, allow
the local processor, have a `raw` ceiling and explicitly select
`canonical.message_disclosure.v1`. Its evidence predicate must match every
recursive fact and source; its output predicate must match every returned source.
Summary/inference ceilings and explicit empty selections cannot release raw
messages. Any applicable exclusion wins, including an unresolved exclusion.
When a deny source overlaps, evidence exclusions conservatively apply to the
whole contributing closure. This may withhold more; it never recomputes an answer
from a selected subset. Existing source/table selectors do not express separate
dataset grants, although full dataset identity remains mandatory for lineage.

The output has only the registered `MessageDisclosure` fields. It is bounded
to 100 records and 256,000 canonical JSON bytes, with each content string bounded
by the existing schema. The fact locator, classifications and private receipts
are not added to the recipient output. Failure responses reveal no distinction
between missing, protected, unreviewed or disallowed facts.

## Final checks and delivery

The node synchronizes its actual canonical protection clock before admitting a
request and checks the current policy, evidence, review, clock, key, and expiry
again at dispatch. A durable one-shot checkpoint precedes the attempted send.
Crashes, cancellation, timeout and uncertain sends require a fresh CP issuance;
the same request can never replay into a second disclosure.

`ControlPlaneClient` invokes the dedicated relay adapter at its actual socket.
The adapter holds the shared node write gate, canonical read transaction and
private review transaction until the bounded `ws.send` finishes. The worker
waits for that send on the socket's event loop. No generic response return,
outbox or reconnect queue can send these contents later. A generic invocation
of the registered handler always denies.

The signed node result uses the independent `topos-node-disclosure/v1` domain
and binds the exact request, envelope, authority, output hash and expiry. This
proof is for the trusted CP; a signature alone never authorizes forwarding.
The CP must validate it against its durable issuance and current cancellation
state while dispatching the actual recipient response.

Node revocation/protection writes serialized before its transport dispatch win.
CP cancellation committed before its final recipient dispatch gate wins. Neither
service can retract bytes whose send has already started. This is an explicit
pair of transport linearization points, not a claim of instantaneous global
revocation after data has left a trusted service. Arbitrary external WAL writers
or privileged modification of code and all durable state are outside the
single-process beta transaction guarantee.

## Remaining capabilities

This path uses the output form already registered in P2a; it does not silently
widen an old policy to new forms. The first fact projection, direct-language
serving evaluator, automated classification, complete entity/copy lineage,
scrubbed copied-corpus deployment and four-client A/B campaign remain separate
gates. Existing owner-only corpus facts stay owner-only.
