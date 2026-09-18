# p2c-v1: permitted-set message search

Plan and review: `audits/2026-09-14-permissions/P2C_V1_IMPLEMENTATION_PLAN.md` (control-plane tree), approved 18 Sep 2026 with six conditions. Decision D21 of `SCALABLE_GRANTS_DESIGN.md`.

## The invariant

Discovery is a subset of access. A record appears in a search result only if it is a terminal message of a reviewed, scoped fact, and that fact's p2a decision under the same grant is `permit` at that moment. The decision is computed by the locator door's own qualification (`EvidenceResolver._qualified_bundle`: floors first, then the review, then `_eligible`) and its own decision function (`release.source_message_decision`, whose body is unchanged; p2c-v1 is one more entry in `SOURCE_DECISIONS`). The check runs for every returned record, in one canonical read under the node write gate, immediately before the set checkpoint. The index only chooses the order in which candidates are offered to that check. A defect in the index can cost availability; it cannot release a record the check refuses. Search also refuses a permitted fact whose whole locator disclosure could not be built (more than 100 leaves, more than 256,000 bytes, a leaf that is not text), exactly as the locator door does, so search never returns a message that door would refuse.

## Grant

The capability literal is `permissions-beta/p2c-v1`. The grammar is p2a-v2's: the owner-attested subject rule and the same rules, plus a `search` declaration: tables, `max_permitted_records` (≤ 5,000), `max_k` (≤ 25) and a rolling event window. Ceiling is `raw` only, rejected at parse otherwise, because p2a's decision permits only raw. No p2a or p2b grant can gain search. The registry dispatches on the literal, and adding a `search` key to another document fails `extra="forbid"`.

## Request and view

- Request: `{query ≤ 8,000 chars, k 1..25, window?: {after, before} (UTC seconds)}`. The form hashed into the envelope is `signed_payload` (an absent window is omitted). Request type `permissions.v2.search`.
- `k` above the grant's `max_k`, or a window not inside the grant's rolling window, is the uniform refusal.
- View `canonical.message_search.v1`: an ordered list, at most `k` long and at most 256,000 bytes, of `{record_id, source_id, canonical_table, event_at, content}`. There are no scores, counts or reasons.
- `record_id` is `r.` + HMAC-SHA256 under a per-grant key (`opaque_ids.py`). The key rotates when the grant is revoked.

## The index (`search_index.py`)

R(g) is the part of P(g) that search could ever release:
- P(g) is the terminal messages of reviewed facts that p2a permits under g.
- R(g) drops NSFW-flagged records, undated records, and records already older than the rolling window.

The index is built owner-side only, from the handler hooks after a grant mutation and after an evidence review is recorded or revoked, or by `permissions_v2_message_search_rebuild`. A recipient request never builds it. What the index holds:
- Per member: the opaque id, the event time, a term bag and chunk vectors. The term bags and vectors are derivatives of the content (a bag of words is most of a message), so the file is treated as content at rest.
- The member's identity and witness fact ids, sealed with AES-GCM under a key derived from the grant key, which lives in a separate file.
- Nothing else. There is no raw content, no sender field and no row id.

Files are 0600 inside a 0700 directory, use `journal_mode=DELETE`, and are published by atomic rename.

The index is a scrub surface. A file is zero-overwritten and unlinked when any of these happens:
- the protection clock moves (black hole, tombstone, owner-only mark);
- a member's row is deleted or scrubbed;
- the grant is revoked or expires, or its policy or authority changes;
- a member's row or any of its witness facts changes after the build (edited, re-flagged, superseded, deleted). Each member carries a sealed fingerprint of those rows, so a stale index is dropped rather than ranking from outdated statistics. Search refuses until the owner's next rebuild.

That deletion comes from three places:
- `purge_for_database`, called by `BlackholeStore.blackhole_entity` / `unblackhole_entity` and by `scrub_source`;
- `sweep` over every grant, in every owner hook and on a 10 s daemon timer; a request checks only its own grant's file (`check_own`, O(|R(g)|) keyed lookups), so other grants' sizes never enter its time, and a request never deletes another grant's index;
- the request path, which refuses a missing, stale or over-cap index.

**Merge gate.** The build holds the node write gate for O(reviewed facts). Before any run on a copy of the owner's database, it must build on a read snapshot outside the gate and take the gate only to publish.

## Ranking (`search_lanes.py`)

This is a closed allowlist, and no owner lane or cache is imported. The lanes are BM25 and exact cosine, fused with RRF (k = 60). Ties break on event time, then on the opaque id. Every statistic is computed over the members of R(g) inside the request's effective window. The node-wide FTS5 index is never used, because its bm25 IDF and average length come from the whole table, so hidden text would move the order of permitted records. The twin-corpus test caught exactly that when window-excluded P members were still in the statistics. The query embedding never goes through the process-wide query-embedding cache. Rerank is off in v1.

## Order of work in a request (`search_release.py`, `search_transport.py`)

1. Admit under the gate, as p2a does.
2. Without the gate: the grant bounds, the own-grant index check, loading the index, embedding the query, ranking.
3. Under the gate, in one read: the authority and floor re-checked; each candidate's witness fact re-qualified and re-decided; window, NSFW and size checked on the same row; one `SearchSetDecision`; `checkpoint_set_decision` with receipt `topos-local-receipt/v3`.
4. Sign.
5. The transport sends with every gate released. The linearization point is the checkpoint (design §7 R12).

Every failure leaves the node as the one error frame.

## Known residuals

- Output semantics are "R(g) as of the last owner-side build, re-checked live". Any change to a member after the build drops the index, so there is no silent reordering. But search is unavailable until the owner's next review or grant sync. A node-internal rebuild trigger needs a principal decision; one is not built.
- `event_at` is returned at one-second precision. The locator view returns no time, so for that one field search discloses more than the locator door does. It is inherent in request windows.
- `request_hash` and the MCP `arguments_hash` are unsalted SHA-256 of the query. Anyone who can read the CP database or the node ledger can confirm a guessed query. Plaintext is never stored.
- The daemon sweep holds the write gate for O(sum of members over grants) every 10 s.
- The rebuild's gate hold is visible to concurrent requests as timing, which reveals that the owner acted.
- The re-check stage reuses p2a's per-read scans: the sibling GLOB (R2) and the copy count (R1). Its time therefore grows with node size until the bookkeeping stream lands. `scripts/permissions_v2/p2c_timing_twins.py` reports it apart from the gated discovery stages.
- The embedder's warm or cold state is observable.
- A black hole anywhere empties P under the global D8 floor, so search answers `[]` after the owner re-syncs.
