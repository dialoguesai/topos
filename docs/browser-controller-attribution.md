# Browser controller provenance

Browser capture actor identity is independent of the authenticated ingestion
writer. `browser_actor` in activity metadata carries kind, declaration/detection
method, provider, name, session ID, source-scoped parent conversation ID and
optional turn ID. Raw browser tables retain the envelope and queryable actor and
parent columns. Stable capture IDs make retries idempotent. Missing metadata on
an internal replay or duplicate cannot erase an existing actor.

Ordinary browsing defaults to human-controlled (`method: default_human`), including
legacy rows without an actor or with an empty unknown/unresolved envelope. This
is the owner-selected product default (26 Oct 2026), not detection evidence.
Positive automation signals, incomplete agent identity, and named agents do not
receive that default. Provider and parent are
cooperative declarations, not a new principal or an access grant. Existing
writer-class ownership protections still apply. Parents may sync after events;
source + conversation ID is an exact reference, not a guessed timestamp match.

Owner timeline reads return the actor and accept `actor_kind=human|ai|unknown`.
AI includes identified agents and unidentified automation. Filtering precedes
counts and pagination. Owner recall and vector results retain controller labels;
full provider and conversation references are not added to lower disclosure tiers.

Personal enrichment jobs exclude AI, automation and unresolved browser rows carrying evidence in both
canonical and signal lanes. Recall embeddings, privacy redaction and timeline
projection remain available. Clustering excludes nonhuman browser vectors, and
monthly interests include explicit human actors and ordinary default-human activity. Mixed existing clusters with
unproven browser members cannot release their label until rebuilt from eligible
human evidence. Existing previously derived facts are not automatically deleted.

Roll out the node, the control-plane timeline relay, the frontend, then the
browser extension. The extension alone cannot enforce this policy on an old
node. Historical ordinary rows use the default during reads; stored metadata is
not backfilled. Replaying a default-human or unmarked record cannot overwrite a
stored agent identity. The Human filter uses the same normalization before counts
and pagination, so old and new ordinary activity agree.

Validation uses synthetic records and temporary databases: actor validation,
writer integrity, raw/canonical/reload retention, duplicate/replay behavior,
parent references, recall, enrichment selection, interest rejection, and
filtered timeline counts/pagination. No production node mutation is required.
