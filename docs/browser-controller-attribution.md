# Browser controller provenance

Browser capture actor identity is independent of the authenticated ingestion
writer. `browser_actor` in activity metadata carries kind, declaration/detection
method, provider, name, session ID, source-scoped parent conversation ID and
optional turn ID. Raw browser tables retain the envelope and queryable actor and
parent columns. Stable capture IDs make retries idempotent. Missing metadata on
an internal replay or duplicate cannot erase an existing actor.

An unknown/legacy controller is never silently a human. Provider and parent are
cooperative declarations, not a new principal or an access grant. Existing
writer-class ownership protections still apply. Parents may sync after events;
source + conversation ID is an exact reference, not a guessed timestamp match.

Owner timeline reads return the actor and accept `actor_kind=human|ai|unknown`.
AI includes identified agents and unidentified automation. Filtering precedes
counts and pagination. Owner recall and vector results retain controller labels;
full provider and conversation references are not added to lower disclosure tiers.

Personal enrichment jobs exclude AI, automation and unknown browser rows in both
canonical and signal lanes. Recall embeddings, privacy redaction and timeline
projection remain available. Clustering excludes nonhuman browser vectors, and
monthly interests require explicit human actors. Mixed existing clusters with
unproven browser members cannot release their label until rebuilt from eligible
human evidence. Existing previously derived facts are not automatically deleted.

Roll out the node, the control-plane timeline relay, the frontend, then the
browser extension. The extension alone cannot enforce this policy on an old
node. Historical unattributed rows remain unknown; do not backfill them as human.

Validation uses synthetic records and temporary databases: actor validation,
writer integrity, raw/canonical/reload retention, duplicate/replay behavior,
parent references, recall, enrichment selection, interest rejection, and
filtered timeline counts/pagination. No production node mutation is required.
