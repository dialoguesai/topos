# Changelog

All notable changes to `topos-node` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); lane tags
follow `RELEASING.md` (`[S1]`, `[E:…]`, `[D]`, `[O]`, …).

The machine-readable twin of each release is
`topos/upgrades/manifests.json`.

## [Unreleased]

### Added
- **[S1] Goals-first milestone integrity**: `asp.milestone` must attach (fuzzy,
  generic-token-stopped) to a stored active goal or it quarantines — the measured
  failure prompt rules couldn't fix twice, fixed structurally. Unattached-but-valid
  milestones stay reviewable, never lost.
- **[S1] Tier-2 `exclusive_with`**: pack-declared value-sets that cannot both hold for
  one identity; RECENT contradictions queue for review (a same-week partner/ex_partner
  flip-flop) while life changes still supersede. relationships pack 0.2.5 declares the
  first set.
- **[O] High-harm 3-vote judging at ingest** (relationship roles partner/child/sibling/
  spouse; events loss/conflict/reconnected) — the measured single-vote variance fix.
- **[O] Drip catch-up**: every ingest batch also processes 25 unprocessed history
  pairs; dark deltas self-heal without a scheduler.
- **[O] Owner backfill control**: `run_pack_backfill` + POST
  /derivation/packs/{id}/backfill — bounded history runs from the lens catalog.
- **[O] Trajectory synthesizer** (work.venture_history / professional_visibility from
  accumulated events+projects; accumulation anchors to its newest evidence).
- **[O] WA.E report card as code** (`report_card.py`): live acceptance / reroute /
  owner-upheld scores per pack from the training ledger.
- **[O] Semantic pack router (R0-M2), shipped FLAG-OFF on measured evidence**: lexical
  recall already 86/86 on fact-yielding records; hybrid awaits substrate that outruns
  pack vocabulary (TOPOS_DERIVATION_ROUTER=hybrid).
- **[O] `TOPOS_FACTS_LANE_WEIGHT`**: the facts-lane fusion weight is a measured,
  configurable choice — live sweep confirmed 1.5 (16/37 presence · 81 items) over
  1.0 (71 items) and 0.6 (lane collapse), closing BP1.


## [1.3.28] — 2026-08-26

### Added
- **[S1] Training-data factory** (migration 66): every judged assertion — accepted or
  rejected — lands in `derivation_training_ledger` with quotes, verdicts and reasons;
  rejects are the hard negatives a future classifier distillation trains on. Daily
  per-pack yield counters (`pack_yield`) power cost visibility and the nudges below.
- **[S1] Per-node self-gating with offers** (`pack_offers`): a disabled lens whose
  prefilter keeps matching gets a capped shadow trial (no writes); passing mints an
  enable offer with example facts. An enabled lens burning ≥50 calls/30d with zero
  yield gets a disable nudge — keyed on CALLS SPENT, so low-volume high-value sources
  (a daily journal) are never nudged off. The node offers; the owner decides
  (`put_pack_offer`, POST /derivation/offers/resolve; dismiss backs off 30 days).
  Consent precedes computation: special-class packs are never auto-trialed.
- **[O] Packs 0.2.4**: health.mental felt-state discipline; aspirations.goals
  named-goal discipline (Wave-B contract iterations from measured gate junk).


## [1.3.27] — 2026-08-26

### Fixed
- **[D] Graph nodes for structured facts take their HEAD value, not raw JSON.** The
  materializer minted nodes literally named `{"project": …, "status": …}`; the head
  field is the identity (what pack `key_fields` key on) — the rest is fact state.
  Existing JSON-named nodes repoint and purge on the next materializer pass.
- **[D] A fact's time is its evidence time (owner rule).** `valid_from` anchors to the
  stated occurrence, else the source record's date (`event_date`, threaded from the
  DerivationJob); accumulation facts anchor to the newest record that completed them.
  Extraction time remains in the extractor provenance stamp only. Correction still
  inherits the belief clock.

### Fixed
- **[D] Graph nodes for structured fact values are named by their HEAD value** —
  the materializer minted nodes literally named `{"project": …, "status": …}`;
  the head field (what pack `key_fields` key on) is the identity, the rest is
  fact state. Next materializer pass re-points edges and the value-surface purge
  removes the JSON-named orphans.
- **[D] A fact's time is its evidence time.** `valid_from` anchors to the stated
  occurrence, else the source record's date (`event_date`, threaded from the
  DerivationJob), else now; accumulation facts anchor to the newest record that
  completed them. Extraction time remains in the extractor provenance stamp only.

## [1.3.26] — 2026-08-26

### Added
- **[S1] Attribution ladder A1–A4** for ontology-pack fact derivation
  (PLAN_DERIVATION_WAVE2 §WA). A1: reaction-prefix hygiene at extraction input +
  deterministic person-field rejects (pronouns, generics, garbled lists). A2: every
  person-fact assertion carries `about: owner | other:<name> | unclear` — missing
  never silently means owner. A3: `other:` facts route to that person's dossier
  (`subject_entity_id`), unresolvable/unclear assertions quarantine into
  `fact_conflicts` — nothing the ladder catches is silently lost. A4: a second-pass
  verifier model judges every assertion before it lands (`TOPOS_DERIVATION_VERIFY`,
  default `required`); measured on a 32-record battery: junk classes 19/23 → 0/23
  while controls recover to near-extractor recall (template `shadow-9`, verifier `a4-2`).
- **[S1] DerivationJob at ingest** (migration 65: `pack_registry` + `derivation_progress`):
  ontology packs run as a canonical enrichment job for registry-enabled packs (Wave A
  seeded: relationships.social, work.career, health.physical). Packs now ship inside the
  wheel (`topos/features/derivation/bundled_packs/`). Extraction/verification model split
  is measured, not aesthetic (extractor keeps recall, verifier supplies precision).
- **[S1] Event-identity keying** (`event_identity: once|windowed:N|dated`, pack-declared):
  a retold life event corroborates the original instead of minting a phantom duplicate —
  measured before: one firing → 6 events, one death → 5.
- **[S1] C7 facts-direct answers**: known-item asks ("what medications am I taking")
  answer straight from live facts — exact values, validity dates, evidence counts, zero
  LLM — gated by packet resolution (special-class requires `facts_all`); follows the
  `band` deterministic-answer precedent.
- **[S1] Derivation surfaces API**: `get/put_derivation_pack(s)` (lens catalog: registry
  + per-pack live fact and quarantine counts, owner enable/disable) and
  `get/put_fact_conflict(s)` (the quarantine/conflict review queue).

### Changed
- **[S1] `required_fields` retired from the pack contract** (4 measured fabrication
  backfires: forced fields make models invent values — "Founder @ Smithers"). Parsed for
  compatibility, never enforced; intent survives as abstention guidance.
- **[S1] Pack prefilter learns the pack's own eval gold** — 13+ gold examples across 11
  packs were silently dropped by their own prefilters; an invariant test now guards every
  pack, present and future.

### Fixed
- **[S1] Derivation extractor provenance stamp told the truth all along — the constant
  didn't.** `writer.py` carried a stale local `TEMPLATE_VERSION = "shadow-1"`, stamping
  every fact from later templates as shadow-1; it now imports the real one.
- **[S1] Honesty metadata now outranks evidence in the inference packet's truncation
  order.** The packet builder's char slice ate the TAIL of the serialized context, and the
  catch-all loop placed honesty keys (`truncated` row-cap markers, `exclusion`,
  `empty_cause`) last — deleting "this result was capped" exactly on the large packets
  where caps fire. Those keys are now hoisted ahead of the evidence they qualify; a cut
  context ends with a visible `…[CONTEXT CUT AT CHAR LIMIT]` marker instead of silently
  invalid JSON; and the builder's return flag is renamed `context_truncated` to stop
  colliding with the retrieval packet's row-cap `truncated` (two meanings, one key; no
  committed consumer read the old name). Found by a peer session's review of the
  truncation-honesty work-in-progress.


## [1.3.25] — 2026-08-25

### Added
- **[S1] [P] Packet resolution** (`packet_resolution`: `scores_only` | `facts` | `facts_all`, default
  `scores_only`): per-database setting for how much fact content the inference packet may
  carry. Two structural floors: non-owner turns are always `scores_only`, and content flows
  only while the resolved `primary` model runs on-device (hosted binding ⇒ paused, declared
  on the turn). Resolution joins the retrieval fingerprint and session cache key (disclosure
  dimension); a downgrade expires cached query artifacts immediately. New engine messages
  `get/put_packet_resolution_config`; inference-mode retrieval gains a facts lane and the
  packet a structured `facts` block at `facts`+. (PLAN_DERIVATION_LAYER.md, owner decision
  2026-08-25.)
- **[S1] Migration 63 lands.** `enrichment_record_progress_v1` was written on 2026-08-25 and
  held out of the repo head the same day: registering it early let an editable-dep checkout
  stamp the live database to `user_version=63` while the installed engine understood 62, and
  the downgrade guard correctly refused every write — ~25 minutes without ingest, sync or
  enrichment. A release is the only thing that can safely ship it, so it ships here, ahead of
  `derivation_provenance_v1` (64). After installing, raise the routine playground's
  `PG_NODE_SCHEMA_BASELINE` to 64.
- **[E:entities] iMessage entity extraction now runs at ingest**, and the API reports the job
  list the engine actually runs — an override could previously make the reported and executed
  lanes disagree, and `enrichments[].lanes` was already override-aware while the summary was not.
- **[E] Enrichment records a per-record "this ran" marker** (backed by migration 63), so a
  backfill stops re-scanning records that legitimately produced no output. On the live node a
  2,400-record imessage/entities backfill left 1,903 of 2,355 messages still counting as
  "missing", because roughly three in five ("ok", "haha", an emoji) contain no named entity and
  NER correctly emits nothing.
- **[E:entities] Contacts without a display name get a seeded person entity**, so they can be
  linked and resolved rather than silently dropping out of the spine.
- **[O] Script to fold a misnamed test-dataset corpus back into the owner's dataset.**
  Neither enrichment change bumps `JOB_SPEC_VERSIONS`: both change which jobs run and how
  their progress is tracked, not what a job derives, so previously-derived outputs stay valid.
- **[O] Derivation-layer runtime** (`topos/features/derivation/`): pack loader + validator,
  identifier guard, sandboxed prompt template, lexical prefilter, the fact-assertion ladder
  (NOOP / CORROBORATE / CORRECT / SUPERSEDE / CONFLICT) and two no-LLM synthesizers. Library
  code only — no ingest-pipeline caller, no packs shipped, nothing runs on a node. It backs
  the shadow-pilot harness (PLAN_DERIVATION_LAYER.md §F-S) and is released here so the
  provenance columns it writes have their schema in the same version.

## [1.3.24] — 2026-08-21

### Added

- `[E:facts]` `[E:llm]` **The two node functions that were hard-wired to local Ollama —
  facts extraction and the conversation-context classifier — can now run on hosted
  providers, so an owner on a weak machine can pay their way onto Topos Secure or
  OpenAI.** Both configs accept `{provider, model}` with the same provider set signal
  extraction already speaks (`ollama` / `platform` / `openai` / `redpill`), stored in a
  per-function provider `engine_config` key beside the model key that already existed.
  - **The vocabulary lives in one module so the two configs cannot drift.**
    `topos/config/node_function_providers.py` owns the provider set, the hosted-model
    defaults and the UI→engine provider mapping (`platform` runs on the OpenAI adapter —
    the same mapping signal extraction uses). `resolve_facts_llm_request()` /
    `resolve_context_llm_request()` return `(provider, model)`, and `normalize_put_config`
    validates writes so a garbage body 400s instead of storing.
  - **Legacy bodies keep behaving exactly as before, and hosted providers never inherit
    the Ollama env fallback chain.** A bare string or `{"model": ...}` with no provider is
    the legacy ollama-model write; an unset provider means ollama, where the model still
    resolves through the historical env chain. A hosted provider only ever comes from the
    device override, and resolves the override model or that provider's own default —
    never Ollama's.
  - **The fact pass routes through the real adapters and meters under the real
    provider.** `_make_hosted_extractor` (`topos/features/facts/llm_extract.py`) drives
    the Redpill/OpenAI chat-completions adapters on the same contract as the Ollama
    extractor, emits usage observations under the engine provider actually billed, and
    hosted transport *and* auth failures degrade the batch exactly like an unreachable
    Ollama — retrying per row would fail identically. An explicit `model` argument stays a
    caller-pinned local model; only the device config can steer the pass onto a hosted
    provider.
  - **The classifier's `ModelRequest` carries the resolved provider**
    (`topos/features/signal/conversation_context.py`) instead of a hardcoded `"ollama"`.
  - **GET payloads gain `provider` and `device_override_provider`, and their presence is
    the capability signal**: the settings UI refuses hosted saves against a pre-provider
    engine build that would silently store a provider it never reads.

### Fixed

- `[O]` **The test suite could reach the operator's live shadow log, and nothing said
  so.** `~/.topos/scope_shadow.on` is an operator gesture — the node under the app shell
  inherits no shell environment, so touching that file is the only reachable way to arm
  shadow mode — and on a machine where someone had done it, `enabled()` returned True
  *inside the test process too*, so every production hook (`QueryPipeline.execute`, the
  `tools_retrieve` handler, the engine-direct `/tool_index` route) appended its
  observations to `~/.topos/scope_shadow.jsonl`, the file the running node is writing.
  Since the log gained rotation it is worse than appending: `ShadowLog.append` rolls the
  file to a `.1` sibling once it passes the cap, so a test run could rename the log out
  from under a node concurrently appending to it — and that file holds the only
  real-traffic evaluation record the classifier promotion
  (`PLAN_SCOPE_CLASSIFIER.md` §6.5) is measured from, which no re-run can regenerate. The
  quieter reason is reproducibility: whether `enabled()` was True depended on whether
  someone had touched a file in their home directory, so the suite meant something
  different on an operator's laptop than in CI.
  - **The guard takes no marker exemption, unlike the live-DB guard beside it.**
    `_no_live_scope_shadow_guard` (`tests/conftest.py`) is autouse and lets nothing
    through: `live`/`e2e`/`qq_eval` mean to read a real database, but no lane means to
    write the operator's shadow log — and those are precisely the lanes that run real
    queries, which would file synthetic eval traffic into the record as though it were the
    traffic the log exists to capture. Opting in still works: a test that wants shadow ON
    sets the env flag itself, and the pinned log path is what makes saying yes safe.
  - **Pinned by the log's own supported lever, not a monkeypatch.** The guard sets
    `TOPOS_SCOPE_SHADOW_LOG`, which reaches a `ShadowLog` built in a subprocess — a
    `setattr` on the path resolver never could, and the lambda variant broke the test
    asserting the env override works.
  - **The guard has its own tripwire, because it is invisible when it works.**
    `tests/query/test_scope_shadow_hermetic.py` asserts the resolved path BEFORE writing
    anything, so a guard that has come loose is not discovered by appending to the very
    file the test exists to protect — and nothing else in the suite fails if the guard is
    deleted, which is why this file has to.
  - **Measured:** public lane 3745 passed, 15 skipped, exit 0; the tests' sentinel string
    appears 0 times in the operator's live `scope_shadow.jsonl` and no rotation sibling
    was created.

## [1.3.23] — 2026-08-21

### Added

- `[E:query]` **Q3 — a question can now be anchored to a PERIOD THE SENTENCE DOES NOT NAME,
  derived from the subject's own mention density, and the derivation is returned so the
  owner can dispute it.** "What did I miss while I was heads-down on the classifier?" was
  unanswerable, and not for want of data: every window this pipeline could build was parsed
  out of the WORDS — an explicit range, a relative phrase ("last week"), a differenced pair —
  and that question contains no dates at all. The period the owner means is in their own
  activity, and only the node can see it. `topos/query/entity_window.py` resolves the anchor
  the other way round: **the sentence names a subject, the subject's mention density names
  the period**, and the existing attention triage then runs INSIDE that derived window.
  - **The method is stated, not implied — `mention_density_peak_span`.** Mentions are
    bucketed by calendar day over the entity's observed span; a day is hot when it carries
    at least the entity's mean daily rate, **floored at one** (without the floor an entity
    mentioned 20 times across 90 days has a mean of 0.22 and every day it appears on is
    "above average", which is not a period); hot days join into runs tolerating up to two
    cold days, because a heads-down stretch survives a weekend, and each run is trimmed to
    start and end hot; the run carrying the most mentions wins, ties to the most recent
    because "while I was heads-down" is a recent-past frame. A fixed trailing window (the
    sentence's window wearing a hat), a changepoint fit (needs more days than this corpus
    has, and returns a boundary rather than a span a person can read) and a percentile cut
    (a percentile of five active days is not a statistic) were considered and rejected.
  - **REFUSING TO GUESS is the feature, and it is three distinct refusals.** An entity with
    three scattered mentions has no heads-down period.
    `entity_window_density_too_thin` (under 5 mentions, under 3 active days, or under 3
    inside the winning run — not enough evidence for a period to exist),
    `entity_window_density_uniform` (mentions exist and are evenly spread: the run's rate is
    under 1.5x the rate outside it — a subject you touch every other day for two months is a
    habit, not a stretch), `entity_window_span_too_broad` (clears every floor but runs
    longer than 31 days — a "heads-down period" of three months is a guess with a date range
    stapled to it), plus `entity_window_unresolved` and `entity_window_no_mentions`. Each
    rides `packet["time_window"]["empty_reason"]` as a lane-level cause on Q1's precedent,
    so a refusal to date a window can never out-rank a genuine turn-level `store_empty`.
    **Sparse nodes will hit this constantly and that is correct behaviour, not a degraded
    mode** — `tests/query/test_entity_anchored_window.py` opens with ten tests that assert
    a refusal, before any test asserts a window.
  - **The derived range is DATA, returned to the caller.** `packet["time_window"]` — the
    same block the parsed window has always been published on, which `DefaultGameLayer`
    already forwards onto the summary payload — gains `source: "entity_mention_density"`,
    the method slug, `from`/`to`, `window_days`, `mentions_in_window` and `rate_lift`. "I
    think you meant Aug 4-11" is only useful if it is shown; a window the owner cannot see
    is a window they cannot argue with.
  - **The dates are content and never leave as a slug.** The narrowing ledger records
    `planner` + `windowed`/`not_applied` + one of eight new closed-set reasons; the range
    itself goes in the local-only `detail`, and `as_telemetry()` carries neither. The three
    public fields are enums and integers.
  - **The density may only see the record surface the grant authorizes.** This is the plane
    that matters, because a window is derived from records the answer may never contain:
    the mention scan is bounded to `manifest.canonical_tables` (∩ the request's
    `source_ids`), plus `triage_verdicts.record_id` for a scope whose `signal_objects` name
    `attention_summary` — the exact records that scope's own digests are already computed
    over, which is how `attention:read` (canonical_tables: `[]`) is reachable at all without
    a special case. A scope authorizing neither derives nothing. Black-holed and
    request-excluded record ids are removed BEFORE the arithmetic, so a protected subject
    cannot move the dates; an exclusion the engine cannot compile abandons the derivation
    rather than deriving around it. `tests/query/test_entity_anchored_window.py` proves each
    by severing it: dropping the table bound, the source bound, or
    `_blackhole_blocked_record_ids` each changes the derived dates, and a mention whose
    `canonical_table` is NULL never votes.
  - **It composes with the windows that shipped rather than forking them.** The derivation
    is armed only when the sentence carries BOTH an anchoring frame ("while/when/during …
    heads-down on / deep in / buried in / working on …") AND a cost half ("miss", "slip",
    "fell through", "distracted"), and only when the planner found NO window of its own —
    neither `plan.time_range` nor `plan.as_of`, so "in July 2026" (which parses to an
    `as_of`, not a range) still wins over the density, as does a differenced two-window ask.
    When it fires it writes `plan.time_range` and flows through P3's existing window path;
    every other question takes the byte-identical path it took before.
  - **The triage is the one that already exists.** `attention:read` items are selected for
    the derived window by the day already encoded in their `object_key`, recorded as
    `retrieval/windowed/entity_window_triage_lane` with the drop count, or as
    `no_match`/`entity_window_no_triage_in_window` when the window is real and holds no
    digests. No triage logic is reimplemented; `triage_verdicts` already holds the analytics.
  - **The window is a QUERY, not a filter over the newest page.** `_load_attention_summary_items`
    fetched the ten newest interest objects — five days, one digest and one interest profile
    each — and the window was applied to whatever those turned out to be. Right for "what did
    I miss yesterday", which is the question it was written for, and wrong for every report
    that names a past week: a report for Aug 11-16 asked on Aug 21 fetched Aug 17-21, dropped
    all of it, and reported `entity_window_no_triage_in_window` — while the node held a digest
    AND an interest profile for every one of the six days it was asked about. Filtering a
    fixed page is not asking for the days, and downstream the two are indistinguishable:
    both produce an empty attention section and a ledger line that reads as a quiet week.
    A resolved window now goes into the SQL, with the row budget widened to hold its days
    (capped at 31, past which the window's newest days answer) and undated keys selected
    whatever the window, matching the rule `_attention_items_in_window` already documents.
    That filter still runs and is still the authority on what counts as in-window. When the
    window genuinely holds nothing, the `dropped` count on the ledger line is now every
    digest the node holds rather than however many the `LIMIT` had happened to reach — a
    number about the owner's week rather than about the page size. The inference path takes
    the same window: a packet scored on the newest five days while `plan.time_range` says
    otherwise is a score for a week nobody asked about.

- `[E:query]` **Q1 — "did I actually do it?" is now answered PER GOAL, with the record ids
  the claim rests on, or an explicit statement of which kind of nothing was found.**
  "What did I say I'd do last week, and did I actually do it?" returned a list of goals and
  a separate list of journal and message rows, with nothing connecting them. The model was
  left to adjudicate, and what it adjudicates on is wording: a goal that says "finish the
  rewrite" and an unrelated entry that says "rewrote the intro" look like progress. The
  result is a **confident wrong answer**, which is the most damaging failure shape in the
  catalog precisely because the owner cannot tell it from a right one. `retrieval.py` now
  resolves each stated goal to **its own `record_id` and its own entity ids** and retrieves
  evidence against those, and `packet["commitment_report"]` carries one entry per goal.
  `DefaultGameLayer` forwards it on the `summary` payload — without that line the mode is a
  mechanism computed and never consumed, and synthesis goes straight back to matching
  wording against wording.
  - **The join is per goal, not one blended query.** Each goal's mentions are joined
    separately through the shipped `_entity_thread_mentions`, so a row can only ever be
    evidence for the goal whose own ids reached it. A blended query is how one goal's
    follow-through gets attributed to another — the same wrong answer as before, now
    wearing a citation, which is worse rather than better.
  - **Evidence must post-date the commitment.** The window is `[stated_at, ∞)` per goal, and
    a goal with no statement time gets **no evidence lane at all** rather than an unordered
    one. Without this the join re-creates, with ids instead of wording, the "sounds related,
    must be progress" error it exists to remove: the fixture carries a row that shares the
    goal's entity *and* its vocabulary and is dated four days earlier, and only chronology
    separates them. There is deliberately **no lexical fallback** for a goal that resolves
    no entity — matching the goal's words against the corpus is the original bug.
  - **"No evidence found" is four different sentences and they never collapse into one.**
    Per goal, on the shipped empty-cause taxonomy in closed-set slugs:
    `not_queried`/`commitment_scope_no_evidence_store` (the grant names no store to search —
    the `work_context:read` shape, which names only `profile_records`),
    `not_queried`/`commitment_goal_unresolved` (no entity link, so no ids to join on),
    `not_queried`/`commitment_goal_undated`, `store_empty` + its supply sub-cause,
    `no_match`/`commitment_no_evidence_matched` (looked and missed) and
    `scope_denied`/`commitment_evidence_withheld` (**the vetoed lane** — candidates were
    reached and a plane removed them). Collapsing the last into the second-to-last would
    tell the owner their week was empty when their own exclusion emptied it.
  - **The report is a PROJECTION of `packet["summaries"]`, never a source**, on exactly the
    contract Q7's thread runs on. It is drained at the very end of `retrieve()` — after the
    fusion cap, after `_blackhole_policy_for_summary`, after `_enforce_request_exclusions` —
    and intersected with the surviving rows, so **it cannot cite a record the answer does
    not already carry**, and a goal whose own row did not survive gets no entry at all. It
    adds no plane of its own because it inherits every one, which
    `tests/query/test_commitment_evidence_retrieval.py` proves by severing them one at a
    time: excluding the subject empties the citation (and an unrelated exclusion does not),
    black-holing the subject empties it for a grantee at **wire A, before the row is read**
    (and un-black-holing restores it), a manifest naming no table reaches none,
    `CanonicalStore.get()` — which takes no `disclosure_tier` — is asserted never to be
    called, and replacing `_load_commitment_evidence_items` with a function returning `[]`
    removes the report entirely rather than falling back to anything.
  - **Entity ids are owner-only throughout.** Record ids and timestamps are safe here
    because they are already in `summaries`; entity ids are not, because nothing else in the
    packet publishes them. Grantees get `entity_count` — named for the owner, counted for
    everyone else. The `commitment_evidence_withheld` cause is likewise owner-only: for a
    grantee that line is a receipt that the evidence exists, which is the existence leak the
    entity lane has already had once.
  - **The mode is armed by intent, not by topic**, and both halves are required: a marker of
    the owner *placing* a commitment ("said I'd", "promised", "committed to") **and** a
    retrospective or interrogative marker ("did I", "actually", "followed through"). The
    follow-through half is deliberately past-tense only — the bare infinitives of completion
    are the words a commitment is *made* of, and with them "remind me I said I'd finish the
    rewrite" armed a progress audit on a request that was stating a goal. A goal browse
    ("what am I working on") and every other request take the byte-identical path they took
    before: the four seeded ranking-floor cases are unmoved at C11 0.725, C8 0.786, S4 0.917
    and T7 0.700.
  - **The gap this exposes, stated rather than papered over:** `work_context:read` is the
    grant the goal list belongs to and it names only `profile_records`, so on that grant
    there is effectively nothing to search and every goal reports
    `commitment_scope_no_evidence_store`. The mode is genuinely reachable today on
    `messages:read` and `ai_conversations:read`, which carry `user_goals` *and* a message
    store. Making it reachable on the goal-shaped grant is a scope-registry decision about
    what one grant may span, and that decision is the owner's, not this commit's.

- `[E:query]` **Q7 — a topic ask now returns a THREAD: one ordered conversation over the
  message stores the grant names, with its participant set and its decision points.**
  "Who did I talk to about the classifier, and what did we decide?" is one question, and
  the engine answered it with a ranked list — twice, if the owner also held
  `ai_conversations:read`. Three things were missing and none of them could be recovered
  downstream: **order** (`event_at` is on every item, but "these items are one
  conversation, in this order" is a claim only retrieval is positioned to make — ranked by
  relevance, the reply that ended the argument sits above the question that started it),
  **participants** (`speaker_label` is attached to prose only on a first-person plan, so on
  most phrasings the speaker is not in the packet at all), and **decisions** (where the
  thread settled, if it did). `packet["topic_thread"]` now carries all three, and
  `DefaultGameLayer` forwards it on the `summary` payload — without that line the mode
  would be a fourth mechanism computed and never consumed.
  - **The thread is a PROJECTION of `packet["summaries"]`, never a source.** Candidates are
    tapped from inside the existing `entity_thread` lane's own loop — same
    `link_query_entities` resolver, same mention join, same wire-A black hole, same
    disclosed `CanonicalStore.list()` — and then INTERSECTED with the packet at the very
    end of `retrieve()`, after the fusion cap, after `_blackhole_policy_for_summary`, after
    `_enforce_request_exclusions`. There is no path by which the thread can name a row the
    answer does not already name, which is why it adds no plane of its own: it inherits
    every plane, and severing any one of them empties the thread with it. Proven by
    ablation in `tests/query/test_topic_thread_retrieval.py` — excluding the subject
    ("…but nothing about X") empties it, black-holing the subject empties it, a
    `journal_entries` row the same entity links is unreachable under a message grant, and
    replacing `_load_entity_thread_items` with a function returning `[]` empties it
    entirely. `CanonicalStore.get()` (which takes no `disclosure_tier` at all) is asserted
    never to be called.
  - **`cross_source` is honest, and today it is usually `false`.** The assembly spans
    exactly the message tables the manifest names. No scope in
    `topos/query/scope_registry.json` names both — `messages:read` →
    `conversation_messages`, `ai_conversations:read` → `ai_chat_messages` — so **on every
    grant that exists today the thread is single-store**, and it says so on the ledger as
    `retrieval/scoped/topic_thread_single_store`. The cross-store assembly is implemented
    and tested against a two-table manifest; making it reachable in production is a
    scope-registry decision about what one grant may span, and that decision is the
    owner's, not this commit's.
  - **Participants have their own three-plane rule**, because a name off `contacts` is the
    one thing the thread discloses that the summary items do not. Black hole first and
    silently (a roster of two that says "and one withheld" has confirmed the third exists);
    then the disclosure tier — named at `owner_raw`, **counted and not named** below it,
    with `disclosure/dropped_items/topic_thread_participants_withheld` carrying the integer
    and never the name; then the selector policy, so a grant that already names an entity
    may name it. The owner is a boolean (`owner_participated`) and never a roster entry. A
    model turn is typed `assistant` and is deliberately **not** counted as a person —
    answering "who did I talk to about the classifier" with a chatbot is a wrong answer,
    not a lenient one.
  - **Decision points are lexical, closed, and conservative on purpose.** The failure that
    matters is the false positive — telling someone they decided something they did not —
    so only explicit performatives of settling are matched, the emitted `marker` is a slug
    from a fixed set (`decided` / `agreed` / `settled_on` / `chose` / `signed_off` /
    `locked_in` / `final`) and never the matched words, and `decision_points` is an empty
    **list** rather than a missing field so "a thread, no identifiable decision" is a
    sayable answer (`retrieval/scoped/topic_thread_no_decision`).
  - **Two things the tests found and the implementation had wrong.** (1) A receipt that was
    itself a disclosure: `topic_thread_no_message_rows` fired with `dropped: 0` on exactly
    the path where the wire-A black hole had removed the subject's mentions at source,
    telling a grantee — in a slug whose meaning the protocol guarantees — that the entity
    they named resolved and has a thread. It is now emitted only when there were candidates
    to lose. (2) "have we decided anything yet?" contains "we decided" and is the *opposite*
    of a decision; a closed list of interrogative auxiliaries, conditionals and negations
    immediately preceding the phrase now disqualifies it, and every occurrence is checked
    so one hedge cannot suppress a real settling later in the same row.
  - **No ranking effect, by construction.** Nothing in `summaries`, `rows`, `scores` or the
    fusion order changes — the mode adds one new top-level packet key, and
    `narrowing.result_is_empty()` does not read it, so an empty packet cannot be made to
    look non-empty by it. The seeded ranking floors reproduce exactly: S4 `0.917` (floor
    `0.917`), T7 `0.700` (floor `0.700`). `tests/query/` 680 → **716 passed**, no
    regressions.
- `[E:query]` **The `entity_thread` lane now has a ranking floor, and the honest measurement
  says the lane is very slightly net negative.** Severing the lane (adversarial sweep
  2026-08-19, probe `r15`) turns **14** tests red, and every one of them is a presence or a
  privacy assertion: rows were emitted, rows carried the right source tag, rows respected
  the manifest. Nothing anywhere asked whether the rows make the answer better. **No
  behaviour change: `topos/query/retrieval.py` is byte-for-byte unchanged, and no retrieval
  weight, fusion order, or lane scoring was touched.**
  - **The measurement.** `_load_entity_thread_items` was replaced at RUNTIME with a
    function returning `[]` — the `r15` severing with no source edit — and both arms were
    scored with `score_composition` unchanged, against one read-only snapshot of the owner
    database (`scripts/snapshot_owner_db.py`) plus a freshly built seeded corpus. Exactly
    **four** of the catalog's 51 cases retrieve anything through this lane, so they are the
    whole of what it can be judged on: C11 `0.725 → 0.725`, C8 `0.786 → 0.786`,
    S4 `1.000 → 0.917` (**−0.083**), T7 `0.700 → 0.700`. Catalog aggregate: mean
    `0.8344 → 0.8327`, **−0.0016**.
  - **Answer: not net positive.** No case gains. Three are flat — the lane adds a source
    tag and moves no score, and on C8 it displaces `recent` for no measured gain. One
    loses: on S4 the lane adds a third item (`n_items` 2 → 3) the oracle does not want, and
    specificity falls `1.000 → 0.667`. The sweep's attribution of C11 (`0.900 → 0.725`) and
    C8 (`0.929 → 0.786`) to this lane **does not reproduce**: both sit at the lower number
    with the lane ON *and* OFF, so whatever moved them, it was not `entity_thread`. C9 and
    C29 carry no `entity_thread:*` source at all and cannot have been affected either way.
    Keep, gate, or rework is the owner's call; nothing here was tuned to recover a score.
  - **The guard**, `tests/gap/qq/engine/test_en_qq_entity_thread_ranking_floor.py`, uses the
    ratchet idiom already in this repo (`QUALITY_FLOOR` in the justfile): a per-case floor
    at the high-water mark on THIS environment, failing below, nagging to be raised above,
    never lowered to make a red go away. The floors pin the **as-shipped** numbers,
    dilution and all — pinning the lane-OFF numbers would be pinning a wish. Per-case
    rather than aggregate on purpose: a single `0.83` hides a lane that is flat on three
    cases and negative on one, which is the exact shape this file exists to expose. Runs in
    the `qq_eval` lane (`just test-owner-db-eval`, which snapshots `~/.topos` and never
    writes to it); the seeded half needs no live database.
  - **Two assertions, proven red independently.** Non-vacuity — the case must still
    retrieve *something* through `entity_thread:*`, or its floor has silently migrated to
    another lane's behaviour and stopped being evidence. Then the floor itself. Severing
    the lane at runtime → `4 failed, 1 passed`, all four on non-vacuity with the composites
    unmoved. Severing the neighbouring `entity_context_items` lane instead, leaving
    `entity_thread` alive → `2 failed, 3 passed`, both on the floor with
    `sources: ['entity_thread:conversation_messages']` still present (T7 `0.250 < 0.700`,
    S4 `0.000 < 0.917`). Restored: `5 passed`, and the four composites reproduced to the
    third decimal on a second independent run.
- `[E:query]` **"Embed the subject, not the instruction" is now guarded at the embedding
  call.** The retrieval rewrite that swaps the owner's sentence for the distilled
  `needle_text` before the vector lane could be deleted outright and **1,058 tests passed
  with zero new reds** (adversarial sweep 2026-08-19, probe `r07b`), while the slug it
  emits — `embedded_subject_not_instruction` — is declared in **four** places across three
  repos (`topos/protocol/narrowing_vocabulary.json`, `topos/query/narrowing.py`,
  `next/lib/mcp/narrowingLedger.ts`, `control_plane/narrowing.py`). Four declarations of a
  token nothing verified was ever spoken. **No behaviour change: `topos/query/retrieval.py`
  is byte-for-byte unchanged.** Two independent assertions, in
  `tests/query/test_retrieval_needle_text.py`:
  - **The encoder gets the needles.** A spy replaces the embedding backend and records
    every string handed to it, so the assertion is made AT THE CALL rather than on a
    return value — the whole failure mode is that the wrong string is embedded and every
    downstream shape stays plausible, which leaves nothing to assert afterwards. The
    string is followed through a real `SignalService`: `retrieve()` → `_semantic_hits` →
    `search_vectors` → encoder, with only the encoder and the vector page faked.
    Recordings are tagged by lane, because `_load_ranked_clusters` deliberately keeps the
    owner's sentence and an untagged recorder could be satisfied by either string. The
    needle-absent case is the control: it proves the spy CAN see the sentence at that
    seam, so the positive assertion is not vacuously satisfied by a recorder that saw
    nothing.
  - **The receipt only fires with the action.** `embedded_subject_not_instruction` is
    recorded when `needle_text` differs from the sentence, and is ABSENT both when no
    needles are sent and when they equal it. A receipt that appears without its action is
    the failure class this programme keeps meeting.
  - **They fail independently, which is the point.** The ledger class never reads the
    encoder and the encoder class never reads the ledger, so severing either half turns
    exactly one of them red. Verified three ways: rewrite removed entirely → `2 failed, 16
    passed` (one from each class); `ledger.record` removed alone → `1 failed, 17 passed`
    (slug only); `semantic_query = needle_text` removed alone → `1 failed, 17 passed`
    (encoder only). Restored to an empty `git diff` on `retrieval.py`: `18 passed`, and
    `1058 passed, 2 skipped` across `tests/query` + `tests/gap`.
- `[E:query]` **`relationship_context:read` declares the canonical table it was already
  reading.** The previous release bounded the entity-mention lane by the manifest's
  `canonical_tables`, which was the right rule applied to an under-specified grant: this
  scope declared `canonical_tables: []`, so the rule read it as authorizing no canonical
  table and the lane correctly emitted nothing. Eval D4 ("Who is Marcus?", asserted at
  `tests/gap/qq/engine/query_eval_cases.py:288-292`, requires at least one `entity_mention`
  item at this scope) went red on exactly that. The eval was not wrong. Before the bound
  existed the scope was **already** emitting those pointers — undeclared, because nothing
  was checking. This adds `canonical_tables: ["conversation_messages"]`, which converts a
  de-facto grant into a declared, tier-gated one. **Measured relative to shipped
  behaviour this is not a widening; it is the same behaviour, finally stated**, and stating
  it is what puts the lane under the exclusion and disclosure planes that a silent grant
  routed around.
  - **`conversation_messages` alone, deliberately.** Measured read-only against the live
    node: the probe entity yields 2 `entity_mention` items from that table alone, where D4
    requires ≥ 1. `journal_entries` was considered and **excluded** — it is the most
    sensitive store on the node and the canonical example the enforced-exclusion feature is
    written around, and a relationship scope that reads the journal to name a collaborator
    would be a real widening rather than a declaration. The owner expects to point this
    grant at further data later; that will be its own decision carrying its own evidence,
    and nothing here is built in advance of it.
  - **The registry is one artifact in four places, and they now agree.**
    `control_plane/uma/scope_registry.json` is canonical; this engine copy, the wiki's
    `scope_registry_mvp.json` and the front end's `scopeCatalog.ts` are generated from it by
    `just sync-scope-registry`. The canonical copy had been given
    `["conversation_messages", "journal_entries"]` while this copy carried only
    `["conversation_messages"]`, so the narrow value shipped here and the wide value shipped
    to the control plane's own resolver — a registry that disagrees across repos is worse
    than the gap it was patching. Reconciled at the source to the narrow value and re-synced;
    `just sync-scope-registry-check` and the `scope-registry` quality gate (`sync-check`,
    `parity-backend`, `parity-frontend`) are green. `canonical_tables` is deliberately not
    projected into `scopeCatalog.ts`, which is a consent-UI surface and carries no table
    manifest, so that file is byte-identical.
  - **Sabotage-checked, and the guards that motivated the bound re-run green.** Reverting
    this scope to `canonical_tables: []` turns D4 red with `dossier present but no
    supporting mentions` — the eval's own words, its assertion untouched. The property the
    bound exists to create survives intact: a scope still declaring `canonical_tables: []`
    receives **zero** mention pointers, asserted by
    `test_a_grant_with_no_tables_gets_no_pointers` and re-confirmed live by that same
    sabotage. A grantee still learns **zero** identifiers of a black-holed entity
    (`test_a_grantee_learns_nothing_of_a_black_holed_entitys_records`), and the exclusion
    red-checks still bite — no-op'ing `apply_exclusions` turns 11 of them red. 142 guard
    tests and 676 tests across the entity/exclusion/disclosure/retrieval surface pass.

- `[E:query]` `[E:storage]` **A turn can now say what it cost.** §09 shipped four quality
  metrics and no latency metric; `query_artifacts` had no duration column, so the redesign's
  price per request was unknown by construction. This release measures it and changes
  nothing else — nothing here is an optimization, and no execution model moved.
  - **Per-stage durations on the narrowing ledger.** `NarrowingEntry` gains an optional
    `elapsed_ms: Optional[int]`. A whole non-negative integer carries no text, so it rides
    `as_public()` without touching the privacy argument that the rest of the ledger rests
    on. `_duration()` coerces in `__post_init__` for the same reason `_member()` does — the
    dataclass is a public constructor and `as_public()` serializes whatever the object
    holds, so a float, a string or a negative clock reading must not reach the wire. The
    key is **omitted** when a stage did not time itself, which is today's normal case:
    absent means unmeasured, present means measured, and an unconditional key would have
    silently rewritten every existing entry on the day this shipped. `as_telemetry()` sums
    per *stage* rather than listing per entry, and likewise omits the map when it is empty —
    `tests/query/test_narrowing_ledger.py:96` asserts an exact dict, and it is still green
    unchanged. `extend_public()` carries the field across the seam; the control plane's
    `merge_narrowing()` and the front end's `mergeNarrowingRecords()` rebuild entries
    field-by-field, so a missing carry loses the measurement with no error at either end.
    All three now carry it, and all three have a test that goes red when the carry is
    removed.
  - **A turn-level total on the one durable per-turn record.** New migration
    `query_artifacts_duration_ms_v1` (order 62) adds a nullable `duration_ms INTEGER` to
    `query_artifacts`. A **new** migration rather than an edit to `wiki_mvp_phase0`: the
    live node's `user_version` is already past it, and editing an applied migration in
    place strands exactly the machine the measurement is for. Nullable rather than `0`,
    because a row written before this shipped was genuinely never timed and a zero would
    read as a fast turn. `pipeline.py` times the whole retrieve-to-persist path — retrieval,
    disclosure filtering, the minimizer, the game layer, inference, DDR assembly, session
    write — which is later and more complete than the DDR's own `timings.total_ms`. It
    cannot include the INSERT that carries it; a value cannot time its own write.
  - **The fifth §09 metric exists as a script, and it was run.**
    `scripts/query_latency_percentiles.py` computes end-to-end p50/p95 per entrance,
    read-only (`mode=ro` plus `PRAGMA query_only`), printing sample count and window beside
    every number. Nearest-rank percentile, so every figure printed is a duration some real
    request actually took. **Run against this node on 2026-08-19, 30-day window: home chat
    0 samples; routines 77 samples, p50 59306 ms, p95 124148 ms, max 168793 ms; engine
    per-scope-query 0 samples.** The zeros are the correct output and the script says so in
    its own footer rather than being read as a failure — the column shipped empty today and
    fills from the next turn onward.
    - Two defects were found by running it rather than by reading it, and both are recorded
      in the source. Timing routine runs from `payload_json.created_at` gave a p95 of
      86,429,354 ms, because `created_at` is stamped when a run is *scheduled* and the delta
      was measuring a cron interval; it times from `started_at` now. Runs closed by the
      stale-run reaper then left a 23.8-hour maximum, because their `completed_at` is the
      reaper's clock and not the work's; `--stale-run-minutes` (default 60, mirroring
      `control_plane/config.py:577`) excludes them and reports them as `abandoned_runs`
      rather than dropping them silently.
    - Entrance is **not** attributable from `query_artifacts`: `requester_id` holds a grant
      identity or the literal `"mcp"`, and the routines bridge sends `requester_id=None`.
      The script derives the two entrances from `routine_runs` and from the front end's step
      trace instead, and says so where a future reader would otherwise assume a split
      exists.
  - **Sabotage-checked.** Removing the `__post_init__` coercion, the `extend_public()`
    carry, the store's coercion or the migration's idempotence guard turns 9 + 1 engine
    tests red; the two control-plane carries turn 12 red; five separate sabotages of the
    percentile script (empty-series returning `0`, `created_at` for `started_at`, no stale
    ceiling, no leg-membership check, NULL folded in as zero) turn 6 red. All restored and
    re-confirmed green.

- `[E:query]` **The narrowing ledger's vocabulary is a closed set now, closed by the
  mechanism rather than by every call site's care.** `narrowing.py`'s docstring credited
  "enums by construction, not by care". An independent pass tested that by hand on
  2026-08-19 and it was false: `_slug` is a character filter with a 64-character cap, so
  it sanitizes and does not constrain membership, and
  `record("scope_routing", "dropped", "Sarah Chen divorce lawyer meeting")` arrived in
  `as_public()` — the serializer that, per its own docstring, is what leaves the node —
  as `sarah_chen_divorce_lawyer_meeting`. A name and a legal matter, fully legible. All
  three implementations (this one, the control plane's `slug`, the front end's
  `slugNarrowingToken`) had the same shape. There was no leak on the day, because every
  live call site passed a literal or a module constant; the guarantee rested entirely on
  every *future* call site, which is exactly what the docstring said it did not.
  - **`STAGES`, `ACTIONS` and `REASONS` are declared, and `_member` enforces them.** A
    value outside its set becomes `UNRECOGNIZED` (`"unrecognized"`). A coercion rather
    than a drop or a raise, because three properties have to hold at once: the entry
    still testifies that a stage narrowed the search, telemetry still cannot cost a turn,
    and the owner's sentence still cannot reach a public field. The sentinel is a member
    of every set, so coercion is idempotent across the merge hops.
  - **Enforced in `NarrowingEntry.__post_init__`, not in `record()`.** `as_public()`
    serializes the dataclass and the dataclass is public, so a `record()`-only check
    would have left the direct-construction path open — and `extend_public()` merges
    entries written by another codebase, which is precisely the careless-upstream case.
  - **The members are what the tree already emits.** Enumerated from the real call sites
    in all three repos (26 engine, 16 control plane, 13 front end), including the dynamic
    ones: `ManifestValidationError` codes, `_NOT_QUERIED_OUTCOMES`, the `grantee_scrub_*`
    and `entity_thread_*` families, and the `_scope_supply_state` returns. Nothing was
    invented. Tests walk `exclusion.py`, `negotiation.py` and `manifest_validation.py`'s
    AST so a rename there fails here instead of silently emitting `unrecognized` in
    production.
  - **`topos/protocol/narrowing_vocabulary.json` publishes the sets**, beside
    `query_field_contract.json` and located the same way. The control plane and the front
    end cannot import Python from here, so they test against the file; the engine's set
    is the reference and neither may emit a member it lacks. A drift test beats a comment
    saying "keep in sync".
  - **The tests that covered this were the reason it survived.**
    `test_public_fields_cannot_carry_the_owners_words` asserted the *slugged* owner text
    and called that safe; the front end's `ledgerCoverageAgreement.test.ts` asserted
    `how_often_do_i_go_to_the_gym` appears in the synthesis prompt, as the correct
    outcome. Both now assert the sentinel. `tests/query/test_narrowing_vocabulary.py`
    runs the verification's probe verbatim. Sabotage-checked in all three repos: swapping
    the membership check back for the bare slug turns 8 engine, 10 control-plane and 8
    front-end tests red.

- `[E:query]` **Keyword scope routing now has a retirement path, and a measurement that
  says it is not time yet.** The rule pile — ~24 regex predicates plus ~10 composite
  recipes, mirrored in the control plane and the front end — has only ever grown, because
  nothing measured what deleting a rule would cost. `topos/query/scope_arbiter.py` builds
  the path: keyword rules become `RulePrior`s, the Horos head produces a routing
  decision, and `arbitrate()` is the one written-down arbitration between them.
  - **Default OFF, and a test says so.** `TOPOS_SCOPE_HEAD_ROUTES` is unset everywhere in
    the package (a test walks the tree to prove it), and with the flag off `arbitrate()`
    returns the rule priors byte-identically. This release does not flip production
    routing; it ships the machinery, the gates and the measurement that would justify
    flipping it later.
  - **The head can only add a scope, never empty one, never widen disclosure.** Escalation
    (`ambiguity`/`ignorance`) and `confident-none` both defer to the priors, because the
    head's measured failure on real traffic is choosing *nothing*, not choosing wrongly. A
    scope the head names that no rule predicted is queried at `summary` — routing is not
    authorization, and a model with no disclosure signal does not get to raise an access
    mode.
  - **Promotion is arithmetic, not a call.** `RoutingGates` states the bar in the Horos
    card's own metric names (`exact`, `dead_rate`, `disjoint_rate`, per-scope recall) and
    `evaluate_promotion()` returns the failed clauses. Unmeasurable fails rather than
    passes: `routing_comparison()` returns `None`, never `0.0`, for a rate with no
    denominator, so an empty log cannot greenlight anything.
  - **Measured now, on the traffic that already exists.**
    `scope_shadow.routing_comparison()` regroups the per-scope shadow rows back into
    turns — routing is a decision about a *set*, and judging it row by row gives a head
    that correctly names both scopes of a compound ask partial credit for a right answer.
    On 52 routed turns of this node's captured traffic the head's `exact` agreement with
    the rules is **0.173** and its `dead_rate` is **0.596**, against a card `dead_rate` of
    0.149 on held-out synthetic data. Six of seven gates fail. The head is not ready, the
    numbers and their method are in `NOTES_HOROS_ROUTING_ENDGAME.md`, and
    `scripts/horos_routing_endgame.py` regenerates them read-only.
  - **No keyword rule was retired.** Retirement is what the gates authorise, and the gates
    need traffic that does not exist yet.

- `[E:query]` **A question about a subject can now reach that subject's records.**
  Every lane in the query path routes by SCOPE and filters by TIME. Nothing routed by
  WHO or WHAT, so "what happened with the Anthropic thread" was answered by whether
  the owner's words happened to appear in a row — while the entity graph that knows
  exactly which rows belong to that subject sat one join away and unreachable from the
  query path. `_load_entity_thread_items` closes that: the entities
  `link_query_entities` already resolves for a request (the same resolver the dossier
  lane uses — no new one) contribute their linked records as an additional fusion
  contributor, `entity_thread`, at the canonical lane's own weight. It contributes
  **beside** the scope routes and never replaces them; it is not a new access mode and
  does not require the graph query surface.
  - **It selects from disclosed rows; it does not fetch around them.** The obvious
    implementation — turn each mention row into a record with `CanonicalStore.get()` —
    would be a privacy incident, because `get(table, record_id)` takes no
    `disclosure_tier` and applies none. So the mention table supplies an id **set**,
    and the rows come from `CanonicalStore.list(..., disclosure_tier=…)`: two disclosed
    pages per table (a `contains` prefilter over the whole table, plus a plain recency
    page as the floor for surfaces that do not live in a filtered column), intersected
    by record id. A test spies on `get` and asserts it is never called for the turn.
  - **Every existing plane still binds.** Only `manifest.canonical_tables` are scanned,
    so an entity linking a `journal_entries` row contributes nothing under
    `messages:read` — including when the mention's `canonical_table` is NULL, which it
    is for 619 of this node's 4313 mention rows. Rows convert through the same
    `_canonical_row_to_item` the canonical lane uses (extracted, not transcribed, so
    the two cannot drift), carrying the belief/identity authorship filter, the scope
    redaction, the exposure-profile rule and speaker labelling. Items are stamped with
    `canonical_table` and `entity_id` so set 6's exclusions can reach a row by the very
    entity that contributed it. The soft time window, the black-hole policy, the rare
    gate and the disclosure filter all run over the result unchanged.
  - **`is_self` and unconsolidated aliases.** The owner's own entity is refused: its
    "thread" is not a subject, it is the corpus, and a first-person phrasing that links
    it would become an undirected dump. Duplicate `normalized_name` rows are real and
    heavy on this node (`topos` ×7, `personal projects` ×13); the lane keys on the
    resolved ids whatever their number and dedupes on `(table, record_id)`, so an
    unconsolidated subject contributes its whole thread exactly once. An unverifiable
    resolution fails closed, and under an active entity-selector policy the lane
    contributes only for explicitly accessible entities — the pipeline's existing
    suppression is person-shaped and this lane also threads orgs and places.
  - **It declares itself.** One `retrieval / contributed / entity_thread_lane` ledger
    entry per turn carries `linked_records` vs `matched` vs `contributed`, so a
    truncated scan is visible rather than silently answering with part of a thread;
    refusals record `entity_thread_is_self` and friends, because a declined thread and
    an empty one are different answers. Counts and closed-set slugs only — the entity
    name, the table names and the record text stay in local-only `detail`.

### Fixed

- `[O]` **`just gate` — the thing a developer runs before every release — opened the
  owner's live database and ran DDL on it.** The hermeticity work of 2026-08-19 and
  2026-08-20 made the PYTEST lane hermetic. `just gate` has seven legs and two of them are
  pytest; the guard lived in `tests/conftest.py`, so the other five ran with nothing
  watching them. Leg 7, `scripts/release_smoke_test.py`, said so in its own output and
  nobody was reading:

  ```
  INFO: Serving database /Users/<owner>/.topos/database.db (source=slot, profile=personaldb, schema=62)
  INFO: DB tuning: journal_mode=wal sqlite_vec=True
  DEBUG: Ensured table browser_visits exists
  ```

  - **The cause was one branch.** `TOPOS_DATABASE_PATH` was set only under `--seeded-db`,
    so the ordinary run — the one the gate does — booted the built wheel's FastAPI app with
    the variable unset, and `resolve_active_database` correctly answered with the
    developer's own Topos. It is now pinned to a scratch database in the run's temp
    directory on every path through `_install_and_verify`; `--seeded-db` copies into that
    same file instead of choosing whether to have one. What the check asserts is "the built
    artifact boots and answers `/healthcheck`", which a scratch database proves exactly as
    well. A test reads that from the AST rather than by string search, because the defect
    was that the assignment existed — it was just inside an `if`.
  - **`scripts/live_db_tripwire.py` extends the guard past pytest.** It arms the same
    `tests/live_db_watch` module — one implementation, no second copy to drift — in the leg
    and, through a generated `sitecustomize.py` on `PYTHONPATH`, in every Python child it
    spawns, including one in another virtualenv running another Python. It deliberately does
    NOT put the repo root on that path: that would make the smoke venv's `import topos`
    resolve to the working tree and delete the only thing a release smoke test is for. Every
    non-pytest leg of `just gate` now runs under it, and a test fails if one stops doing so.
  - **REFUSING IS NOT REPORTING, and here the difference was load-bearing.** The first armed
    run refused the connect and the leg still exited 0:
    `topos.core.state._open_owner_db_connection` catches the failure, logs
    `WARNING: Failed to create database connection`, and the app serves `/healthcheck` from
    a degraded path — so `app_boot_ok` and `release_smoke_ok` both printed over the top of
    it. Every armed process therefore appends what it refused to a journal file and the
    parent fails the leg from that, independent of exit codes and of whether the child chose
    to notice. Worth carrying forward as a fact about the node, not about this commit: a
    node whose database will not open still answers 200 at its front door.
  - **The tripwire self-checks before it is trusted.** `site.execsitecustomize` swallows
    exceptions, so an interpreter that failed to arm looks exactly like a clean one. Before
    each leg, a child interpreter arms a throwaway path as owner data, asserts the connect
    is refused, and asserts the refusal beat SQLite to the filesystem — a guard that lets a
    0-byte file through has already lost. It never names `~/.topos`: a hermeticity check
    that has to open the owner's database to prove itself is the bug it is checking for.
  - **Two adjacent defects in the same script, found by arming it and fixed here.** The
    `python -c` checks ran with the repo root as the working directory, which `python -c`
    puts on `sys.path[0]` — so the venv's `import topos` resolved to `./topos` and both
    checks described the working tree instead of the wheel just installed. They now run with
    the venv as cwd and the import check asserts `topos.__file__` is under the venv's
    `purelib`, so it is checked rather than intended. And `_find_wheel` sorted filenames
    lexicographically, which ranks `topos_node-1.3.9` above `topos_node-1.3.21`; since
    `dist/` is not cleaned between builds, any machine that has built twice was
    smoke-testing an old wheel. It sorts by parsed version now.
  - **What is established, and what is not.** Established, from the smoke test's own stdout:
    the leg resolved the owner's database and ran `CREATE TABLE IF NOT EXISTS` on it. NOT
    established: that any row changed — `query_artifacts` (1268) and `query_sessions` (1031)
    were flat across the run that found it, and a node process holds the same file open, so
    WAL mtime cannot attribute anything either way. Post-fix the leg reports
    `source=settings` against a temp path, the tripwire records zero owner-data opens, and
    `~/.topos/database.db` is unchanged in size and mtime.

- `[O]` **Both of this repo's privacy safety nets turned out to be advisory: the 305-test
  privacy battery ran nowhere a developer runs, and the live-database guard warned instead
  of stopping. Neither was a missing check — both checks existed, worked, and had caught
  real leaks. They were simply not wired into anything that runs by default.**
  - **The battery was deselected from `just test` and `just gate`.** Both recipes select
    `-m "public and not e2e and not live and not qq_eval"`, and every file under
    `tests/evals/privacy` is auto-marked `private` by `tests/conftest.py`'s
    `PRIVATE_PATH_HINTS`, so all 305 were deselected: the black-hole leak probes, UAR/CER
    zero-leak, minimality, the negotiation ratchet, dense sparsification, redaction
    idempotence. Only `ci.yml` reached them, by naming the path. These are the most
    productive tests in the repo — they found the black-hole existence leak, the SG1
    access-mode ceiling gap and both of Q7's roster leaks — and they cost 42 seconds, and
    the local gate was green on a machine where the privacy plane was red. Both recipes
    now run them as a second pytest session via a new `just test-privacy-battery`.
    Deliberately NOT by re-marking them `public`: `public` decides what an OSS fork's CI
    runs, so flipping it would drag 305 internal-fixture tests into that lane as a side
    effect. **`just test` goes from 3700 tests to 4013** — 3708 in the public lane (3700
    plus the eight this entry adds) and 305 in the battery, 466s + 42s, both green.
  - **A lane that only exists in CI is a lane developers discover by breaking it**, so
    `tests/test_local_gate_composition.py` now fails when a pytest target in `ci.yml` stops
    being reachable from `just test` or `just gate` — instead of leaving the next drift to
    be noticed on a red push, which is how this one survived.
  - **The live-DB guard raised only under `TOPOS_TEST_DB_GUARD_STRICT`, and nothing set
    it.** No lane, no recipe, no workflow, no doc — so `tests/live_db_watch.py` recorded
    owner-database opens and then let them through, and the only enforcement was a
    session-end report. A report cannot stop the write it describes. `addopts`' `-m` filter
    is last-one-wins, so a single explicit `-m` (`-m ""` will do) re-selects the live lanes;
    that is how 18 rows reached the owner's `query_artifacts` on 2026-08-19 while the
    session still exited 0. **The guard now refuses the connect by default**, before
    `sqlite3` is handed the path, naming the test, the file, the six-frame origin chain and
    the way out.
  - **The opt-out is explicit, visible, and set by nothing in this repo.**
    `TOPOS_TEST_ALLOW_OWNER_DB_WRITES=1` downgrades the refusal to recording. It is worth
    having, because refusing costs information: the run dies on the FIRST violation where
    the report lists every one. `test_no_lane_in_this_repo_sets_the_opt_out` fails if a
    recipe, workflow or script ever sets it, because an escape hatch wired into a lane is
    the old default wearing a new name. Neither opt-in lane needs it —
    `just test-owner-db-eval` exports `TOPOS_DATABASE_PATH` to the snapshot before pytest
    starts, so the modules that freeze the path at collection time freeze to the snapshot
    (verified: 32 passed, 3 skipped, zero refusals), and `just test-live-node` drives the
    node over HTTP, where the writes happen in a process this guard is not installed in.
  - **A marker is not consent.** `live`/`e2e`/`qq_eval` keep a test out of the default
    SELECTION and out of the session-end verdict; they no longer waive the refusal itself.
    That is precisely the gap 2026-08-19 fell through: every marker was present and
    correct, and a widened `-m` selected them anyway.
  - **Proved, not asserted.** A connect aimed at the real `~/.topos` tree was refused as an
    unmarked test, refused again as a `qq_eval` test selected by `-m ""`, and reached
    `sqlite3` only with the opt-out set. The target was a profile path that does not exist,
    so a regressed guard would have reached `sqlite3` and still created nothing — proving
    this against `~/.topos/database.db` would mean betting the owner's data on the
    assertion under test.
  - **The first thing the armed guard caught was a real one, and it had been invisible.**
    `mark_graph_dirty()` arms a 90-second `threading.Timer` that opens a database
    connection when it fires. Nothing cancelled it, so it fired during some later test,
    minutes after `_no_live_db_guard`'s `monkeypatch` had undone the `TOPOS_DATABASE_PATH`
    pin — resolved the path afresh, and landed on the developer's real
    `~/.topos/database.db`. Ordinary ingestion and enrichment tests arm it as a side effect
    of doing their work, and the graph refresher swallows every exception ("refresh must
    never die"), so the write left no trace in the run that caused it. `conftest` now
    cancels any pending debounce at `pytest_runtest_logfinish`, where engine state
    outliving its test is already checked, and the timer thread is named
    `topos-graph-refresh-debounce` — under `Thread-318` the refusal read as an anonymous
    thread inside `tests/storage/test_connection_tuning.py`, through a call stack that
    test does not contain.

- `[E:query]` **Q7's participant roster handed a grantee two identifiers the rest of the
  answer had already withheld: a stable pseudonymous join key, and a phone number served
  as a display name.** Both were reachable on grants the registry ships today, and both
  were in the same eight lines of `_thread_participants`. Neither was a raw-text leak;
  each was a grantee receiving an identifier every other plane in the pipeline treats as
  the owner's.
  - **`entity_id` was attached unconditionally.** `label` was correctly gated on
    `nameable = owner_view or (policy_active and entity_id in accessible)`; `entity_id`
    was set one line above it with no gate at all. On the default `messages:read` grant —
    selector policy OFF, so the grantee may not be told anyone's name — the roster read
    `{"kind": "person", "entity_id": "ent-sam"}`, an id appearing in **no** disclosed
    summary. A production entity id is `ent_{uuid4().hex[:16]}` and carries no name
    information, so this is a LINKABILITY leak rather than a content one: the grantee can
    count distinct counterparties, watch who recurs across sessions, and — the moment the
    same id appears in an `accessible_entity_ids` list on any other grant they hold —
    resolve the pseudonym and retroactively de-anonymize every roster that carried it.
    Withholding the name while handing over a stable join key is not withholding. The
    field is now `owner_view`-only, which is the rule Q1 already applies to it in
    `_attach_commitment_report` — the same rule, not a third one.
  - **`label` could be a raw phone number.** `_sender_display` ends in
    `cache[sender_id] = name or sender_id`, which is right for owner prose and wrong for
    a roster: on a real entity-scoped grant (`resolve_scope_manifest("messages:read",
    filter_manifest={"accessible_entity_ids": [...]})`) a GRANTED person with no
    `display_name` was disclosed as `{"label": "+15550001"}`. The grant licenses a NAME;
    it does not license the identifier that stands in when there is no name.
    `_thread_speaker` now returns `label` as **a name or the empty string**, carrying the
    identifier separately, and the roster falls back to it only at `owner_raw` — an owner
    is not blinded to their own contact, and no tier below them is told the number.
  - **The scrubber owned the right key and never walked the container.** `"label"` has
    always been in `disclosure._GRANTEE_TEXT_KEYS`, but `_scrub_grantee_text_items` was
    applied over four top-level LISTS (`summaries`, `scores`, `semantic_hits`, `facts`)
    and `topic_thread` is a DICT — so the policy covered a container the enforcing code
    never reached. `_GRANTEE_NESTED_TEXT_CONTAINERS` now declares the dict-shaped
    artifacts and their item lists explicitly, so a new one has to be listed and the
    listing is the review. The other two P4 blocks were enumerated with it:
    `commitment_report.goals[]` is ids, slugs, counts and timestamps (listed anyway, so a
    text field added later is covered by default) and `time_window` is a flat dict of
    dates, integers and closed-set slugs with no item list to walk. **`topic_clusters`
    was found unvisited by the same audit** — a cluster carries `label`, the key was in
    the tuple, the container was simply never named — and joins the top-level list here.
  - **Why 45 green tests said it worked, and what changed about the tests.**
    `test_a_grantee_learns_the_count_and_not_the_name` asserted
    `all("label" not in p for p in people)` — a deny-list of exactly one key, the one its
    author was thinking about — and never looked at `entity_id`. The new
    `TestTheRosterEntryCarriesOnlyWhatItsTierAllows` asserts an **allow-list over the
    whole entry** at each tier (`{kind, label, entity_id}` for the owner,
    `{kind, label}` for every tier below including a granted one), so a field added later
    fails until somebody states which tier may see it. Nine tests added; five of them
    fail against the unfixed source and four are controls — the owner still sees an
    unnamed contact by their identifier, a named granted person is still named, and the
    owner's packet passes through the new disclosure walk untouched.

- `[E:query]` **Q1's per-goal report named its citation list `evidence`, which is a
  RESERVED artifact key — so every turn the commitment lane answered died, and 21 green
  tests said it worked.** `_attach_commitment_report` wrote `entry["evidence"]`;
  `evidence` is in `FORBIDDEN_ARTIFACT_KEYS`, and `_execute_turn` runs
  `validate_public_result` over `public_result` **recursively** with no `try`/`except`
  around it. The first goal the lane kept therefore raised
  `ValueError: public_result contains forbidden keys: ['goals[0].evidence', …]` out of
  `QueryPipelineOrchestrator.execute()` — not a degraded report, **the whole turn**,
  including the summaries the owner would have received had the feature never shipped.
  Both tiers were down; the crash is tier-blind. Renamed to `evidence_records`, which is
  the same pointer list under a name the artifact contract does not reserve.
  - **The reserved key is aimed at evidence BLOBS, and this list is not one.** Each entry
    is `record_id` / `canonical_table` / `event_at` (plus an owner-only `entity_id`) — ids
    that point *at* `summaries`, never a second copy of their text. The ban is right and
    stays; the name was wrong. `evidence_count`, `status: evidence_found` and the
    `commitment_*_evidence_*` empty-reason slugs are untouched: they are not keys the
    contract reserves, and the mode's vocabulary should stay the owner's vocabulary.
  - **Every one of Q1's 21 tests called `DefaultSignalRetrievalAdapter.retrieve` and
    stopped there.** That is the right level for the join and for severing the planes, and
    it cannot see this class of defect at all, because the contract that was broken lives
    two layers above the adapter — past the disclosure filter, the minimizer and the game
    layer. A mode is not shipped when its retrieval lane is green; it is shipped when a
    **turn** survives.
  - **Nine new tests drive `QueryPipelineOrchestrator.execute()`, the production entry
    point** (`TestTheModeSurvivesTheTurnItShipsIn`, `TestTheGranteeTurnSurvivesItToo`).
    The owner turn returns `live_query` and carries `commitment_report` in
    `public_result` with the kept goal's source record and the dropped goal's empty-cause;
    the grantee turn does the same at `default_disclosure` and still gets `entity_count`
    rather than `entity_ids`. Two of them assert the **contract** rather than the field
    name, by calling the very `validate_public_result` the pipeline calls — so the next
    field added to this report that collides with a reserved key fails a test instead of
    taking a live turn down. **Acceptance: watched fail, then fixed.** With the key
    restored to `evidence`, all nine fail at `session_utils.py:13`; with the rename, 36
    pass.
  - **No ranking effect, by construction.** `_attach_commitment_report` runs at the end of
    `retrieve()` and writes exactly one packet key. No scorer, weight, fusion order or cap
    is touched, and no eval catalog case or expected output was added or edited.

- `[E:query]` **The access-mode ceiling had no test, and 336 privacy tests did not
  notice.** `disclosure.py`'s two mode blocks — summary mode dropping `rows`, inference
  mode dropping `rows` / `summaries` / `content` / `messages` — decide *which artifacts*
  a grantee receives at all. Everything the privacy battery held guarded the layer
  underneath: what a grantee may see *of the artifacts they are already allowed*. Sever
  both blocks and the whole battery still passed, while an adversarial sweep's probe
  reported `rows present: True | row contents: [RAW OWNER SENTENCE]` to a grantee holding
  a summary-mode grant. **No behaviour changed here; the code was already correct.** It
  was simply unguarded, which is the worse of the two states — nothing would have told us
  when it stopped being correct.
  - **Three tests, in `tests/evals/privacy/technical/test_grantee_summaries_scores_filter.py`.**
    (a) summary mode: `rows` is absent from the packet and `summary_mode_strip_raw` is in
    `filters_applied`. (b) inference mode: none of the four evidence keys is present and
    `inference_mode_strip_evidence` is in `filters_applied`. (c) a positive control: a
    label-only stand-in that appends the receipt and strips nothing must **fail** (a) and
    (b). Without (c) the first two are satisfiable by a no-op that lies in its own receipt
    — `filters_applied` is a self-report, and a packet claiming to have been filtered is
    exactly the failure class this programme keeps finding. (a) and (b) call the same two
    assertion helpers the control does, so the control exercises what those tests actually
    assert rather than a paraphrase of it.
  - **Non-vacuous by construction.** The fixture carries a real `rows` list, real
    `summaries`, and the `content` and `messages` keys, because "`rows` is absent" asserted
    against a packet that never had rows is a test that cannot fail. The canary sentence
    deliberately contains no email and no phone number, so the PII redaction the rest of
    this file guards would leave it fully legible: the ceiling is the only thing standing
    between it and the grantee.
  - **Acceptance: watched fail, then restored.** With both blocks at
    `disclosure.py:136-146` severed, the privacy battery reports `2 failed, 303 passed` —
    the two new ceiling tests and nothing else — with `AssertionError: summary mode
    delivered raw rows: [{'_table': 'conversation_messages', 'content': 'zx-canary-sg1 …'}]`
    and the matching `inference mode delivered evidence under 'rows'`. Restored to an empty
    `git diff`, all 305 pass. That battery is deselected from `just test` and `just gate` by
    their `-m "public and …"` filter, so it is run by naming the path, the way CI does.

- `[E:query]` **A node that had never ingested anything 500'd on its owner's first
  question.** `relationship_context:read` declares `canonical_tables:
  ["conversation_messages"]` (the entry above) and the entity-mention lane is bounded by
  that list (the release before it). Neither probed that the table exists. It is created
  by the writer that first lands a message in it, so a standard init produces 77 tables
  and this is not one of them — retrieval walked the manifest straight into
  `canonical.list()`, which built `SELECT COUNT(*) FROM (…)` over a table SQLite has
  never heard of and died at `storage/adapters/sqlite/stores.py:449` with
  `sqlite3.OperationalError: no such table: conversation_messages`. On the **owner**
  path, where nothing is filtered and no grant is involved. Found by an adversarial
  sweep's severing probes, not by a test: every test in the tree that reaches this scope
  seeds a message first, so every one of them creates the table on the way in.
  - **A declared table that does not exist yet is an empty store, not a fault.**
    `_canonical_table_absent` reads `sqlite_master` deliberately and the lane contributes
    no rows for a table that is not there. It is **not** a `try`/`except` around the read:
    a disk error, a locked database or a malformed row still reaches the caller as the
    failure it is, asserted by `test_a_broken_read_is_not_reported_as_an_empty_store`. It
    is also narrow the other way — existence that cannot be *established* (a non-SQLite
    adapter, an unreadable catalog) counts as present, because an unknown must not
    silently disable a lane, the mirror of the rule `_scope_supply_state` already states
    for diagnoses. The probe sits in `_list_canonical_rows`, the one funnel every
    canonical lane goes through (scope routes, the entity-thread lane, the employer
    heuristic), so a future lane cannot reach around it.
  - **The owner gets an answer that explains itself, in the vocabulary that already
    exists.** The absence is recorded on the narrowing ledger as `store_empty` /
    `connected_never_delivered`, so the empty result carries a cause instead of arriving
    as a silent nothing — which is the false-absence bug the taxonomy was built to end and
    which a "does not raise" test would have accepted. **Why that sub-cause of the three:**
    `delivered_then_emptied` is excluded by the evidence itself — the writer that creates
    the table has never run, so nothing was ever delivered and then removed.
    `no_source_connected` is a claim about the *install set*, not about this store, and it
    is the more actionable of the two remaining: it sends the owner off to add a connector.
    Saying that to an owner who has connected one and is merely pre-first-sync is the same
    false absence in a new coat, and `_scope_supply_state` already refuses the symmetric
    guess for the symmetric reason. What the missing table evidences first-hand is that
    this store has never received a delivery. `no_source_connected` stays reachable
    unchanged for every scope whose tables the migrations do create. The table's name rides
    in local-only `detail`; the public serializer carries closed-set slugs only.
  - **Red first, on both tiers.** `tests/query/test_fresh_node_absent_canonical_table.py`
    fails 7 of 13 against the pre-fix code — `test_it_does_not_raise[owner_raw]` and
    `[default_disclosure]` with the verbatim `sqlite3.OperationalError` above, and the
    cause/sub-cause assertions with it. Two premise tests guard the setup (a standard init
    really does omit the table; the scope really does declare it), and two controls guard
    the fix from itself: a table that exists is still read, and an existing store is never
    labelled never-delivered. 3562 tests in the public lane and the 301-test privacy
    battery (run by path — it is deselected from `just test`) pass.

- `[O]` **The documented test gate wrote to the developer's own database.** `pytest tests/`
  was not hermetic: it opened `~/.topos/database.db` read-write, and on 2026-08-19 a single
  run inserted 71 rows into the owner's `query_artifacts` and reported two
  environment-dependent 500s from the node on `:9000` as suite failures.
  - **The markers were doing half a job.** `live` / `e2e` / `qq_eval` exempted a test from
    the `_no_live_db_guard` fixture, but nothing *deselected* them, so the ordinary gate ran
    the whole set against the owner's data. `pyproject.toml` now carries
    `addopts = ["-m", "not live and not e2e and not qq_eval"]`. Naming a file or a node id
    does **not** opt you in — only `-m` does, deliberately, because a script or an agent
    reaching that database by muscle memory is exactly how this went unnoticed for three
    weeks. A conftest hint prints the way back in when everything you selected was
    deselected.
  - **The fixture could not have covered it anyway.** It pins `TOPOS_DATABASE_PATH`, and
    five modules resolve the path themselves —
    `LIVE_DB_PATH = Path(os.environ.get("TOPOS_DATABASE_PATH", Path.home() / ".topos" / ...))`
    evaluated at COLLECTION time, when the variable is still unset, then handed to
    `AdapterFactory` as `db_path=`. `tests/live_db_watch.py` closes that from the other end:
    it wraps `sqlite3.connect` for the whole session, records every read-write open of a real
    `~/.topos` (the tree, so archived profiles count) or legacy database, and reds the run
    naming the test, the path, and six frames of blame. `mode=ro` and `immutable=1` are not
    recorded — reading owner data is what the live evals are for and it changes nothing on
    disk.
  - **A third shape nobody had described, found by that guard's first full run.**
    `tests/features/test_p3_entity_spine.py` held
    `MESSAGES_MANIFEST = resolve_scope_manifest("messages:read")` at module scope. Resolving
    a manifest reads as a registry lookup, but `manifest_from_scope_entry` asks
    `get_sources_by_scope` which installed sources back the scope, and that reads
    `source_runtime_installs` through `core.state.get_db_connection()`. At module scope it
    ran during **collection** — where no fixture exists to redirect anything — so the unset
    path resolved to the developer's database before the first test started. Made lazy, and
    `test_no_test_module_reaches_the_database_at_import_time` now walks each module's AST for
    database-reaching calls at import scope (module body, class bodies, and decorator
    arguments, since a `parametrize` argument is evaluated during collection like any other).
    Second-order trap worth stating: once `core.state.db_conn` is cached, later calls reuse
    the handle and open nothing, so one recorded open is not evidence of one offender.
  - **The lanes that do need real data are named and disposable.**
    `just test-owner-db-eval` takes a point-in-time online backup
    (`scripts/snapshot_owner_db.py`, SQLite's backup API rather than `cp` — the node runs WAL
    and a byte copy can miss committed pages, and a torn snapshot reads as a retrieval
    regression) and points the lane at it: the reads are real, the writes are thrown away.
    `just test-live-node` is the `:9000` lane, whose database no environment variable in the
    pytest process can redirect. Both documented in `docs/testing/TEST_LANES.md`; README and
    CONTRIBUTING no longer print a filter that lets these through.
  - **Measured.** Three full `pytest tests -q` runs: 3883 passed, 6 skipped, 32 deselected,
    **zero** owner-database opens, `query_artifacts` unchanged at 1518 rows.

- `[O]` **The latency script was reporting the test suite's latency as the owner's.** The
  same leak filled `query_artifacts`: 1473 of its 1518 rows were harness sessions, and
  **100% of the 85 rows carrying a `duration_ms` were synthetic**, so
  `scripts/query_latency_percentiles.py`'s engine series described the eval harness while
  reading as a statement about this person's node. Those rows are permanent, so they are now
  excluded by session-id prefix (`HARNESS_SESSION_PREFIXES`) with the excluded count printed
  beside the sample count — a filter nobody can see is indistinguishable from missing data.
  The honest reading of that series today is `samples=0`, and the script says so in those
  words rather than letting an absent number imply an un-instrumented system.

- `[E:query]` **A denial returned no reason, and the guard that should have caught it was
  written so that it could not.** `deny_reason` has been on the field contract's
  `required_return` list since the contract existed. Nothing anywhere covered it.
  - **The drop.** The handler's `ManifestValidationError` path is the one denial that
    returns above the orchestrator, so it builds its whole response from a literal
    allow-list. It emitted four of the eight declared return fields. The control plane
    then recovered `deny_reason` only out of the `audit` block — which that path never
    writes, because there is no orchestrator to build one. An owner who asked on an
    ungranted or misspelled scope got a refusal with no why, which reads exactly like a
    scope that holds no data. Canonical location is now written down in the contract
    (`response.deny_reason_location`): TOP LEVEL, where all six pipeline denials already
    put it, with `audit.deny_reason` as the fallback the control plane reads second.
  - **The two fields the denial did not carry.** That same allow-list omitted
    `query_session_id` and `narrowing`. The path now writes its own ledger —
    `empty_cause: scope_denied`, one `grant` entry whose `reason` is the validation code
    — so a denial explains itself the way every other denial in the engine does. The
    codes are closed-set slugs and `record` re-slugs them regardless; a test sends a
    question full of names and asserts none of it reaches the ledger.
  - **Why the guard was blind.** All three repos checked the return path against literals
    spelled in the test file: the engine grepped its handler source for two of the eight
    declared fields, the front end read the contract for the request half and nothing for
    the response half, and the control plane's single fixture set `deny_reason: ""` under
    a `if not value: continue` skip — so the one field with a falsy fixture was the one
    field with a live drop. Each repo's return guard now reads `required_return` off the
    contract file, runs two real payloads (a live turn and a manifest denial, because no
    single response can carry both `public_result` and `deny_reason`), and fails if any
    fixture value is falsy or any declared field is exercised by neither. Verified by
    adding a fictional field to `required_return`: red in all three.
  - **Exemptions are argued in the protocol, not skipped in test code.** `scope_id` and
    `access_mode` are echoed by the transport, not emitted by the engine. That is now
    `response.originates` in the contract, with a reason per field and a test in two
    repos that an exemption naming a field outside `required_return` exempts nothing —
    the same shape the request half's `consumed_before_handler` already had.

- `[E:query]` **The hop that actually delivers `retrieval_text` and `retrieval_parts` was
  the one hop no test executed.** Coverage stopped at the orchestrator's keyword arguments
  on one side and resumed at greps of the retrieval module's source on the other. Between
  them sits `pipeline.py` building the `RetrievalRequest`, and severing
  `needle_parts=needle_parts or None` there left 592 query tests green: the field arrives,
  the pipeline drops it, and the per-part rare gate silently reverts to one flattened
  needle set — the exact production behaviour multi-needle was added to end. A source grep
  cannot see it, because the source still says `needle_parts`. The new end-to-end test
  drives a real `type: "query"` message through a real orchestrator on in-memory adapters
  and watches both landing sites: the `RetrievalRequest` the pipeline hands to retrieval,
  and the call to `_needle_token_groups` where the gate turns needles into the groups it
  vetoes on. Both severings — `needle_text` and `needle_parts` — were run and each turns
  exactly two of the new tests red while the other 597 stay green.

- `[E:query]` **Four planes the rest of the engine treats as total each had a lane running
  outside them.** A partial boundary reads exactly like a whole one from the call site,
  which is the property all four share and the reason none of them showed up as a failure.
  - **The mention pointer ignored the manifest.** `entity_context_items` took no manifest
    at all, so its mention lane offered whatever canonical table the entity graph happened
    to know about. Measured: a request on `availability:read`, whose resolved manifest
    declares `canonical_tables == []`, came back holding `{'topic': 'Anthropic in
    conversation_messages', 'record_id': 'msg-thread-1'}`. A mention row is a POINTER — it
    names a table and a record id without ever reading the record — and that is still a
    disclosure: it says the record exists, which table holds it, and when it happened. The
    lane now scans the manifest's tables and no others (the bound
    `_load_entity_thread_items` already applied one join further on), and the pointer it
    emits carries the keys the other planes match on: `canonical_table`, which an exclusion
    category tests, and `object_type`/`disclosure`, which the tier filter tests and which
    mention items declared neither of, so they crossed the tier untouched. `entity_mention`
    is registered against the `entity_dossiers` grant rather than falling to the default. A
    mention whose `canonical_table` is NULL is dropped rather than offered to every table:
    the thread lane can carry an untabled mention because a record-id intersection against
    already-disclosed rows decides its membership, and a pointer has no such second opinion.
  - **`must_not_retrieve` bound one access mode out of three.** `raw` applied a scope's
    declared restrictions to its rows and `inference` to the whole packet; `summary` — the
    mode most scopes actually answer in — never applied them. `availability:read` is the
    live case: it declares `calendar_events.title`, `conversation_messages.content` and
    `content`, its ceiling is `inference`, and `MODE_RANK` puts `summary` below that
    ceiling, so the reachable mode was the unenforced one. Applied to the whole summary
    packet, as `inference` does, so it covers the fusion lanes that grow later.
  - **One canonical read out of nine used the owner's tier.** `_list_canonical_rows`
    defaults `disclosure_tier` to `owner_raw`; the legacy employer heuristic over
    `profile_records` was the only call site taking that default, so a grantee's
    work-context ask read that table at the owner's tier — past the NSFW row exclusion and
    the disclosure-column swap the other eight get for free. It now passes the request's
    tier. (On SQLite `profile_records` has no disclosure spec, so the wrong tier was inert
    there and live in the in-memory store; the test asserts on the argument for that
    reason.)
  - **The cohort rollup skipped the exclusion plane in silence.** `_cohort_aggregate_bundle`
    returns from `retrieve` before the enforced-exclusion call at the foot of the method,
    so "…but nothing from my journal" left no trace: not enforced, and not reported as
    un-enforced either, which is indistinguishable from an honoured exclusion. It is
    deliberately NOT routed through the item filter — the packet holds one derived count
    over cohort membership computed before any row exists, so the filter would match
    nothing, report `enforced=true, dropped=0`, and leave the count still counting the
    excluded members. `enforce_request_exclusions(..., aggregate_only=True)` reports
    `requested=true, enforced=false` with every compiled target counted as un-applied, and
    records `stage=disclosure, action=not_applied, reason=exclusion_aggregate_unfilterable`.
    The compiled targets go in `detail`, which does not leave the node.
  - **The black hole's source wire had no property of its own.** The fix has two wires: the
    source filter in `_load_entity_thread_items` and the exit filter at the packet
    boundary. Every existing assertion is a LEAK assertion, and the exit filter satisfies
    all of them alone — severing the source wire leaves the packet clean and all 34
    entity-thread tests green. It is not a redundant copy: it is what stops the lane
    recording, in the PUBLIC ledger, that it found rows for the entity before the exit
    filter removes them, converting hiding-by-absence into hiding-by-denial. Verified by
    severing it: the packet stayed clean and the public ledger gained `{stage: retrieval,
    action: contributed, reason: entity_thread_lane}` for a protected entity. The wire is
    now `_blackhole_filter_thread_mentions`, a named seam a test can sever, and the ledger
    property is asserted against both the intact and the severed wire.
  - **A fallback that would have disabled the new bound.** The `except TypeError` around
    the `entity_context_items` call guarded a pre-M1 `linking` that cannot exist in this
    tree, and it caught exactly the error a missing `manifest=` raises — so the day the
    bound was added it would have turned a required argument into a silently unbounded
    lane. Removed, and the test that pinned it replaced by one pinning that the call site
    hands linking the request's manifest.

- `[E:query]` **A black-holed entity still existed, if you asked the entity lane.** The
  P4 lane reaches records through `entity_mentions` — by construction it returns rows
  whose text never names the subject — and the only black-hole filter on the way out was
  a NAME SCAN. So a grantee asking about a protected entity got a summary item back with
  `record_id`, `entity_id`, `canonical_table` and `event_at` all intact and only the body
  withheld. The disclosure tier was holding perfectly; the black hole was not. That is
  the one outcome D5 rules out — a protected entity must be indistinguishable from one
  that was never stored, and this told the caller it exists, which records it owns, in
  which table, and when. `BlackholeGuard.blocked_record_ids()` had the exact answer and
  had no production caller anywhere in the engine.
  - **Filtered by id, at source.** `_load_entity_thread_items` now subtracts
    `blocked_record_ids()` from the mention set before it reads any canonical row, so
    for a non-owner the protected records are never resolved. Records are filtered
    rather than entities on purpose: a record linked to both a protected entity and a
    visible one arrives under the visible entity's id and would sail past an
    entity-level drop.
  - **The name scan is now the backstop, not the floor.** `_blackhole_policy_for_summary`
    matches `record_id`/`entity_id` first and falls back to scanning text, which keeps
    the lanes that have no source to filter at covered.
  - **The owner keeps their rows and they are stamped.** Entity-lane items now carry
    `blackhole_protected: true` for `owner_raw`, matching their `entity_dossier` and
    `entity_mention` siblings. The control plane cannot detect protected content itself;
    an unstamped row is an untainted row.
  - **The receipt was leaking too.** The narrowing ledger recorded
    `stage=disclosure, action=dropped_items, reason=blackhole_policy, dropped=N` and,
    when it emptied the lane, `empty_cause=scope_denied`. `as_public()` leaves the node,
    so that line told a grantee in a guaranteed-meaning slug that something about the
    entity they asked for was being withheld — hiding-by-denial in place of
    hiding-by-absence, with a count attached. Non-owners now get a debug line that stays
    on the node; the owner, whose rows are stamped rather than dropped, is unaffected.
  - **The BHLR battery was structurally blind to all of it.** Every reader in the battery
    read storage and applied the guard itself, so no reader had ever run a query: a live
    existence leak through the retrieval path sat under a green BHLR = 0. The battery
    gains a `query_retrieval` surface that drives
    `DefaultSignalRetrievalAdapter.retrieve()`, and the corpus gains a P4-shaped record —
    linked to the protected entity by mention only, text that never names it, and its
    RECORD ID planted as a canary token in its own right, because the identifiers were
    what leaked while the body was correctly withheld.
  - **…and that new surface was still blind, one layer down.** A reader that calls
    `retrieve()` is not the same as a reader that reaches the lane. The corpus seeded its
    entities at the column default `mention_count = 0` and its rows under
    `source_id = 'src-a'`, and either alone is disqualifying: `link_query_entities` reads
    `FROM entities WHERE mention_count > 0 OR contact_id IS NOT NULL`, so nothing linked
    and the P4 lane never ran; and `resolve_retrieval_source_ids` intersects the caller's
    installed sources with the scope manifest's own defaults and *silently falls back to
    those defaults* when the intersection is empty, so every canonical lane read sources
    the corpus had never written. Severing both black-hole wires in `retrieval.py` — the
    exact pre-fix state — left the battery at 26 passed, 0 failed. BHLR = 0 was a
    statement about an empty packet. The corpus now backfills `mention_count` from its own
    mention rows and seeds under a real connector id, and under the same severing the
    battery goes red and names `query_retrieval` as the leaking surface.
  - **The blindness is now itself a failure.** Two probes assert the lane RAN rather than
    that it leaked nothing: the owner's packet must carry an `entity_thread` item for the
    mention-linked record whose text names nobody, stamped `blackhole_protected`; and no
    non-owner caller may receive that `record_id` or `entity_id` on any item. An empty
    packet leaks nothing, so a leak gate can never notice a surface going quiet — only a
    non-vacuity assertion can.

- `[E:query]` **An entity mention pointer shadowed the record it pointed at.**
  `entity_context_items` emits a pointer item (`"2026-03-13 — Anthropic"`) carrying the
  record's id for a record it never reads. `_fusion_item_key` collapsed it with the
  record itself under `rec:{id}`, and `_rrf_fuse_summary_lists` keeps the FIRST lane to
  claim a key — `entities` fuses ahead of `canonical`. So whenever a mention happened
  to point at a row the canonical lane also returned, the owner's actual sentence was
  replaced in the packet by the surface text and the date. The id was present, the
  content was gone, and `fusion_sources` listed `canonical` the whole time. Mention
  pointers now take their own fusion key, so the pointer and the record coexist. Third
  instance of a documented pattern (`contact_identifiers`, `user_goal`) and the same
  remedy; found because the entity-thread lane above was shadowed identically and
  appeared to contribute nothing.

- `[E:query]` **The rare-token gate was dead on multi-part requests; it now runs per
  part.** One needle set derived from a whole multi-part request cannot gate any
  individual part: `_rrf_fuse_summary_lists` vetoes when *any* needle is unevidenced,
  so for "1) how did the Threnody-7 rewrite go 2) how did I sleep" the word
  `threnody` — which the corpus does not contain — emptied the sleep lane too. The
  consequence is not a degraded gate but an absent one: on a multi-part ask it fires
  for every part or none, so the sections that most need protection from filler are
  exactly the ones it can never fire for correctly. `RetrievalRequest.needle_parts`
  (pipeline and wire `retrieval_parts`) carries the needles split per part;
  `_needle_token_groups` tokenises each part and `_rare_token_groups` prices them
  against **one** df pass over the union, so N parts cost one FTS pass, not N. Both
  gate sites are per part now — the fusion veto and `_route_canonical_rows`'s browse
  suppression — and a request is vetoed only when *every* part is. A part carrying no
  needles (a pure date-scoped "my week") is never vetoed: the window alone narrows it.
  - The two-field separation is untouched. Parts are needles only; the planner, the
    embedding and the scope classifier still get the owner's sentence, and the
    absolute-date framing rules (`_MONTH_TOKENS`, year and day-number handling) apply
    inside each part exactly as they did to the whole request.
  - A partial veto is now a *narrowing*, not an empty: the ledger records
    `rare_gate_partial_veto` with the vetoed part indices and no longer declares the
    turn empty when other parts answered. Tokens stay in local-only `detail`; the
    public entry is closed-set slugs and integers.
  - No parts — every caller today — yields exactly one group and is token-for-token
    what the single-needle path did. No migration.

- `[E:query]` **The per-part rare gate had no caller; it does now.** The entry above
  shipped `RetrievalRequest.needle_parts` and the wire field `retrieval_parts`, and
  closed on the sentence "No parts — every caller today". That stayed literally true:
  nothing anywhere sent the field, so every multi-part request still arrived as one
  flattened needle set and the per-part veto was off in production on exactly the
  requests it was written for. A correct mechanism with no sender is an absent
  mechanism, and the release note read as if the bug were fixed. The senders exist
  now, end to end, on both entrances:
  - **Front end.** `buildRetrievalQueryParts` (`lib/mcp/scopeQueryRouter.ts`) segments
    with `segmentRequestParts` — the *same* function the coverage map labels sections
    with, so the sections synthesis names and the sections the gate reasons about
    cannot disagree — and `buildQueryToolArguments` puts them on every scope query the
    turn issues. It segments the **pre-cap** text: sections are precisely what the
    500-character cut removes.
  - **Control plane, both paths.** `prepare_engine_query_payload` rebuilds the engine
    request field by field, so a field it does not name dies silently there; it names
    `retrieval_parts` now. The home-chat path reaches it through the `query_scope` /
    `shared_query_scope` MCP tools, the scheduled path through
    `scope_query_router.build_query_tool_arguments` → `forward_routine_tool`. The
    weekly report is the motivating case and it goes through the second path.
  - **The preamble is not a section.** "Using my journal entries, write me a report:"
    distills to instruction words with no rare token, `_veto_for` returns no verdict
    for an empty needle set, and — because the full veto requires *every* part to be
    vetoed — one unvetoable part turns the veto off for all the real ones. Both
    segmenters drop it, and fewer than two surviving parts sends nothing at all,
    which is byte-identically the pre-change path.
  - **Two-field separation untouched.** Parts are needles; `query` remains the owner's
    sentence for the planner window, the embedding and the scope classifier. Asserted
    at each hop rather than assumed.

- `[E:protocol]` **The query field contract now covers `retrieval_parts` and
  `resource_id`, and its exemptions are declared rather than hardcoded.**
  `topos/protocol/query_field_contract.json` is what the three seam guards read, so a
  field absent from it is a field no guard can miss. Both are declared in
  `required_forward` now. The engine's own guard used to carry a bare
  `if field == "dataset_id": skip` — an exemption with no stated reason, which is how
  the next one gets added silently; exemptions live in a `consumed_before_handler`
  block with a reason each, and a test asserts every exemption names a field that is
  actually in the contract and gives a non-empty reason.
  - Found while wiring the guards: the control plane's `shared_query_scope` tool
    accepted neither `retrieval_text` nor `retrieval_parts`, while the front end's one
    call builder emits them for shared routes too — so a grantee turn lost the subject
    at the *tool signature*, one seam before the allow-list that usually gets blamed.

- `[E:query]` **`empty_cause` may not contradict the payload it travels with.**
  `_attach_narrowing` published the ledger's turn-level verdict unconditionally,
  but a stage can empty its own lane without emptying the turn: the rare gate
  returns `[]` for evidence while the derived lanes still produce summaries, so a
  response could arrive carrying `empty_cause: gate_vetoed` *and* a
  `public_result` with rows in it. The ledger and the result then tell two stories
  about one turn, and a consumer that believes the ledger — the front end's
  coverage map now derives its section labels from exactly this field — reports
  absent data that is sitting in the same response. That is the false-absence bug
  re-entered through the fix for it. The cause is now published only when
  `result_empty` is true, which is the condition `pipeline.py`'s other publish
  site already used. The emptying stage's ledger *entry* is unaffected; only the
  turn-level claim is withheld. No migration: serialization only, nothing stored
  changes shape.

- `[E:query]` **`retrieval_text` is part of a turn's retrieval identity, so it is
  part of its intent hash.** It steers the rare-gate needles and (since P2) the
  semantic query, so two calls sharing session + scope + mode + query but
  differing in `retrieval_text` retrieve different things — and used to collide
  in `compute_intent_hash`, at which point the artifact cache returned the first
  call's `public_result` verbatim as the second's, stamped `memory_hit`. A
  healthy-looking, per-section fabrication, strictly worse than an honest empty.
  Latent today; P3's per-section retrieval would have made it reachable, which is
  why the design review ordered this fix first. Absent or query-equal
  `retrieval_text` hashes byte-identically to the old formula, so every existing
  caller and every cached artifact keeps its key.
  - `needle_parts` and `time_windows` now extend that identity for the same reason
    and against the same hazard: P3 gives a request per-part needles and a
    differenced ask two windows, both of which change what comes back while leaving
    scope + mode + query + `retrieval_text` identical. Two report sections that
    differ only in their parts, or one week's comparison and the next's, would
    otherwise collide — and a collision here is not a stale answer, it is one
    section's findings served as another's under a `memory_hit` stamp.
  - Order is significant for both (parts in request order, windows
    baseline-then-current), and the fields are joined by a control character rather
    than punctuation, because `normalize_query` only lowercases and collapses
    whitespace — an owner sentence can contain `|`, and two different requests must
    never be able to forge one payload. A lone part that merely repeats the needles
    is the single-part request spelled twice and adds nothing. Absent — every caller
    today — leaves the payload byte-identical, so no cached artifact is orphaned.

### Changed

- `[E:query]` **The fourth text: embed the subject, not the instruction.** The
  planner strips *time* framing ("this week") and leaves instructional framing
  alone, so a structured request embedded its own instructions — measured
  2026-08-18, the weekly-report prompt sent all 315 characters of "generate a
  personal work report … summarize achievements … with any adjustments made" to
  the encoder, which is a vector query for the *shape* of a request rather than
  its subject. When the caller supplied `retrieval_text` the semantic query now
  uses it, and records `embedded_subject_not_instruction` in the ledger.
  - Self-limiting by construction: `retrieval_text` is only ever sent when
    distillation removed something, so a plain question is untouched and the
    sentence still reaches the encoder — which is what 2026-08-16 measured that
    it needs.

### Added

- `[E:query]` **Exclusion is enforced in the retrieval plane.** The product shows
  exclusion off as a headline capability — *"everything about my week, but nothing
  from the therapy journal"*, *"exclude anything involving Sarah"* — and the engine
  had nowhere for that sentence to become a decision. The only place it could
  plausibly have gone is the synthesis prompt, and a model that has been shown the
  journal entry and asked nicely to ignore it is not an enforcement mechanism: the
  entry is one paraphrase from the answer, and it has already entered the context
  packet, the stored artifact and the audit trail. `topos/query/exclusion.py`
  compiles the prose into a closed typed structure and applies it inside
  `retrieve()`, alongside the disclosure tiers, the black-hole policy and
  `_strip_forbidden` — the plane that already runs before synthesis — rather than as
  a second enforcement plane beside it. Excluded content is gone before the
  disclosure filter, the game layer, the artifact cache and the prompt.
  - **Closed vocabulary, or nothing.** A fragment compiles to a `category` (a slug
    over the scope registry's canonical tables, dimensions and source ids), a `tier`
    (a row-level content class that actually exists — today `nsfw`, via the ingest
    flag `exclude_nsfw_rows_for_grantee` already reads), or an `entity` resolved
    through the same entity plane the black-hole feature uses, filtered by both
    halves of it: the `entity_mentions` join and the normalized text scan.
  - **Ambiguity is a refusal to guess.** A fragment that compiles to none of those
    is reported as *not applied* on the packet (`exclusion.enforced: false`, plus a
    `not_applied` count) and in the ledger, and the answer is told. A name the entity
    plane cannot bind is *not* turned into a bare substring filter — that would drop
    rows on a common word and claim enforcement, which is worse than not enforcing.
  - The ledger records `disclosure / excluded / exclusion_{category,tier,entity}`
    with a dropped count, so the owner can see the exclusion was honoured and how
    much it removed; an exclusion that empties the turn declares `scope_denied`, the
    same call the black-hole policy makes, because "no data" would be a lie about the
    owner's own instruction. Public entries are closed-set slugs and integers: an
    excluded person is `named_entity`, never a name, and uncompiled fragments live
    only in the local-only `detail`.
  - Cache-safe by construction rather than by a new key: the exclusion is parsed from
    `query_text`, which `compute_intent_hash` already covers, so an unexcluded
    artifact can never be replayed for an excluded ask.
  - A request with no exclusion leaves the packet byte-identical and gains no block.
    The negation leads are deliberately explicit (a bare "no X" is not an exclusion),
    because a false positive here is silent under-retrieval on an ordinary question.
  - No migration: parse and filter only, nothing stored changes shape.

- `[E:query]` **A differenced question keeps both of its windows.** "What changed
  between last week and this week" resolved to a single time range, because
  `_relative_time_range` returns the *first* relative phrase it matches and stops —
  so the second period named in the question was discarded before retrieval, and the
  answer was drawn from whichever half the planner happened to see first. There is no
  honest difference to state from one window. `QueryPlan` now carries `time_windows`
  and `comparison_intent`: when the request uses a difference verb *and* names two
  resolvable periods, both are kept, sorted earliest-first so `baseline` is always the
  earlier one regardless of the sentence's order — otherwise "this week vs last week"
  would silently invert the sign of every finding. `time_range` becomes the union
  span, so every existing consumer of the single range still sees a range that covers
  the evidence. Retrieved summaries are stamped `time_window_label` (`baseline` or
  `current`), and the packet's `time_window` gains `comparison` and `windows`.
  - Deliberately the smallest thing that answers a differenced ask, not a temporal
    algebra: two windows, both relative, and only when a difference verb is present.
    "My work this week and last week" names two periods with no difference asked and
    stays on the single-window path.
  - `comparison_windows()` resolves the pair without a database connection, because
    the intent hash is computed before retrieval opens one and the hash and the plan
    must agree about which windows a turn is for.

- `[E:query]` **The source listing says which scopes nothing feeds, so a router can
  stop spending a slot on them.** `_scope_supply_state` explains an emptiness that
  already happened; a router has to decide *before* it commits one of its four
  route slots, and had no way to ask. `routing_supply_states` answers the same
  question from the registry and the installed set alone — no query, no data read —
  and `list_sources` now carries it as `scope_supply`.
  - It rides the source listing rather than a new endpoint: that response is
    already the node's answer to "what is connected here", supply state is a
    projection of the same fact, and a router forced into a second call would
    either skip it or route on a stale copy. Best effort — a projection failure
    costs the caller nothing but the key, and an absent key means "not stated",
    never "nothing is connected".
  - Deliberately narrower than `_scope_supply_state`, because this one is allowed
    to make a request retrieve *less*. Only `no_source_connected` is ever
    reported: `connected_never_delivered` and `delivered_then_emptied` describe
    connected feeds whose store is empty, which is a real empty answer and must
    still be queried. The three sub-causes are distinct on purpose and a
    router-facing map carrying all three would collapse them.
  - A scope with no feeds at all in the registry is left *out* of the map rather
    than called unsupplied. `attention:read` and `complexity:read` are computed
    on-node and register no feeding source, so "no feeds" here means "not knowable
    from the registry" — and an unknown must never cost a route.
- `[E:query]` **Why a scope holds nothing, not just that it does.** `store_empty`
  already separated "nothing has ever been stored" from "you had a quiet week";
  it did not say which of three things went wrong, and the remedies differ —
  connect a source, wait for a first sync, or find what emptied the tables. Only
  one of those is the owner's to act on, and the old answer sent them to the
  wrong one. The cause now carries `no_source_connected`,
  `connected_never_delivered` or `delivered_then_emptied`, read from
  `scope_source_generation` cross-checked against installed sources.
  - Which sources feed a scope comes from `get_sources_by_scope` — the static
    registry plus active runtime installs. A first pass used the manifest's
    `default_source_ids` and reported "no source connected" for a scope whose
    calendar connector *was* installed, which sends the owner to add something
    they already have.
  - An undeterminable state returns `None` rather than guessing. A wrong remedy
    is worse than none, because the owner acts on it.
- `[E:query]` **A declared field contract across the client → CP → engine path.**
  `topos/protocol/query_field_contract.json` names every field that must survive
  the round trip, in both directions. Five seams on that path rebuild their
  payload from hand-written allow-lists and two of them are on the way *back*, so
  a field can be declared at one end, sent faithfully, and vanish in the middle
  with nothing failing — `sourceRefs` and `retrieval_text` were each lost that way
  on 2026-08-17. The engine, the control plane and the front end now each test
  their own seam against this one file.

### Added

- `[E:query]` **A narrowing ledger, and four causes where "empty" had one
  message.** A request crosses eight stages over three codebases and six of them
  can make the search smaller; until now none of them said so. On 2026-08-17 ten
  independent defects were found in one afternoon, every one of them returning a
  well-formed result and not one of them logging a warning. The most useful
  diagnostic available all day was `stores_touched` missing the string
  `"signal"` — a field that exists for unrelated reasons.
  - **Every narrowing stage now appends `{stage, action, reason, dropped}`** to
    an optional `narrowing.NarrowingLedger`, threaded by reference. The planner
    records the window it scoped to and the rows the soft window set aside; the
    rare-token gate records that it vetoed a lane and how many candidates it
    dropped; fusion records the item cap; the disclosure filter records what tier
    policy and the black-hole guard removed; the grant path records a denial. The
    ledger comes back on the query response as `narrowing`.
  - **Every empty result now carries WHY it is empty.** `store_empty` (nothing
    has ever been stored for this scope — one `SELECT 1 … LIMIT 1` per canonical
    table, run only when a result is already empty and only when a ledger asked),
    `no_match` (candidates existed, none matched), `gate_vetoed` (the ask named
    something the corpus does not contain), `not_queried` (no retrieval ran), and
    `scope_denied` (permission, mode ceiling, selector suppression). The cause
    lands on `public_result.empty_cause`, because the model that writes the
    owner's answer reads that and not the envelope — and it is stored on the
    session artifact, so a memory hit replays the cause instead of losing it. The
    fifth cause is the point: reporting `store_empty` for data that is present but
    unmatched is exactly the false absence that told an owner their journal "may
    not be synced" while it sat indexed.
  - **Additive, optional, and mute by construction.** Every function that takes a
    ledger takes `None`, and `None` leaves the path byte-identical — nothing here
    decides anything, it records what was already happening. `record` never
    raises. Public fields are closed-set enums passed through a slug filter, so a
    call site that hands `reason` a fragment of the owner's question cannot leak
    it; anything worth keeping goes in `detail`, which `as_public` never
    serializes and only on-node debug logging reads. Same two-serializer split as
    `scope_shadow`.
  - The turn body moved from `QueryPipelineOrchestrator.execute` to
    `_execute_turn`, with `execute` as a thin wrapper that owns the ledger. Ten
    early returns end a turn; attributing each at its own return site would leave
    the eleventh unattributed the day someone adds it.

- `[E:graph] [D]` **The community pass now stamps structural analytics: every
  graph rebuild writes `centrality` = {degree, eigen, betweenness} and a
  human-readable `community_label` into `entities.metadata_json`.** Node
  prominence in the graph UI has only ever encoded extraction volume
  (mention_count); the witcher-network measures — connections (degree),
  influence (weighted eigenvector; PageRank stands in when power iteration
  diverges on a disconnected spectrum), bridging (betweenness, unweighted
  because edge weights are affinities not distances, Brandes-sampled at
  k=256 sources past 256 nodes, seeded) — are computed over the same
  in-memory graph `compute_communities` already builds for Louvain, outside
  the write gate. Each community is auto-named after its highest-eigenvector
  member's canonical name (ties break by weighted degree then id, so labels
  hold still across rebuilds): the legend can say "Ada" instead of
  "Community 3". Entities that leave the graph shed all three stamps in the
  same sweep that removed community_id; an analytics failure logs and
  degrades to community stamping alone, never blocking the rebuild.
  `graph_snapshot` passes the new keys through node metadata unchanged.
  Upgrade manifest: `rebuild-entity-graph-centrality` (fast, auto) rebuilds
  once so existing nodes light up at upgrade rather than at their next
  enrichment-triggered rebuild.

### Fixed

- `[E:graph]` **Goals (and every materialized edge) no longer vanish from the
  graph while a rebuild runs.** The materialized-edge lifecycle deleted every
  mz-tagged edge up front (`fact_materializer`), then re-created facts, goals,
  places and conversations lane by lane — and the goal lane alone takes minutes
  (it embeds every goal text to cluster near-duplicates). Each enrichment
  completion arms the debounced graph refresh, so a busy node rebuilds nearly
  back-to-back and the committed graph spent a large share of wall-clock time
  with **no `pursues`/`relates_to`/`located_at`/`mentions` edges at all**: any
  `/entities/graph` read in that window rendered a goal-less graph and a bare
  owner node (observed live: 462 edges mid-rebuild vs 3,041 after). The
  lifecycle is now upsert-then-sweep — lanes update surviving edges in place
  and record what they touched, and `rebuild_entity_graph` sweeps stale mz
  edges once at the END, only after every lane succeeded (a failed lane
  retains its old edges rather than losing them to a wipe it never followed).
  Readers now see at worst a few-minutes-stale edge, never a missing one.
  Same discipline `rebuild_evidence_edges` already applied to co-occurrence
  ("the delete and insert share one hold"). Report gains `mz_swept`.

- `[E:query]` **The scope-shadow env flag is authoritative in both directions.**
  `enabled()` checked the truthy spellings of `TOPOS_SCOPE_SHADOW` and otherwise
  fell through to the `~/.topos/scope_shadow.on` flag file, so
  `TOPOS_SCOPE_SHADOW=0` did not turn shadow off — the file won. The flag file
  exists because the node under the macOS app shell inherits no shell
  environment, and that same inheritance is what made the asymmetry reachable in
  the one place it mattered: a subprocess harness (release smoke, the upgrade
  matrix, a demo script) inherits the *operator's* home directory, so an armed
  flag file arms the harness too, and running the query path appends synthetic
  traffic to a real person's `~/.topos/scope_shadow.jsonl` with no way to decline
  from the environment. Tests are covered by an autouse guard fixture; a fixture
  does not cross a process boundary. An explicit `0`/`false`/`no`/`off` now
  returns `False` before the file is consulted, and the file stays the opt-in
  gesture for when the env says nothing — unset, blank, or a value in neither
  closed set. `TOPOS_SCOPE_SHADOW_LOG` redirects the log, for a harness that
  wants observation somewhere disposable rather than none at all.
  - Latent, not live: `scripts/release_smoke_test.py` was checked on 2026-08-18
    and never reaches the query path (`/`, `/healthcheck`, `/version` only) —
    the log's mtime and size were unchanged across a full run.
  - `scripts/run_query_eval.py` was the one harness genuinely exposed — its
    engine path runs `QueryPipeline` in-process against the operator's own
    `~/.topos` database — and now declines observation for itself. Absent or
    blank is read as "no opinion" there, the same way `enabled()` reads it, so
    an explicit `TOPOS_SCOPE_SHADOW=1` still opts a run in. It governs the
    in-process engine path only: under `--mcp` the observing happens in the
    node's process, under the node's own flag.

## [1.3.22] — 2026-08-18

### Fixed

- `[O]` **The update button could be pressed forever and never install
  anything.** Reported live 2026-08-18: the menu offered "Update to v1.3.21",
  said "Installing update…", the node restarted, and it came back on 1.3.20 —
  offering 1.3.21 again. Nothing had failed. The engine on that machine was
  installed from a working copy (`uv tool install ~/.topos/deploy-head`, which
  is what the deploy lane does), so `uv tool upgrade` faithfully re-resolved
  that same directory, rebuilt the same version, and exited 0. The node took
  exit 0 as proof and logged "Update installed"; the restart then wiped the
  in-memory update state, so even the menu's "Update failed — click to retry"
  never appeared. Every layer reported success and the version never moved.
  - `check_for_update` no longer offers a PyPI release to an engine that did
    not come from PyPI. uv's own tool receipt says where it came from; an
    install carrying a `directory`, `path`, `editable`, `url` or `git`
    requirement cannot be moved by a published release, and offering one
    anyway is a button whose only possible outcome is nothing.
  - `apply_package_update` refuses such an install outright rather than running
    uv to no effect, and — for every other install — now reads the version back
    off disk afterwards and reports failure when it did not move. An exit code
    of 0 was never proof that anything was installed.
  - The version is read from the tool's own `dist-info` rather than
    `importlib.metadata`, because this check runs inside the very process uv
    just rewrote, whose metadata was resolved at import time. When it cannot be
    read at all the update is still reported as success: an unknown must not be
    dressed up as a failure.
  - Machines running the deploy lane now see no update offer at all, which is
    the truth, with the reason logged once at startup and the one command that
    puts them back on PyPI (`uv tool install --force topos-node`).

- `[E:query]` **"Aug 11–16" searched Aug 11 only, and said the rest of the week
  was unsynced.** `_iso_date_hints` had patterns for `<month> <day>` but none for
  a day range inside one month, so the compact spelling yielded a single hint and
  `_explicit_time_range` — which takes min/max of the hints — collapsed to one
  day. Nothing looked broken: the window it returned was well-formed, the query
  succeeded, and the thin result read as missing data rather than a truncated
  search. Live 2026-08-17: a work report for `Aug 11–16, 2026` returned one day
  of activity and told the owner to check their sync.
  - Added a same-month range pattern covering `-`, `--`, en/em dash, and
    `to|through|thru|until|til|till`, with optional ordinal suffixes
    (`11th-16th`). The repeated-month (`Aug 11 to Aug 16`) and cross-month
    (`Aug 28 - Sep 3`) forms already worked, which is exactly why this survived:
    the broken spelling is the one people type first.
  - Impossible days are now dropped instead of formatted (`Feb 29-30, 2026`
    yields nothing rather than an unparseable ISO string), and month
    abbreviations are resolved through one shared alias table instead of a
    second inline dict.
  - Fixed alongside it: the full month-name pattern accepted "may" as a month, so
    `"I may 11 times reconsider"` parsed as May 11 — "may" is the one month name
    that is also an everyday verb, and a following number is not evidence (the
    abbreviation list had always omitted `may` for exactly this reason). The
    guard is anchored on the "may" token rather than on any one pattern's match,
    so a range and its first endpoint can never disagree. A capital, a date-like
    comma, an adjacent year or an ordinal still settle it as the month.

### Changed

- `[E:query]` **Scope shadow: observe every turn, and stop calling the router's
  guess "truth".** Two defects in the shadow log, found while diagnosing a
  session where three of four turns answered with no data at all.
  - **The blind spot was structural.** `observe()` ran inside
    `QueryPipeline.execute`, i.e. only *after* something had already chosen a
    scope. A turn that retrieved tools and then queried nothing never reached
    it — so the log could only ever see the turns that already worked well
    enough to route, and the failures were invisible by construction.
    `handlers/tool_index.py` (`tools_retrieve`) now observes the owner's raw
    text as it arrives, once per turn, before any routing. It runs concurrently
    with retrieval so it costs no wall-clock, and `return_exceptions=True` keeps
    a telemetry fault from failing the turn — a red-first test asserts that,
    because the first wiring did fail it. **Both** entrances to tool retrieval
    are hooked: the control-plane handler (prod) and `POST /v1/tools/retrieve`
    (engine-direct, the dev transport). Hooking only the first would have made
    shadow coverage depend on the client's transport and quietly excluded every
    locally-tested turn.
  - **`true_scope` was never truth.** It is whichever scope the incumbent
    heuristic router picked. Observed 2026-08-17: for *"what is a good prompt we
    could ask of our work, schedule…"* — a meta-question that should have
    retrieved nothing — the router chose `schedule:read` **and**
    `work_context:read`, and the log recorded both as gold. Training on that
    teaches the head to reproduce the router, mistakes included. Renamed to
    `router_scope`; `ShadowReport.accuracy()` is now `agreement_rate()`;
    telemetry `confusion` is now `divergence`. Rows written under the old name
    still read (`row_router_scope`), since they are the same data.
  - **New `turn_coverage()`** reports the population this was built for: turns
    observed, turns that queried nothing, and — among those — how many the head
    would have routed anyway. Those are recovery *candidates*, not proven
    recoveries; the head can be wrong too.
  - Records now carry `kind` (`"turn"` | `"route"`). `summarize()` counts only
    `route` rows, so turn rows cannot silently deflate the agreement rates.

### Fixed

- `[D]` **A topic cluster could name an off-limits entity in
  `related_entities`.** Found on a live node by the install QA's b19 probe: the
  cluster's *label* was clean — that surface has refused protected names since
  1.3.16 — while the list beside it named the same person, and `query_aliases`,
  derived from it, carried the name again.
  - **Why this surface needs the producer, not a read-time filter.** The
    black-hole guard filters by `entity_id`, and `entity_edges` (both ends),
    context vectors and affinity all rely on that. A bare NAME inside a cluster
    payload has no id to filter on, so no read path can catch it — and this one
    is not merely displayed: `fact_materializer` resolves each name into an
    entity node and a `discusses` edge. `_load_related_entities` now refuses
    protected names where they would be minted, so they never reach the payload
    or anything derived from it.
  - **And the other direction of time.** Clusters minted before an entity was
    protected still carried it, so the black-hole rebuild now withdraws
    name-bearing metadata (`related_entities`, `query_aliases`) alongside the
    label and previews it already handled. Blackholing an entity today cleans
    this surface immediately rather than at the next recompute.
  - The cap is applied after the refusal, so protecting one entity no longer
    silently shortens every list that mentions them.

## [1.3.21] — 2026-08-17

### Fixed

- `[S1]` **A newly created Topos could bind a database that belonged to no
  Topos.** Switching to a Topos that has not written yet leaves the active slot
  without a `database.db` — and the resolver answered that by searching the
  locations pre-profile installs used, so the node bound
  `~/Library/Application Support/ToposEngine/database.db` (or `~/.topos_engine`)
  instead of creating a database in the slot. Observed twice on one machine on
  2026-08-16 and 2026-08-17: the node migrated that foreign file in place,
  stamped its upgrade baseline, and served a full session from it. No profile
  owned it, so archiving the Topos left the data behind, and the next empty
  Topos would have picked the same file up — two Topoi sharing one database.
  Nothing said which file was in use; `lsof` was the only way to tell.
  - **One resolver.** `storage.db.paths.resolve_active_database()` is now the
    single answer, and `core.state`, the size readout, `discover_databases()`
    and profile adoption all call it. There were four searches with four
    different candidate lists and four different orders — `discover_databases()`
    did not even include `~/.topos/database.db` — so the size shown in the app
    could describe a different file than the one being read.
  - **A machine with profiles resolves to its slot, always.** Legacy locations
    are consulted only where no profile has ever existed, and then the database
    is ADOPTED — copied into the slot, original left in place as its own
    backup — rather than served where it lies. A database being served is
    always a database some Topos owns.
  - **The binding is now stated.** One line names the database, its source
    (`slot` / `new-slot` / `adopted` / `legacy` / `settings`), the owning profile
    and the schema version, and a database served from outside `~/.topos` logs a
    warning naming `profile adopt` as the remedy. It hangs off the code about to
    OPEN the connection, not off `startup_event`: the CLI opens the owner
    connection before uvicorn starts (printing pending consent steps), so
    anything in startup conditioned on "no connection yet" is already too late
    to say — or decide — anything. Adoption has the same one useful moment. `/healthcheck` carries
    `database_path`, `database_source` and `active_profile_id`, so a switch can
    finally be verified from the outside instead of assumed.
  - Removed `migrate_legacy_database()`: dead code (no callers) whose job was to
    copy a legacy database and pin `database_path` in `config.json` — a fifth
    way to end up bound to a file outside the active Topos.
- `[S1]` **Switching into a Topos from a newer engine failed as a broken boot
  instead of a refused switch.** The downgrade guard fires when the database is
  OPENED, by which point the switch has already moved every file and the node
  simply will not start. `switch_profile` now reads the target's
  `PRAGMA user_version` before anything moves and refuses with "This Topos was
  last used by a newer version of Topos … Update Topos, then switch again."
  The check is read-only and fails OPEN: a database it cannot read is not
  evidence of a version problem, and refusing on "cannot tell" would strand
  people on the Topos they are trying to leave.
- `[S1]` **A second Topos migrating deleted the first one's pre-upgrade
  backups.** Every Topos takes its turn in the same active slot, so all of them
  write into one `~/.topos/backups` under names that named no owner — and
  retention kept "the newest 2" across the whole directory. Switching to
  another Topos and letting it migrate therefore destroyed the safety net of a
  Topos that was not even running, silently. Backups are now
  `database-pre-v<version>--<profile>-<stamp>.db` and pruning only ever
  considers one Topos's own. The name is parsed rather than globbed: a glob for
  `--q4-*` also matches `--q4-2-…`, which would have been the same cross-Topos
  deletion one level down. Backups written before this carry no owner, form
  their own group, and are never pruned by a named one — nothing can prove
  which Topos they came from.
- `[S1]` **An archived Topos kept a hot WAL, so it could not be read without
  its sidecars.** Archiving renamed the database and its `-wal`/`-shm`, which
  works but leaves a file that a read-only open refuses — which is exactly why
  the switch preflight above could not read the schema version of the profile
  it was about to activate. Archiving now checkpoints (`wal_checkpoint
  (TRUNCATE)`) first, so an archived Topos is one self-contained file. It will
  not open a file it cannot identify as SQLite: sqlite3 can delete the sidecars
  of a file it fails to parse, and an archive must not lose bytes it could not
  read.

### Added

- `[S1]` **Archived Topoi record what they were last used with.** `profile.json`
  now carries `engine_version`, `schema_version`, `upgrade_baseline`,
  `size_bytes` and a `key_fingerprint` (a 12-char SHA-256 prefix — the key
  itself never leaves `.env`). Three things follow: `profile list` and the tray
  menu can label a Topos without opening a database (listing stays a folder
  read, and no longer walks a 500 MB directory to size it), the switch
  preflight has a fallback when the archived database will not open, and two
  profiles that share a display name can finally be told apart — the machine
  this came from has two archived Topoi both called "q4", bound to different
  keys. When an archive DOES share a key with an existing profile — which
  "Start Fresh" deliberately produces — it records `same_topos_as` instead of
  leaving two identical-looking rows.
- `[S1]` `/healthcheck` gained `database_path`, `database_source` and
  `active_profile_id`; the macOS tray uses them to verify that a switch landed
  on the Topos it asked for (shell `1a4d79b`).
- `[S1]` Adopting a pre-profile database written by a NEWER engine — what a
  machine that downgraded its node has — now logs what is about to happen
  before it happens. The adoption still proceeds and the downgrade guard still
  refuses to open it: starting the node on an empty Topos instead would read as
  data loss, which is the exact silence this release is curing.
- `[S1]` `topos-node --discover` now answers the question people actually run
  it for: it names the database being SERVED and the Topos that owns it, then
  lists databases from older installs as "not served", and warns outright when
  the served one is a stray. It used to print a bare list that led with a
  legacy stub and never mentioned the active slot.

## [1.3.20] — 2026-08-17

### Added

- `[O]` **`topos-node profile remove <id>` — a Topos can finally be taken off a
  machine.** Every profile operation so far moved data: `new` and `switch`
  archive it into `~/.topos/profiles/<id>/`, and "Disconnect this Mac" in the
  menu bar is `new` under another name. Nothing deleted anything, and neither
  did the web app — its Archive button soft-deletes the control-plane record and
  never touches the machine. So a Topos a user was finished with stayed on their
  disk, in full, with `rm -rf` as the only way out. On a product whose claim is
  that the data lives on your own machine, "take this one off my machine" was
  the missing verb.
  - Refuses rather than guesses. The active Topos is rejected by name (switch
    away or disconnect first, and the error says so); a profile id containing a
    path is rejected before anything resolves; and a profile holding a file that
    is not part of a Topos is reported back with the filenames and **nothing is
    deleted** — not even the files that were recognised. Same principle as the
    move allowlist, for the stronger reason that this folder is somebody's only
    copy. `.DS_Store` does not count as a stranger.
  - Does not require the node to be stopped, unlike `new` and `switch`. Those
    move the ACTIVE slot out from under a running engine; this touches only an
    archived profile, which nothing has open. Quitting Topos to delete a Topos
    you are not using would be ceremony, and the restart costs a graph rebuild.
  - `--yes` for shells; without it and without a terminal to answer, it aborts
    instead of deleting on a guess. Reports the bytes it freed.

### Fixed

- `[O]` **The data explorer listed, sized and offered to delete raw records that
  belonged to no Topos.** Raw ingestion is written to `~/.topos/ingestion`, and
  `ingestion` is on `profiles.MOVE_ALLOWLIST` — it archives when you switch away
  from a Topos and comes back when you switch in. But the three explorer
  handlers (`list_jsonl_files`, `delete_jsonl_file`, `read_jsonl_file`) and
  `storage_breakdown.raw_ingestion_size_bytes()` also searched
  `~/.topos_engine/ingestion` and unioned whatever they found there. That
  directory predates the profile layout by months: no profile owns it, no
  switch carries it, and its records are not the active Topos's data. The
  storage readout counted its bytes as this Topos's storage, the explorer
  listed its files as this Topos's files, and the delete handler accepted paths
  inside it. Same shape as the database-binding bug — a surface answering for
  data no Topos owns — and found by looking for the rest of its class.
  - **One answer.** `storage.raw.file_store.active_ingestion_base()` is the
    single resolver; the writer and all four readers call it. No reader
    searches for a directory any more, so what the app shows is what ingestion
    wrote. Legacy folders are left exactly where they are on disk — untouched,
    just no longer presented as this Topos's.
  - **`TOPOS_INGESTION_BASE_PATH` is honoured everywhere.** The writer already
    respected it; the three explorer handlers hard-coded `~/.topos` and ignored
    it outright. With the override set, the app listed one directory while
    ingestion wrote to another, and files written under the override could
    neither be downloaded nor deleted through the UI — the allowlist did not
    include the directory the node was actually writing to.
  - The delete and read handlers shared one copy of the containment check
    instead of two, and the scope narrowed to a single root: a path is allowed
    when it is inside the active ingestion directory, and an ingestion
    directory that does not exist yet denies rather than widens.

## [1.3.19] — 2026-08-17

### Fixed

- `[E:facts]` **One cancelled fact-extraction batch could disable LLM fact
  extraction for the life of the node.** `FactExtractionJob` answered
  `asyncio.CancelledError` with `runtime_shutdown.request_shutdown(
  "fact_extraction_cancelled")` — a process-lifetime flag that only an
  `app.startup_event` could clear. Every later batch in that process then
  returned 0 on entry ("LLM fact pass skipped; engine shutting down"), the
  rules floor kept writing, the job kept reporting success, and
  `/healthcheck` stayed green: a lane going dark with nothing to see, the same
  signature as the `db_ok` outage below. Nothing cancels enrichment today
  (no `wait_for` in `enrichment/`/`pipeline/`, and the pipeline worker task is
  never cancelled), so this was latent, not firing — but it was one job
  timeout or cancel button away, and the flag's scope was wrong regardless.
  Three changes, prevention plus detection:
  - **Cancelling one batch is now scoped to that batch.** The job passes a
    `threading.Event` down through `extract_facts_from_batch(cancel=…)` into
    `extract_owner_facts_llm(cancel=…)` and sets it in its own
    `except CancelledError`. It cannot reach any other batch, or the run.
  - **`runtime_shutdown` tracks a generation, not a boolean.** A generation is
    one runtime run: an app lifespan, or the ambient generation of a process
    that never starts one (CLI, scripts, tests). `begin_runtime()` at startup
    mints a fresh one; `end_runtime()` at shutdown retires it — its own
    workers stop and STAY stopped — and installs a fresh, unset one for
    whatever comes next. Workers capture the generation they started under and
    poll that, so a run that ends mid-batch still stops the batch it started,
    and a run that ends cannot leave a later, unrelated caller reading
    "shutting down". The old `clear_shutdown()`-at-startup did the opposite:
    it un-stopped the previous run's still-draining workers. `begin_runtime`
    also retires an outgoing generation, so a run that dies without
    `end_runtime` cannot strand workers polling a flag nobody will set.
    `is_shutdown_requested()` / `request_shutdown()` keep their signatures and
    their meaning for the signal path.
  - **A stop is reported, not silent.** `extract_owner_facts_llm(stats=…)`
    fills an out-dict with `written`/`eligible`/`stopped`/`stop_reason`/
    `unprocessed`, and the job carries `_facts_llm_stopped` into its result and
    logs at WARNING. "Stopped with 12 rows left" no longer reads as
    "there was nothing to extract". Unprocessed rows stay unmarked, so the
    next pass retries them, as before.

- `[O]` **18 tests stopped failing only when the suites run together.**
  `tests/topos tests/storage tests/core tests/features tests/api
  tests/disclosure` failed 18 tests that every one of those directories passed
  on its own — not test-order randomness (`-p no:randomly` reproduced it),
  three separate pieces of global state leaking out of `tests/topos` and
  `tests/core`:
  - The shutdown flag above accounted for 16 of them: any test that ran an app
    lifespan (`tests/topos/test_smoke.py` and the other `load_topos_app` files)
    left every later `extract_owner_facts_llm` call inert, and
    `tests/features/test_fact_extraction_llm.py` with it. Fixed at the source
    by the generation change rather than papered over in a conftest — an app
    run's shutdown no longer belongs to the process it ran in.
  - Five helpers in `tests/topos` re-import the app under fresh env by popping
    `topos.app` / `topos.config.settings` / `topos.auth` / `topos.core.state`
    out of `sys.modules` and never putting the originals back. The re-import
    FORKS those modules — `sys.modules["topos.core.state"]` becomes a new
    object while `topos.core.handlers.common.get_db_connection`, imported
    earlier, still reads the old module's globals — so a later test patches one
    module and the code reads the other. That is
    `test_attention_dashboard_endpoint`'s 401 (its `dependency_overrides` key
    was a post-fork `require_api_key`) and
    `test_affinity_traversal_over_ws_bridge`'s empty result (its injected
    `:memory:` handle was ignored and the handler opened a fresh guard.db).
    `tests/topos/conftest.py` now restores module identity after any test that
    forks one, the same cure `engine_runtime_isolation` already applied to its
    own purge.
  - `tests/core/test_enrichment_entrypoint_parity.py` imported the handlers
    package AFTER patching `topos.core.state.get_db_connection`, so on
    `pytest tests/core` the package re-exported the patched value and
    monkeypatch's saved "original" was the test's own lambda — reinstalled at
    undo for the rest of the session. Imports moved to module scope.

- `[O]` **A node can no longer keep answering "healthy" after its database has
  died.** On 2026-08-17 the node served `/healthcheck` normally for nearly two
  hours while every data read failed and the app showed a connected Topos over
  an empty graph. Root cause: `run_db_read` accepted a `sqlite3.Connection`
  resolved by the caller on the event-loop thread and then ran the work on an
  `asyncio.to_thread` worker. Loop thread and worker executed on that one
  Connection at the same time, and CPython's per-connection prepared-statement
  cache — an unsynchronized C LRU keyed by the 1-tuple `(sql,)` — went
  inconsistent. Its eviction path deleted a key that was already gone, and from
  then on **every** `execute` on that handle raised `KeyError(('<sql>',))`,
  quoting a statement the caller had never issued. Reproduced on the shipped
  interpreter (3.10.16). Five changes:
  - `run_db_read` now resolves the connection INSIDE the worker, exactly as
    `run_db_write` does; all 15 call sites stopped passing one in. Request-scoped
    objects built from a connection (`BlackholeGuard`) are constructed in the
    worker too — one built on the loop carries the loop's handle in with it.
  - The routine handlers that still wrote inline on the loop (`create_run`,
    `update_run`, `advance_next_run_at`) go through `run_db_write`. Those were
    the writes racing the read at 07:00:16.
  - Schema probes distinguish "not created yet" from "connection unusable"
    (`storage/db/schema_probe.py`). Both probes previously swallowed every
    exception and answered "absent", so a broken connection took the write gate
    ON THE EVENT LOOP to run DDL that could never succeed — silently undoing the
    gate work above.
  - `SELECT 1` liveness probes in `core/state.py` now treat `KeyError` as an
    unhealthy handle and open a fresh one, so a poisoned connection stops being
    fatal for the life of the process. (`cached_statements=0` does not help:
    CPython 3.10 clamps the cache to a minimum of 5 — measured, 0/1/4 all behave
    exactly like 5.)
  - Prevention is not detection, so `/healthcheck` and the relayed `healthcheck`
    handler now carry `db_ok` (a `SELECT 1`, off the loop, on the worker's own
    connection). `status: "ok"` never meant more than "the event loop answered".

- `[O]` App shutdown now reaps the background work app startup launched. Four
  fire-and-forget `asyncio.create_task` calls, the pipeline worker and the
  upgrade-runner thread all outlived their app: the runner's handle was
  discarded outright, and its ready-wait and UI grace were uninterruptible
  sleeps, so it stayed alive ~80s past shutdown and then ran migrations against
  a database the next app instance was already migrating. Each is tracked in
  `core.state` now and stopped in `shutdown_event`. This pairs with the
  generation change above rather than duplicating it: retiring a generation
  tells workers to stop, and these handles are what WAIT for them to be gone.
  The distinction is load-bearing here — the next app's startup migrates the
  same database, so "asked to stop" is not enough; a writer still draining
  takes the write gate and stalls it.
- `[O]` A migration blocked on SQLite no longer pins the process-wide write
  gate. `ensure_migrations_applied` took the gate and then issued a write that
  could wait out the full 30s `busy_timeout`, so every other writer queued
  behind a holder that was itself only waiting. The wait inside the gate is now
  bounded and retried with the gate released between attempts; worst-case total
  is unchanged, but it is no longer spent blocking the rest of the process.
- `[O]` The startup DB section (stage 9 + source-install rehydration) is bounded
  and names the write-gate holder when it times out, instead of awaiting an
  event that nothing was going to set. `write_gate` tracks its current holder
  (site, thread, duration) — an RLock names neither — and the test lifespan
  timeout reports it, so the failure stops pointing at the event loop and starts
  pointing at whoever is holding.
- `[O]` Together these end an intermittent CI failure that surfaced as
  `App startup did not complete within 30s` on a rotating cast of tests in
  `tests/topos/test_ingestion_sources.py`. It was never event-loop starvation
  and never that test's fault: the two 30s figures — SQLite's `busy_timeout` and
  the lifespan budget — are the same number by coincidence. New guards in
  `tests/topos/test_startup_background_reaping.py`, plus a conftest check that
  reds the run when an engine thread outlives its test.

## [1.3.18] — 2026-08-16

### Fixed

- `[O]` Local model setup answers for the machine it is running on. Three
  surfaces asked "how do I get Ollama here" and answered three different ways,
  all wrong off macOS: the one-click refusal said only "available on macOS", the
  web card hard-coded `brew install ollama` for every owner, and the terminal
  path never mentioned Ollama at all — so a `topos-node` owner finished setup
  with a running node, no model, and nowhere to go but the macOS-flavoured card.
  One table (`engine/ollama_setup_guidance.py`) now serves all three, keyed on
  the platform the NODE runs on. macOS moves to the Homebrew CASK: the formula
  drops a binary and starts nothing, so `:11434` stayed closed and every surface
  that gates on reachability looped forever telling the owner to run a command
  they had already run.
- `[O]` New `topos-node setup-models`: reachability, the install command for this
  platform, and the starter pull, without leaving the shell. It does not create
  the pack — the control plane seeds the local family from what the machine
  actually has, so once a model exists here the next pack read leads with it.
- `[O]` A pull that did not happen no longer reads as a pull that did. Ollama
  reports failures as an `error` frame INSIDE a 200 response and nothing looked,
  so the stream ended cleanly and the record was marked done — a model that was
  never written, reported as installed, with the setup card advancing to
  "seeded" and the first chat 404ing. Covers every mid-stream failure, not only
  the disk-full one that exposed it.
- `[O]` Model downloads check for disk space. Nothing did, before the largest
  write this node asks a machine to make. Two layers: a preflight when the size
  is known up front, and an abort as soon as the stream reports the real total —
  seconds in rather than at 97%, and before the write that would fill the volume
  the node's SQLite is on (`runtime_housekeeping`: "ENOSPC mid-write is how
  databases corrupt"). A remote Ollama is not our disk to judge and an unreadable
  volume is not a full one; only a real free-space number below a real
  requirement refuses.
- `[O]` The setup CLI pulls through the adapter instead of shelling out to
  `ollama pull`, which downloaded via the local binary while reachability had
  been probed against `engine_ollama_base_url` — the wrong daemon on a node
  using a remote host, or no binary at all.

- `[O]` The engine's local-model defaults name a model the machine can actually
  pull. `ollama_extraction_model` and `privacy_judge_model` both defaulted to
  `qwen3.5:9b-mlx`; MLX is Apple's array framework, so that tag exists for
  Apple-Silicon Macs and for nothing else. On Windows, Linux and Intel Macs the
  LLM fact pass and the privacy judge asked Ollama for a model it could never
  serve — and it is the same tag the pack resolver demotes a missing local role
  *to*, so the safety net and the thing it was catching were one dead model.
  Both defaults now resolve from the running machine: a curated tag is stored in
  its portable build and an accelerated build is attached only where one is
  recorded as published for that exact tag. The axis is (os, arch), not os — an
  Intel Mac is macOS and still cannot run MLX — and an unrecognised platform
  takes the portable build rather than a guess. Lane `[O]`: on Apple Silicon
  both fields resolve to `qwen3.5:9b-mlx` exactly as before, and on the
  platforms where the value changes the old one produced no output to
  invalidate, so nothing needs reprocessing.

## [1.3.17] — 2026-08-16

### Fixed

- `[O]` A finished rebuild no longer blocks Select Topos forever. The lock
  beside the database is an advisory `flock` that is created on first rebuild
  and never deleted — the OS drops the *lock* when the child exits, the *file*
  stays — so refusing a profile switch on the file's existence blocked
  switching permanently on every machine that had ever rebuilt its entity
  graph. The guard now asks the OS whether the lock is actually held. Found by
  hand on a node carrying a week-old empty lock, under an error telling its
  owner to wait for a rebuild that had finished eight days earlier. On a
  platform without `flock` the answer is "no rebuild", not "yes": the engine
  takes no lock there either, so the file carries no information and claiming
  otherwise would rebuild the same permanent block one platform over.
- `[E:query]` **The owner's actual question reaches the query path again.** The
  handler read `intent` before `query`, so home chat's stopword-stripped
  fingerprint became the query text for every downstream stage: "how did I sleep
  this week?" arrived as "sleep week". Measured cost — the planner's `\bthis week\b`
  never matched, so the turn silently lost its time window; vector ranking embedded
  a fragment instead of a sentence; and the scope classifier, trained on questions,
  abstained on keyword soup. Nothing wanted the digest (retrieval's `_query_tokens`
  already strips stopwords where a bag of words is needed), and the clients were
  never at fault: home chat has always sent both fields. One-line precedence flip;
  callers that send only `intent` are unaffected.

- `[O]` **The event loop stops taking the SQLite write gate on the hottest
  paths.** The gate is a blocking OS lock, so acquiring it inside a coroutine
  stalls every other coroutine — including the control-plane keepalive, which
  is the node-side half of the 2026-08-15 relay outage: relayed requests
  (healthcheck, `get_device_info`) answered late or not at all during heavy
  enrichment, and only the `control_plane_client` snapshot fallback kept device
  info alive. The engine's own `[WRITE_GATE] acquired on the event-loop thread`
  guard named the call sites; this fixes the ones that fired most (147, 134 and
  107 times in a single log), in two shapes:
  - **Moved off the loop** (`asyncio.to_thread`, resolving the worker's own
    connection — a `sqlite3.Connection` holds one transaction state, so handing
    the loop's handle to a worker is the corruption `get_db_connection` was made
    thread-local to prevent): the Usage Inbox dedupe lookups and delivery
    record in `app_ingest`/`check_inbox_write`, the whole `install_service`
    surface behind the sources API (install/list/patch/uninstall/rehydrate), the
    six routine write handlers, and both gated sections of the platform privacy
    layer (the `canonical_nsfw_v1` migration and the batched disclosure/NSFW
    write pass). A new `run_db_write` helper next to `run_db_read` makes the
    handler-layer conversions one line each and resolves the connection through
    the hub, so tests that monkeypatch `get_db_connection` still bind the
    worker's handle.
  - **No longer taking the gate at all**: `CanonicalTablesManager._ensure_tables`
    and `source_settings.ensure_table` re-ran idempotent DDL on every
    construction and every settings read respectively. Both now probe first
    (`sqlite_master` / `PRAGMA table_info` — plain reads), so the steady state
    costs no gate. Deliberately not memoized on `id(conn)`: addresses are reused
    once a handle is collected, which reports DDL as done on a brand-new empty
    database.

  `run_privacy_disclosure_layer` accepts `conn=None` (what the ingest pipeline
  now passes) to mean "resolve per worker"; a caller-pinned connection keeps its
  thread affinity and runs inline, because test fixtures open theirs with
  `check_same_thread=True`. Remaining loop-thread acquisitions are lower
  frequency and structural — the biggest is `get_signal_service`, which hands
  the caller's connection to a long-lived `SignalService`, so it needs a
  connection-lifecycle change rather than a `to_thread` wrapper.

- `[E]` **Place references extract again: `PlaceContext` is declared for the
  `places` dimension.** The artifact router has always written `PlaceContext`
  objects for place `EntityRef` artifacts, but `places.json` never declared the
  type, so `SignalObjectStore._validate_object_type` rejected every write and
  `dimension_summary` DEGRADED on any batch containing a place reference
  (observed on grow_data_file, 2026-08-15: every batch queued for retry with
  derived data MISSING). Every other router-written type (RelationshipEdge,
  SkillNode, ExperienceNode, Goal) was declared — places was the one gap. The
  static write-site sweep in test_dimension_definitions was blind to it because
  its regex only matched snake_case object types and only checked
  `signal_objects`; it now matches CamelCase entity types and checks the same
  allowlist the store enforces (entity ids + signal_objects + gate_objects).
  A `rerun-places-dimension-summary` upgrade step re-runs the failed job where
  derived output is missing.

### Changed

- `[E]` **Ingest models are no longer steered by the model pack.** The pack's
  `classify` role selected the models for facts extraction and conversation-context
  tagging — ingest functions that run when data arrives, not when the owner asks
  something, and that already have their own selectors (engine_config, surfaced as
  Node functions). The role is retired repo-wide: `resolve_facts_llm_model` and
  `resolve_context_llm_model` are now device-override → settings default with no
  pack rung, and enrichment usage (`ENRICHMENT_SUBTYPES`) is attributed to the
  active pack with NO role — the old chain fell through to a generic `primary`
  label, billing the pack for a decision it did not make. The engine ROLES mirror
  is (primary, reasoning, tool, scope). Guards inverted: the J-B10 wiring test now
  asserts these two functions make NO pack-resolver call at all.

### Fixed

- `[E]` **The node stopped wedging: torch's thread pool is bounded and every model's
  device is resolved in one place.** Twelve event-loop deadlocks on 2026-08-15, always
  the same stack — `TensorImpl::incref_pyobject → gil_scoped_acquire → take_gil`, with
  26 threads resident in libtorch and the loop blocked behind them. Torch's intra-op
  pool is one thread per core and each can need the GIL to refcount a Python-owned
  tensor; in an asyncio server that already runs DB work and job workers on threads,
  the pool and the loop deadlock. New `topos/engine/torch_runtime.py` bounds intra-op
  to 2 and inter-op to 1 at startup, before the first model loads. It also owns device
  selection for embeddings, NER, rerank, the privacy filter and the scope head, which
  previously each guessed for themselves: sentence-transformers took no device argument
  at all and silently chose MPS on any Mac (and CUDA nowhere), while the privacy filter
  ran its own capability ladder that had drifted from it. One resolver now: explicit
  `TOPOS_ML_DEVICE`/`engine_ml_device` → `auto` (CUDA, else MPS, else CPU) → CPU, so a
  CUDA host uses CUDA without a code change and any host can be pinned by name.
  Device is part of each model's cache key, so switching it reloads instead of serving
  a handle pinned to the old device. **`auto` does not select MPS**: bounding threads
  alone did not stop the wedge — the node hung again on the next burst of MPS
  embedding calls with the pool at two — so while the torch bug stands, MPS is opt-in
  by name (`ENGINE_ML_DEVICE=mps`). These models are small (MiniLM 22M, NER 125M) and
  CPU-fast on Apple silicon; the cost is far smaller than a hung node.


### Changed

- `[E:query]` **Horos ships from Hugging Face, picks its device by capability, and
  warms at boot.** Three changes with one theme — nothing about the scope head is
  hardcoded to this machine any more. (1) `load_head` resolves an explicit
  `TOPOS_SCOPE_HEAD` path, then a staged `~/.topos/models/scope_head`, then falls back
  to fetching `Dialogues/horos` **at a pinned revision**; a node that cannot reach the
  hub degrades to prototype routing rather than failing, and `HF_HUB_OFFLINE` plus the
  local cache make a node network-free after first fetch. (2) New `resolve_device`:
  explicit override → `auto` (CUDA, else MPS, else CPU) → default `cpu`. The head is
  no longer implicitly CPU-by-omission, and training's `auto` no longer preferred MPS
  over CUDA, which would have picked the weaker accelerator on a CUDA box. Inference
  still defaults to CPU on purpose: 17 ms is fast enough, and the accelerator belongs
  to the owner's LLM. (3) Shadow warms **once at startup**, single-threaded, before
  traffic and before the MPS models load — the previous daemon-thread warm put a
  265 MB load in flight beside live Metal work, and this process carries a torch
  GIL/MPS deadlock (synchronous dispatch whose block re-acquires the GIL) that wedged
  the node twelve times on 2026-08-15. A circuit breaker disables observation for the
  process after 3 slow-or-failed turns: telemetry may cost a millisecond, never a turn.


### Added

- `[E:query]` **Horos, the scope classifier, trained to its spec: multi-label with an
  explicit `none` class and a four-branch ladder.** `classify()` now escalates on
  UNCERTAINTY (ambiguity or ignorance), never on cardinality — a confident
  `{availability, schedule}` set is acted on, and a new `reason` field keeps the
  branches measurable apart. The trained head is published as `Dialogues/horos`
  (macro-F1 0.512 on classify-8 vs mistral:7b's 0.495; hybrid-with-escalation 0.550
  at ~1/6th the LLM calls). Pre-`none` artifacts keep the legacy two-threshold path.
  New `scope` pack role (engine mirror): on-device `scope-head` provider, optional
  and engine-defaulted so stored packs stay valid. Dimension definitions gain
  `WorkItem`, `BrowseTrail`, `ProfileSurface` — the classifier's dead zone was
  partly a schema gap. Shadow mode gains a `~/.topos/scope_shadow.on` flag-file
  switch (the app-shell node inherits no env) and an off-thread model warm so a
  cold head never loads inline in the request path.


### Fixed

- `[S1]` **The Lab's "apply preferred model" for entities is applied, not just
  stored.** Overrides are saved per JOB id but looked up per engine SUBTYPE via
  `SUBTYPE_TO_JOB`, and `entities_job` emits `entity_extraction_batch` while the
  map carried only the bare `entity_extraction` — so picking a preferred NER
  model in the Enrichment Lab wrote the override, displayed it, and never used
  it. emo_27 hit the identical hole at its `_batch` rename and was patched by
  hand; entities was missed. Both forms are mapped now, and a new test derives
  the required entries from the Lab's own factory dict and each job module's
  actual `run_engine_task(subtype=...)` literals, so the next subtype rename
  fails a test instead of silently orphaning an override.
  (`topos/enrichment/model_overrides.py`,
  `tests/enrichment/test_model_override_subtypes.py`)

## [1.3.16] — 2026-08-15

### Added

- `[D]` **A cold labeling model no longer silently discards a whole relabel
  pass.** `apply_llm_cluster_labels` aborts on the first failure so a down model
  costs one timeout rather than k of them — but the budget was a flat 10s, the
  first call of a pass pays the local model's load cost, and labeling order is
  biggest-cluster-first, so the longest prompt always landed on the coldest
  model. On a live node that aborted all 163 clusters in 12 seconds and reported
  `status: completed, relabeled: 0`: a no-op that reads as success. Three
  changes: the first call of a pass gets a warm-up budget
  (`TOPOS_CLUSTER_LABEL_WARMUP_TIMEOUT`, default 90s) while later calls keep the
  ordinary one; a single slow cluster costs that cluster its label rather than
  the remainder of the pass (abort now needs three CONSECUTIVE failures, or a
  model that never answered at all); and the abort is reported —
  `relabel_existing_clusters` returns `status: "aborted"` with a reason instead
  of a completed run that did nothing. Still never raised: `recompute_topic_clusters`
  calls the same labeler, and a down model must cost the labels, not the cluster
  rebuild. (`topos/features/signal/cluster_labels.py`)

- `[S1]` **Declarative canonical field mapping** (`canonical_field_map` on a
  source definition — PLAN_CONNECTOR_CATALOG_ROLLOUT §5a capabilities 2–3). A
  source now DECLARES which piece of its records lands in which canonical
  column, and where extra rows fan out from, instead of that being Python in
  this repo: `{table: {column: rule}}` with a rule vocabulary of `path` (dotted,
  `[*]` expands lists), `first_of`, `template`, `const`, `map`/`default`,
  `transform` (closed catalog), `join`, `when`, plus `fan_out` + `where` for
  one-row-per-item lanes. A declaration overlays the source's code mapper where
  it has one and IS the mapping where it does not — which is what makes the
  activity/journal/documents lanes reachable for a runtime-installed source
  (previously they fell back to the *browser* mapper, the wrong shape for
  anything but browser visits). Validated at definition time, so a typo'd path
  fails the install instead of silently ingesting nothing.
  (`topos/canonicalization/declared_field_map.py`)

- `[S1]` **Recorded derivation debt now waits for its model instead of burning
  its one attempt on it.** A debt whose job needs a provider this machine does
  not have could only be claimed, re-run into the same wall, and parked
  `failed` — after which nothing in the queue moved it, because `requeue_job`
  only releases a live claim and `recover_stale_jobs` only touches `running`.
  The work resumed when a human hit `POST /signal/derivation-debt/retry`, or
  never. Two halves: `run_derivation_retry_job` now asks
  `job_readiness.job_is_ready()` BEFORE reloading the batch's canonical records,
  and holds with `waiting for provider: …` rather than doing the work to defer
  again; and `revive_capability_blocked_debts()` re-queues the parked rows on
  the not-ready → ready EDGE, swept by the pipeline worker every 5 min. Edge
  rather than level so a debt that fails for a real reason gets one fresh
  attempt per time the missing provider actually appears, instead of being
  re-queued forever. Per-job provider is read from the model catalog
  (`MVP_JOB_SPECS`), so a job registered there is classified without a second
  list to maintain; jobs absent from it are assumed runnable rather than
  stalled. (`topos/enrichment/job_readiness.py`,
  `topos/pipeline/job_store.py::requeue_failed_jobs`)

- `[E:llm]` One-click Ollama install for the quick-start journey (J-B10):
  `ollama_install` runs the official installer on an explicit request —
  Darwin only, single-flight, never from ambient state — and
  `ollama_install_status` reports `idle|installing|started|error` with the
  installer's status lines. A nonzero installer exit with `/Applications/
  Ollama.app` present still ends in `started` (the node launches it and lets
  reachability be the truth), because the script's only sudo is a PATH symlink
  the engine does not need.

- `[E:llm]` `ollama_list_models` now carries per-tag `capabilities`,
  `modified_at`, and size detail so the quick-setup station can prefer a
  tools-capable model and label downloads honestly.

### Changed

- `[S1] [D]` **A commit is the owner's deed, not reliably the owner's words:
  `github_activity` is ambient-posture and no longer fans commits into the
  journal.** The source declared `posture='personal'` ("owner-performed deeds")
  and mapped every PushEvent commit into a `journal_entries` row. But
  `journal_entries` is authored-by-construction in `provenance.roles` — a row
  there IS the owner's own writing, belief-grade, eligible to mint goals and
  self-facts — and commit prose is written by coding agents now. The gate meant
  to protect that lane keyed on a co-author TRAILER, so it correctly demoted
  `Co-Authored-By: Claude` and passed an identical agent-written message with no
  trailer; that blind spot is not fixable by a better regex. Both halves are
  retired: the fan-out is gone (the mapper is single-lane, and the `authorship`
  stamp now routes nothing), and `posture='ambient'` caps any row this source
  produces at `observed`, which the fact store's LLM gate honours ("an
  ambient-flagged source can never mint a belief here"). The lane existed
  because commit messages lived nowhere else — no longer true since they land on
  `activity_events.content`, where the role model already reads them as ambient.
  Retrieval, clustering, interests and attention triage are unaffected: activity
  rows are `ROLE_AMBIENT` by table either way. On the first live node checked,
  485 journal-lane facts existed and **zero** were belief-shaped (all entity
  mentions, no predicates), so this closes the door before anything came
  through it. The owner can still override per connector
  (`storage.source_settings` → `effective_posture`). Empirically, once commit
  text reached the topic layer the corpus surfaced
  `network_bridge :: "Topos (claude)"` as its third-largest cluster (65
  vectors) — the history saying out loud that much of it is "an agent worked on
  this with me".

- `[S1] [E:embeddings]` Ambient activity rows (browser visits) now embed ONE
  vector per distinct page text instead of one per visit: batch-level dedup in
  the embeddings job plus a cross-record `content_hash` check at the vector
  write (`has_duplicate_content`). A page revisited 30 times contributes one
  ANN neighbor, not 30 near-identical ones (August data: 1,034 visit rows →
  360 distinct titles). Message-family rows are exempt — every message keeps
  its own vector row so it stays individually retrievable. Reloaded activity
  batches without a `_table` stamp now classify as `activity_event` via their
  `activity_type`, so backfills get the same policy.

- `[E:llm]` Ollama pull/list hardening from the J-B10 verification rounds:
  monotonic pull progress survives a restarted layer, a garbage tag surfaces
  the Ollama error instead of polling forever, and a 502 answers
  `reachable: false` rather than an ambiguous empty list.

### Removed

- `[D]` **Mega-clusters split; labels read the canonical text.** Two coupled
  changes to the topics pipeline, both from the same live measurement: 23
  clusters of 120+ members held 51% of all members (one 147-member cluster
  mixed T-Mobile spam with housing logistics), and the stored member previews
  are redacted (`[NAME]`/`[ACCOUNT]`) — so the labeler was naming subjectless
  bags from de-specified text, and base-name collisions ("Cameron and
  Danielle" ×13) concentrated in exactly those clusters. (1) A cluster above
  `TOPOS_CLUSTER_SPLIT_SIZE` (default 120) members is re-clustered within
  itself; fragments go straight back through the similarity merge, so a
  genuinely coherent big cluster re-merges and stays whole — the merge is the
  split's veto. The parent id survives on the largest fragment. Rehearsed on a
  live-DB copy: 23 → 14 mega-clusters, mega-member share 51% → 33%. (2) The
  label prompt and distinguishing-term extraction now read the CANONICAL row
  (`_hydrate_label_texts`, in memory only, `TOPOS_CLUSTER_LABEL_RAW_TEXT=off`
  to revert); every persisted surface — embedding previews, member previews,
  centroid previews — keeps redaction exactly where and how it was, pinned by
  a test that persists a hydrated cluster and asserts the raw name never
  reaches disk. The off-limits gate still refuses protected names at the
  label. Owner decision 2026-08-15: labeling is an owner-side surface; the
  redaction that matters stays on the stored tables.
  (`topos/features/signal/topic_clustering.py`)

- `[P]` **The live-engine pressure tests stop failing 401 against a healthy
  node.** `tests/conftest.py` sets `TOPOS_KEY="test-key"` so Settings validation
  passes for the whole suite. `test_live_engine_pressure` guards itself with
  `if not TOPOS_KEY: skip`, which cannot tell that placeholder from a real key —
  so instead of skipping it sent `Bearer test-key` to the running node and got a
  401 on every assertion. Three sessions read those 401s as "engine not
  reachable" and waved them through as environmental, which is how a lane stops
  meaning anything. The module now resolves the key it should actually use:
  an explicit non-placeholder `TOPOS_KEY`, else the node's own `~/.topos/.env`,
  else skip with a reason that says which. Against a live node all four tests
  pass; with no key anywhere they skip rather than fail.
  (`tests/release/iteration4/test_live_engine_pressure.py`)

- `[S1]` **`url_classification` is retired — job, table, engine path and the
  interest tags it wrote.** The job labelled every visited page with a DMOZ
  top-level category. Six months on the node this was measured on produced
  8,616 rows, **73% of them the single label "Reference"** — at an average
  confidence of **0.961, higher than any other bucket**, so no threshold could
  separate it from filler (87% of Reference rows scored ≥0.95, and the lowest
  confidence bucket was `Computers`, the useful one). Labels were not stable per
  site, because the classifier reads the page TITLE and an authenticated app
  title carries almost nothing: `github.com` came back Reference 720 /
  Computers 125 / Business 23 / Kids 5, `google.com` was "Porn" twelve times,
  and `drive.google.com` was *majority* "Home". The skew was structural, not a
  regression — 61% Reference in February, 70–79% every month since.

  It is a taxonomy mismatch rather than a tuning problem. DMOZ categories
  describe where a site files in a web directory; they do not describe what a
  person is doing, and "Reference" is that scheme's catch-all.

  The rows reached `interests` through
  `scope_materializer._materialize_activity_tags`, which upserted each category
  as an `activity_tags` signal object: **458 of the dimension's 828 objects, 349
  of them "Reference"** — roughly 40% of the structured interest layer was that
  one string. The keyword-rule branch beneath it (edtech, ai_research,
  infrastructure, outdoors, privacy) is untouched and becomes the only path.

  Removed with it: `browser_url_classification` and its writer, the three job
  modules, the `website_classifier.py` compat wrapper, `build_url_classification_task`,
  `ModelSlot.URL_PIPELINE` and the backend's two inference paths, the catalog /
  registry / model-override / lab entries, the `/api/enrichment` test+backfill
  endpoints, and two readers that never fired anyway — `topic_clustering`'s ×3
  `url_category` term boost (members never carried the key) and the
  `url_category` fallback in stats grouping.

  Migration `retire_url_classification_v1` drops the table and deletes the tags,
  scoped by `payload_json.source_kind` so the rule-based tags and the personal
  `fact` objects that also live in `interests` survive. Rehearsed against a copy
  of a live node: interests objects 828 → 370, `fact` 6 → 6, other dimensions
  6,580 unchanged.

### Fixed

- `[S1]` **The Lab's "apply preferred model" for entities is applied, not just
  stored.** Overrides are saved per JOB id but looked up per engine SUBTYPE via
  `SUBTYPE_TO_JOB`, and `entities_job` emits `entity_extraction_batch` while the
  map carried only the bare `entity_extraction` — so picking a preferred NER
  model in the Enrichment Lab wrote the override, displayed it, and never used
  it. emo_27 hit the identical hole at its `_batch` rename and was patched by
  hand; entities was missed. Both forms are mapped now, and a new test derives
  the required entries from the Lab's own factory dict and each job module's
  actual `run_engine_task(subtype=...)` literals, so the next subtype rename
  fails a test instead of silently orphaning an override.
  (`topos/enrichment/model_overrides.py`,
  `tests/enrichment/test_model_override_subtypes.py`)

- `[S1] [E:retrieval]` **Attention-digest movers are date-qualified at read
  time.** The routine narrator reads the last ~10 daily digests, and a movers
  list carried no date of its own — so a one-day spike (28 visits to a new
  host on 07-31) was re-narrated in the present tense for 11 days as if it
  were news. Each movers fragment now carries its day and query-time age
  ("on 2026-07-31, 13 days ago — not current" / "yesterday" / "today"),
  computed when the object is served so the qualifier stays true however
  long it keeps being retrieved. A surprise is an event, not a state.
  (44bb721; entry deferred from that commit because the changelog was held
  by a co-resident session at the time.)

- `[S1] [E:embeddings]` **A journal row's signal dimension follows its origin,
  not its record type.** `signal_dimension` was mapped from record kind alone
  (`journal_entry` → `wellbeing`), and the GitHub connector writes one journal
  row per authored COMMIT — so the entire commit stream was stamped
  `wellbeing`. On one live node that was 123 of 765 journal embeddings, and
  because clustering facets by dimension, **19 of 163 topic clusters** came
  back named `Wellbeing Tracker (…)` over distinguishing terms like
  `merge branch`, `gitignore`, `build`, `layout`, `settings`. The labeler was
  not at fault and no amount of prompt work could fix it: asked to "name the
  state, rhythm or condition" for a cluster of commits, the dimension's own
  noun is the only answer left, and the parenthetical suffix ends up carrying
  all of the real signal. A commit is work; what a person writes in a journal
  is wellbeing. `dimension_for_record` now consults a `(record kind, source)`
  table before the kind-only one, so a single source's journal lane can differ
  from the rest without reclassifying anything per record.
  `journal_origin_dimension_v1` re-stamps the rows already on disk — unlike
  `signal_dimension_backfill_v1`, which only filled rows still at the `memory`
  default, this one matches the wrong value explicitly and leaves a dimension
  set deliberately to anything else alone. Clusters pick the split up at the
  next recompute, which the ingest job runner already triggers; nothing here
  forces a repartition, since a recompute reshuffles every cluster (two passes
  over one corpus agree only to ARI ~0.52).
  (`topos/features/signal/embed_context.py`)

- `[S1]` **A repeated cluster name earns its retry even when it arrives
  suffixed.** Not stacking the suffix stopped the runaway shape, but by the
  time `_disambiguated` sees a label the retry has already been skipped:
  collision was tested on the whole string, so a model answering
  "Social Connections (weekend)" where "Social Connections" was spoken for
  produced a brand-new string, matched nothing in `used`, and was accepted. The
  model was never told it had repeated itself and never got the chance to
  rename — it went straight to a deterministic suffix, which is a worse name
  than the one it was not asked for. And the prompt shows each cluster the
  names its siblings took, so a suffixed sibling is exactly what it copies.
  Collision now tests the base name alongside the whole label (`_collides`),
  and an assigned label claims both identities (`_register_label`) — including
  the term labels still carried by clusters the labeler skipped, which sit on
  the same surface. `_label_rank` uses the same test, or a rename would be
  bought by the retry and then discarded when the two disagreed. The eval gains
  `stacked_suffix_labels` beside `suffixed_labels`: one suffix is the
  disambiguator working, two is it running away, and that difference deserves
  its own number rather than hiding inside a count.
  (`topos/features/signal/cluster_labels.py`)

  How many labels share a base is still deliberately not capped — keeping the
  term label instead ("https / good / here") trades a weak name for a useless
  one, and a model that repeats itself must still get every cluster named.

- `[S1]` **Model readiness stops counting jobs ready because they are not LLM
  jobs.** `_model_readiness()` scored every non-LLM job ready unconditionally —
  "HuggingFace task models and rules jobs run in-process on any supported
  device", which is true only once the weights are on disk. A machine set up
  offline has nothing cached and was still reported at **100% model readiness**
  for every dimension those jobs feed, which is precisely the machine that most
  needs to be told otherwise. Readiness is now per job, from the model catalog:
  ollama jobs need a reachable server (and a model above the size minimum),
  HuggingFace jobs need their snapshot present in the local HF cache, rules jobs
  need nothing. The per-dimension profile gains `model_readiness_jobs` —
  `{job, provider, model, ready, reason}` per job — so a dark dimension can say
  *which* model it is waiting on ("the NER weights are not downloaded yet")
  instead of only scoring low. `data_health._LLM_JOBS`, a literal that
  duplicated what the catalog already states, is now derived from it via
  `jobs_for_provider("ollama")`. (`topos/enrichment/job_readiness.py`,
  `topos/features/signal/data_health.py`)

  Readiness and *holding* are deliberately separate questions:
  `readiness_of().ready` answers "can this run now" and is what a person is
  shown; `.blocking` answers the narrower "should queued work WAIT", and only a
  hard stop sets it. Uncached HuggingFace weights make a job not ready but never
  blocking — the first run downloads them, so holding that debt would strand
  work a networked node completes unaided.

- `[S1]` **A deferred derivation job now records durable debt.** Jobs report an
  unreachable provider by RETURNING `[{"_deferred": True, ...}]` rather than
  raising, and only the raise path called `record_failed_derivation()`. So the
  exact case the debt mechanism exists for — ingest with no model installed,
  install one a week later — wrote nothing to `pipeline_jobs`: the worker and
  `POST /signal/derivation-debt/retry` had nothing to find, and the only durable
  trace was an `ingest_audit` row naming the batch but not the job (the
  orchestrator's `deferred_jobs` list, which does name them, is dropped at end
  of batch). The deferral path now writes the same record as the raise path,
  idempotent per (batch, job) so re-ingesting while still offline re-queues the
  one row instead of stacking. Affects `topics` and `goal_extraction` on
  `ollama_unreachable`, plus the seven canonical jobs that defer on
  `database_unavailable`. (`topos/enrichment/orchestrator.py`)

- `[S1]` **A retry that defers again is no longer reported as recovered.**
  `retry_single_derivation()` checked only `results["errors"]`, which a deferral
  never reaches — so re-running a debt while the provider was still down fell
  through to `outcome: "recovered"` with zero rows created, discharging the debt
  and marking the queue row `done` while the data stayed missing. That made the
  retry claim to have repaired data it never produced, and it under-counted
  `pending_derivation_summary()`. The deferral is now `still_failing`, carrying
  the job's own reason. (`topos/enrichment/derivation_recovery.py`)

- `[D]` **`grand-cypher` pin moved to `>=0.13,<0.14`** (was `>=0.12,<0.13`, in
  both core dependencies and the `engine` extra). The ceiling is load-bearing
  rather than cautious: 1.0.0+ changed `RETURN a` to yield the whole node dict
  instead of the node id, which breaks `_rows_from_columns` and
  `test_entity_cypher.py::test_result_is_json_serialisable`. 0.13.0 is the
  newest release this code works against — the upper bound must not be raised
  without re-running that test. (`pyproject.toml`)

- `[S1] [E:embeddings]` **GitHub push events carry their commit messages.**
  `activity_events.content` was NULL on every push row (451/451 on the first
  live node checked, 11 distinct titles across all 451 — all of the form
  "{owner}/{repo}: pushed N commit"), because the mapper carried only
  repo/actor. Every semantic reader downstream could therefore only ever match
  the repo NAME: goal/interest attachment to engineering work was pure name
  matching, a contrastive term extraction over commit-attached items returned
  nothing but "pushed"/"commit", and the derived "creation" signal counted
  events that said nothing about what was created. `github_activity` now
  declares `activity_events.content ← payload.commits[*].message` (joined for a
  multi-commit push) in its source definition — the first bundled consumer of
  the declarative capability above. Event granularity is unchanged: one
  activity row per push, and — as of the journal-lane retirement below — that
  row is the only place the commit prose lands.

- `[S1] [E:embeddings] [E:entities]` **activity_events writes now persist
  `content` and `hostname`.** The `activity_events_content_v1` migration added
  both columns and the P2.1 browser mapper filled them, but the store's INSERT
  never listed them — so every value was discarded at the write (0 of 4,444
  rows populated on the first live node checked, browser highlight spans
  included). Both columns are in the INSERT and in the conflict update
  (COALESCE, so a blank batch never blanks a stored value), which also means a
  re-ingest or reprocess-from-raw heals rows written before this fix.

- `[S1] [E:embeddings] [E:entities]` The activity signal record and the
  signal-reload query now carry `content`, `hostname` and `metadata_json`.
  Without them an activity row embedded on its title alone, and the declared
  ENTITY mapping (§5a capability 4, `metadata_json.repo` → project +
  `worked_on` edge) was reading fields absent from the record it was handed.

- `[S1] [E:embeddings]` Runtime installs of bundled sources no longer shadow
  the bundled enrichment-lane policy. A 2026-05-29 `source_runtime_installs`
  row for `browser_visits` snapshotted the then-bundled definition
  (`enrichment_trigger='manual'`, no jobs); rehydrated at every boot it
  overrode the 2026-07-09 flip to automatic url_classification+embeddings, and
  once the manual gate landed on the signal lane every live browser push ran
  zero enrichment jobs while reporting "done" (August: 0/1,034 visits embedded
  or url-classified across 2,304 no-op inbox jobs). `browser_events`
  highlights, and the newer signal jobs on `chatgpt_ui_conversation`,
  `imessage`, `signal`, and `gcal_events` (availability_scores), were silently
  dropped the same way. Lane policy (trigger + job lists) now always resolves
  from `BUNDLED_REGISTRY` for bundled source ids; per-source toggles remain in
  `source_enrichment_overrides`.

- `[D]` **Topic-cluster labels are named against their siblings, under their
  dimension's question.** Every cluster was named by one prompt that saw only
  that cluster's own most frequent terms: no idea what the neighbouring
  clusters were already called, and the same generic instruction whether the
  cluster was built under the wellbeing lens or the interests one. For a
  personal corpus the most frequent terms are exactly the words every cluster
  shares, so labels collapsed toward the generic — one live node carried 152
  clusters under 91 distinct labels, "personal projects" on fourteen of them
  spanning five dimensions. Four changes: frequent terms become
  DISTINGUISHING terms (class-based TF-IDF — this cluster's share of a term
  against the rest of the set, so a word the whole corpus uses scores zero
  instead of leading every list); each prompt opens with the question that
  dimension exists to answer, read from its own
  `topos/features/signal/definitions/<dimension>.json`, plus one line saying
  what kind of name answers it; the prompt carries the names the nearest
  siblings already took, with clusters named in a deterministic order
  (dimension, then size) so those names exist when they are needed; and an
  answer that comes back generic, already-taken or as a link earns one bounded
  retry naming the problem, then a deterministic suffix that is checked against
  every name in play — including the term labels still carried by clusters the
  model declined. Measured relabel-only over the same 152 clusters with the
  same local model (`scripts/eval_cluster_labels.py`): **93 → 151 distinct
  labels**, worst duplication **13 → 2**, clusters carrying a duplicated name
  **75 → 2**, names duplicated across more than one dimension **10 → 0**. The
  control arm reproduces the live node to within two labels (93 vs 91 distinct,
  "personal projects" 13 vs 14), which is what makes the comparison the prompt
  and not the run. `TOPOS_CLUSTER_LABEL_CONTRAST=off` restores the isolated
  prompt (it is that control arm); `TOPOS_CLUSTER_LLM_LABELS=off` and the
  deterministic term-label fallback are unchanged.
  **Correction (measured later, same corpus): the 151 counts full labels, and
  a full label can carry the `(term)` suffix the deduplicator appends. Counted
  by BASE name — suffix stripped — the same run holds only 113 distinct names,
  with 41 labels suffixed and one base repeated fifteen times. So the honest
  figure for this change is 91 → 113 distinct base names, not 91 → 151, and
  the suffix inflates the word-count rule below by the same stroke. The eval
  now reports `distinct_base_labels` as its headline number.**
  (`topos/features/signal/cluster_labels.py`)

- `[D]` **A cluster label can no longer be a link.** Distinguishing terms are
  drawn from page titles and links, which invites the model to answer with one:
  a measured relabel returned a full `maps.app.goo.gl/…?g_st=i&utm_…` URL as a
  cluster label. Labels are published — into `top_topics` signal facts and every
  surface that lists topics — and the interests definition bans verbatim URLs
  there outright. URL, bare-domain, path and @handle answers now earn a retry
  and, if they survive it, are dropped in favour of the term label. An
  all-lowercase answer is title-cased deterministically (two thirds of live
  answers came back in prose case).

- `[D]` **A cluster label must be 2-5 words, checked and not just asked for.**
  The contrastive prompt opens with that rule and, measured over a full
  relabel, obeyed it LESS than the prompt it replaced: 46% of answers in range
  against the isolated prompt's 90%, single-word answers going 15 → 81
  ("Met", "America", "Friend", "Internet"). Nothing enforced it — `parse_label`
  capped the top at seven words and had no floor — while every other line
  pushed the other way, since each per-dimension directive asks the model to
  *name a thing* and distinguishing terms arrive as bare tokens. Bare proper
  nouns are unique, so this was invisible in the duplication metrics: the
  labels got more distinct and less informative at the same time. A
  wrong-length answer now earns the same one bounded retry the generic and
  link answers do, naming the specific fault ("\"Austin\" is one word. Keep it
  and add what about it"). Length ranks BELOW duplication and genericness when
  the retry is scored, so a distinct bare noun still beats a repeat, and a
  stubbornly terse answer ships rather than reverting to `https / good / here`.
  Measured on the same 152 clusters: rule compliance **46% → 77%**, single-word
  labels **81 → 34**, with distinctness unharmed (151 → **152 of 152**, worst
  duplication 2 → 1) and banned words still **0%**. Still short of the control
  arm's 90%: the 34 that remain are mostly real proper nouns (`Gmail`,
  `Brooklyn`, `Smithers`, `Dialoguesai`), which the rule above arguably wants,
  alongside a weaker tail of bare common nouns (`Friend`, `Food`, `Account`).
  `scripts/eval_cluster_labels.py` reports `word_rule_share` and
  `single_word_labels` so this cannot regress unnoticed again.

- `[D]` **Repeated term labels are disambiguated against each other, not just
  counted.** The suffix for a repeat was the cluster's member_count, which is
  not unique: two 7-member clusters that both reduced to "hello" both became
  "hello (7)", and a live node carried exactly that pair. Every candidate
  suffix is now checked against the labels already handed out, with the
  occurrence index as the fallback that cannot collide. This was the only
  duplicate the LLM labeler could not resolve, because it inherits the term
  label whenever the model declines a cluster.

- `[D]` **Existing clusters can be relabeled without being re-clustered.**
  `relabel_existing_clusters()` runs the labeler over the clusters already on
  disk and writes back labels only — membership, cluster ids and centroids are
  untouched, and the deterministic term label is preserved in
  `metadata.term_label`. A prompt change owes existing nodes new names, not a
  new partition: two k-means passes over the same corpus agree only to ARI
  ~0.52, so shipping this through a recompute would churn every cluster id,
  `top_topics` fact and UI link with it. Reachable as the `derived_rebuild`
  target `topic_cluster_labels` and as the release step
  `recompute-topic-clusters-split`; `scripts/eval_cluster_labels.py` drives the same
  path against a temp copy of the database to A/B two prompts over one fixed
  partition. **Clusters keep their old names until that step runs**, and this
  build reaches a node by reinstall (frozen uv tool snapshot), not restart.
  (`topos/features/signal/topic_clustering.py`, `topos/upgrades/runner.py`)

- `[D]` **A topic cluster label is now a black-hole surface.** Making labels
  specific made them name people: 33 of 152 labels on a live node came back as
  an entity name (six people, four places) where the old term-soup labels named
  nobody. The black-hole battery had no token in `topic_clusters` at all — an
  unenumerated leak surface — and the withdrawal job's list stopped at the
  derived `top_topics` object, which is minted *from* `topic_clusters.label`, so
  a rebuild's withdrawal only lasted until the next cluster pass. Both
  directions of time are now covered: the labeler refuses to mint a name in the
  owner's off-limits set (one retry that never echoes the rejected name back
  into the prompt, then the deterministic term label), and the rebuild withdraws
  labels, centroid previews and member excerpts written before the entity was
  protected. The cluster, its membership and its counts survive — withdrawal
  takes the prose, not the owner's structure. `rerun-blackhole-rebuilds` applies
  it to entities already marked complete, which `run_pending_rebuilds` skips by
  design: on the live node three completed rebuilds still had **seven member
  excerpts quoting a protected entity** (five for one, two for another).
  Probes: `tests/evals/privacy/blackhole/test_cluster_labels.py` (16), plus the
  surface and its two canary tokens in the battery's corpus.
  (`topos/features/signal/cluster_labels.py`,
  `topos/features/lifecycle/blackhole_rebuild.py`)

## [1.3.15] — 2026-08-13

### Added

- `[O]` Select Topos in the menu-bar/system-tray icon, so machines without the
  desktop shell can switch between the Topoi they hold. The submenu lists every
  Topos with its size, ticks the active one, and offers New Topos. This tray has
  no supervisor — run in-process it IS the node — so a click queues the action
  and stands the server down; the swap runs once the port is free and the
  process re-execs onto the chosen Topos. A tray *attached* to a node it did not
  start is not offered the row at all: stopping a node it cannot restart would
  move a database out from under the machine and leave nothing serving. New
  Topos deliberately does not re-exec, because a node with no key exits within a
  second and would bury the pairing instructions under an error.

## [1.3.14] — 2026-08-12

### Added

- `[O]` `topos-node profile` — multiple Topoi on one machine. `list`/`current`
  show the active Topos (top-level `~/.topos`, unchanged layout) and archived
  ones under `~/.topos/profiles/<slug>/`; `new` archives the active Topos and
  leaves the machine fresh for a new pairing (the zero-click "New Topos"
  primitive — pairing can no longer hit the already-bound refusal); `switch`
  swaps the active Topos for an archived one. Only an explicit allowlist moves
  (`.env`, `database.db` + WAL/SHM sidecars, `ingestion/`, `nightly/`,
  `config.yaml`) — backups and scratch files stay put, and logs stay
  machine-global. Every rename is journalled, so a crash mid-switch rolls back
  to the pre-switch layout on the next profile command. Refuses to run while a
  node answers on the port or a database rebuild lock is present. `adopt`
  copies a legacy (`~/.topos_engine`, Application Support) database into an
  empty active slot for pre-profile machines. No derived data is invalidated —
  a profile switch changes which files sit at the top level, nothing else.

- `[O]` LLM stream protocol v1 (PLAN_HOME_CHAT_STREAMING_SLA P1). Streaming
  `llm_generation` now resolves `think` like the non-stream path (default OFF,
  capability-probed, budget floored to 2048 on explicit think-on) instead of
  leaving reasoning models to burn the whole `num_predict` on chain-of-thought
  the loop then discarded — the 2026-08-11 home-chat stall. Thinking tokens are
  forwarded as typed `kind:"thinking"` chunk frames; the handler emits an
  `ack` frame before model work and phase-aware `heartbeat` frames while the
  stream is silent (all interim traffic stays `status:"chunk"` with an empty
  `delta`, which older control planes provably no-op). An exhausted budget is
  a typed `thinking_budget_exhausted` error, never an empty success; a
  truncated oversized prompt gets a soft `context_truncated` verdict on the
  result. New `llm_cancel` message aborts an in-flight generation (the httpx
  unwind closes the Ollama connection, freeing the decode slot) and the
  original request answers a typed 499. Chat generations set
  `keep_alive=30m` so a follow-up question does not pay a cold reload.
  Registration and heartbeats advertise `llm_stream_protocol_version: 1` for
  control-plane feature detection. No stored data is touched.

- `[O]` `ollama_list_installed` / `ollama_pull` / `ollama_pull_status` message
  handlers, so the control plane can seed model packs from what is actually
  installed on this node and drive a model download with typed progress.
  Registration now reports the installed-model set honestly (installed /
  empty / unknown — never unknown-as-empty), and the node-side pack config
  readers (facts, conversation context, signal extraction, sanitization)
  fall back to the engine default instead of a pack tag that is not
  installed. No stored data is touched.

## [1.3.13] — 2026-08-11

### Fixed

- `[S1] [E:entities]` The People tab's duplicate queue asks each question once.
  `entity_review` holds a row per *sighting*, and the resolver queued one every
  time a surface reappeared: 8,134 pending rows for 99 real decisions on a live
  node, the same four pairs repeating down the page, with the genuine person
  merges sorted below all of it and off the visible page. Everything
  owner-facing now keys on the decision rather than the row — the page limit
  counts questions, resolving one settles every row that asked it, and the count
  reports the backlog instead of the page size. Existing queues collapse by
  migration; every prior answer and unbind guard is preserved.
- `[S1] [E:entities]` Three reasons a duplicate question should never have been
  asked. Graph derivation minted vertices through the same call path as ingest
  and asked about strings the owner never said (97% of the rows). A candidate
  never seen in the data is not something to merge into — contacts included,
  because an address book is not a list of important people, and 35 of 39 open
  contact questions offered a never-mentioned contact. And `"Altman's"`,
  `"Williamsburg-"` and text redacted to `[NAME]` are no longer identities.
- `[E:entities]` Orphan cleanup stops reaping the vertices derivation just
  minted. They carry ordinary `ent_` ids, so neither the type nor the id-prefix
  exemption saw them: every scrub deleted 173 mention-less entities on a live
  node and cascaded away the 206 edges that made them load-bearing, and the next
  derivation run rebuilt them. An edge is reason enough to keep a vertex.
- `[S1]` Search returns one result per thing said, not per time it was stored.
  The same text is embedded once per sighting, and 37% of a live corpus (2,967
  of 8,061 embeddings) is duplicate text, so `"GitHub"` took four of five slots
  for the query `git github`. Results now collapse on content hash, keeping the
  best-scoring copy and reporting how many sightings it stands for in
  `occurrences`. The candidate fetch widens to match, and the collapse runs
  before reranking so the cross-encoder's budget is spent on distinct text.
- `[O]` Five documented environment variables were dead. pydantic v2 ignores
  `Field(env=)`, so `TOPOS_FACTS_LLM_MODEL`, `TOPOS_OLLAMA_QUERY_MODEL`,
  `TOPOS_OLLAMA_EXTRACTION_MODEL`, `TOPOS_DISCLOSURE_MINIMIZER_MODEL` and
  `TOPOS_PRIVACY_JUDGE_MODEL` never reached `Settings` despite being documented
  as the way to set them.

## [1.3.12] — 2026-08-10

### Fixed

- `[E:graph]` The entity graph stops dating relationships by when the machine
  touched them. Four defects, one theme. (1) `rebuild_evidence_edges` deletes
  and re-inserts the whole co-occurrence set and stamped a fresh `now` on every
  row, restarting each edge's belief clock — 492 edges on a live node claimed to
  have begun at whichever rebuild happened to run last, and the prior date was
  gone for good. `valid_from` is belief validity, not an event date, so the
  prior date is now carried across the swap and only genuinely new edges begin
  believing now; it is deliberately NOT clamped to the evidence date, which
  would assert a belief history that never happened. (2) Materialized topic
  edges carried `actor_role: authored` without exception: nobody asserts a topic
  cluster, so they passed no `asserted_by` and the fallback's "owner" default
  claimed the owner wrote them — putting browser and GitHub-feed exposure in the
  owner's own voice and reading 91.5% authored on a window where it was a
  fraction of that. The role now comes from the cluster's member records via the
  same role map the co-occurrence edges use, so a topic edge and an organic edge
  over the same records cannot disagree; on the measured node 34 clusters moved
  from 34 authored to 2 authored / 15 observed / 17 ambient. (3) `entry_at` was
  written from the import clock while the true session time sat in `starts_at`
  (309 rows across two sources, 127 sharing one second), so months-old sessions
  looked like they happened at import and pulled years-old relationships into
  the recent graph window; the canonical store now prefers `starts_at` when
  `entry_at` matches `ingested_at` to the second, and an upgrade step re-dates
  rows already on disk. (4) DATE/TIME/CARDINAL surfaces ("an hour", "this week",
  "four", "Mon-Wed") were minted as first-class topic nodes: the extraction lane
  already drops value labels, but the graph's second minting lane resolves bare
  surfaces with no label attached, so `map_ner_type` never ran. Both lanes now
  consult the labels the model already assigned — a surface counts as a value
  only when value-labelled mentions outnumber identity-labelled ones, so a real
  entity mislabelled once survives — and 51 previously-minted junk nodes are
  purged. Verified end-to-end on a live node: 20 junk nodes to 0.

- `[O]` A failed upgrade step is retried again, and the banner that promises
  the retry stops lying. Three defects stacked. (1) The runner planned purely
  from `steps_between(baseline, shipped)` and filed ledger rows under the
  SHIPPED version rather than the release that declared the step, so a failure
  detached from its step: `backfill-attention-triage-redo` (declared 1.3.7)
  failed under shipped 1.3.9, the baseline went on to 1.3.11, and the step left
  the version window permanently — unreachable by the planner and by
  `POST /v1/upgrade/consent`, which rejects anything not in the current plan.
  Rows are now keyed to the declaring release, the plan carries any step whose
  ledger row never reached `done`, and the baseline stamp is gated on that same
  set, so a release with no steps of its own can no longer stamp straight over
  an outstanding failure. A step with no row at all is still never dragged
  back — fresh installs stamp without running history, and re-running all of it
  would be far worse than the gap. (2) `UpgradeBanner` filtered the ledger on
  `status === "failed"` with no version scope and no check against
  `pending_steps`, so one historical row pinned "Upgrade step failed — will
  retry on next restart" to `/data` forever, and the step counter drew its
  numerator from every `done` row the node had ever written. Both are now
  scoped to the current plan, and the retry sentence only appears when a retry
  is genuinely planned. (3) The step had died on its progress bar, not on any
  data work: `ProgressBar` writes to `sys.stderr`, and upgrade steps run in a
  daemon thread ~20s into boot, where a detached node (`--app --no-tray`) can
  have stderr closed — `__enter__`'s first draw raised
  `ValueError: I/O operation on closed file.` six seconds in, before a single
  source was touched. Display writes are now swallowed and disabled after the
  first failure, which also covers the ingestion manager, the iMessage reader,
  and the emo-27 job.

## [1.3.11] — 2026-08-09

### Fixed

- `[O]` The conversation auto-classifier stops re-asking the same question
  forever. It ran on every enrichment batch, took the newest 20 conversations
  with no `context_tag`, and asked the local model "work or personal" — but
  when the model answered `unclear`, nothing was written. Those rows stayed
  untagged, sat at the top of the same `created_at DESC` window, and were
  re-sent verbatim on the next batch. Measured on a real node: ~20 model calls
  and ~26 seconds burned per derive batch, permanently, for zero progress —
  the token counts repeat in identical order across passes. 64 of the 68 stuck
  conversations were `test-dataset` fixtures with one or two messages, far too
  thin to ever label, so they taxed every real batch. An unclear verdict now
  retires the row with an `unclear:n<k>` marker recording how many excerpts
  were judged; `context_tag` still stays NULL, so nothing is guessed. A retired
  conversation becomes a candidate again only once it has grown new excerpts to
  judge, and since the excerpt read caps at 8 a conversation at that cap can
  never pose a different question and never returns. Retired rows only ever
  fill budget left over by never-asked ones, so a backlog cannot crowd out new
  work. Engine failures are now distinct from an unclear answer and never
  retire anything — an Ollama outage previously looked identical to "the model
  declined", which would have buried 20 rows that were never actually judged.
  Verified against a copy of the live database: 4 passes to drain a 68-row
  backlog, then 0 model calls per batch, down from 20 forever.

## [1.3.10] — 2026-08-09

### Fixed

- `[O]` The control-plane connection no longer drops every 50 seconds. 1.3.9
  restored a pong deadline so a silently dead socket could not wedge the node
  forever, but 20s was far tighter than the engine can meet: measured live,
  pongs round-trip through the tunnel in ~0.2s and the app loop answers
  healthchecks throughout, yet the node still could not always *process* its
  pong inside 20s. The link then died on a metronome — 20s to first ping + 20s
  deadline + 10s close handshake — 18 times in one session, and each drop took
  whatever was in flight with it, including a user's chat message: submitted,
  processed, then gone, with no trace in history. The deadline is now 30s
  interval / 90s timeout: far past any observed local stall, still detecting a
  genuine death inside ~2 minutes, and inside the control plane's own eviction
  (30s + 120s) so the node notices first and reconnects itself. Verified on a
  real node: zero drops and a single stable connection across 15 minutes, where
  the previous build produced a drop every 50 seconds.

- `[O]` In-app "Update to vX" works for app-installed nodes. A Finder-launched
  app inherits `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, where `uv` never lives, so
  `uv tool upgrade` raised `FileNotFoundError`, the worker's `finally` stamped
  `last_result="failed"`, nothing reached the log, and the menu silently
  reverted to offering the update again — indistinguishable from a dead button.
  In-app update had therefore never once worked for a DMG install.
  `resolve_uv_binary()` now checks `TOPOS_UV_BIN` (which the macOS shell 0.2.13
  passes from its bundled copy), then `PATH`, then the usual install locations;
  failures log the reason and the `PATH` that was searched, and `OSError` is
  caught instead of killing the worker thread.

### Changed

- `[O]` Tray parity with the macOS shell, for the Windows port that wraps it:
  "Quit Topos Node" stops the node whether this process started it or merely
  attached (previously an attached tray offered only "Close Tray (node keeps
  running)", stranding anyone whose tray attached after a crash or an update
  restart with no way to ever stop the node); the tray-only exit survives as an
  explicit second item shown only when attached; a failed update says so
  instead of rendering nothing; the badge spins while the node is starting; and
  the bound Topos's own name appears in the menu.

## [1.3.9] — 2026-08-09

### Fixed

- `[S1]` `public_bio:read` and `work_context:read` can reach profile data
  again. The 2026-07-03 registry sync (`bee78d5`) replaced the engine scope
  registry wholesale from the control plane and dropped `profile_records` from
  both scopes, leaving no scope in the registry able to reach ingested
  resume/profile data. `public_bio:read` was left with no readable lane at all
  — its only declared surface was the summary object `public_bio`, a name no
  migration creates, and retrieval never reads `summary_objects` (they declare
  which access modes a scope supports, nothing more), so it answered every
  query with `{"summaries": []}` while advertising
  `implementation_status: "live"`. `profile_records` is restored to both scopes
  upstream in `control_plane/uma/scope_registry.json` and re-synced; the
  generated `scopeCatalog.ts` is unchanged, because the frontend's view of both
  scopes stays summary-only. It is declared as `canonical_tables`
  rather than `raw_tables` deliberately: both manifest builders read
  `raw_tables or canonical_tables` for the content lane, but only `raw_tables`
  feeds `_max_supported_access_mode`, so using it would have lifted both
  scopes from their declared `summary` ceiling to `raw` and un-denied the
  inference and raw requests `TestQueryPipelineDenyPressure` exists to reject.
  The engine's retrieval already carried the `profile_records` handling this
  depends on — the certification and prior-employer filters name the table and
  the `work_context:read` scope directly. Harness quality on the seeded gate
  DB goes 35/70 → 42/70 with no regressions and no change to any denial, and a
  new `tests/query/test_scope_registry_surfaces.py` fails any live scope that
  declares no lane retrieval can read.
- `[S1]` Derivation-retry jobs actually run. `record_failed_derivation()`
  enqueues work of kind `signal_derive_retry` so a lost derivation can be
  re-derived, but no executor was ever registered for that kind: the worker
  claimed each row and immediately failed it with `Unknown job kind:
  signal_derive_retry`. Every recorded derivation debt was therefore
  self-cancelling — the retry that was supposed to repair the gap destroyed
  the record of it instead. `run_derivation_retry_job` is added and wired into
  `EXECUTORS`, and `tests/pipeline` now asserts the registration the way
  `topic_consolidation` already did (the assertion that would have caught
  this). Workers also claim only kinds they can execute, so a row written by a
  newer node version stays queued and visible instead of being claimed and
  failed as unknown, and an executor may return `status: "requeue"` to hand a
  claim back untouched when it declines to run — a deferral must not read as a
  failed attempt. Registering the executor does not revive rows already parked
  `failed`, so this release also carries an `auto` upgrade step
  (`retry-recorded-derivation-debt`) that sweeps them once: nothing else would
  — a failed row is only re-queued by the next organic failure of the same
  (batch, job), and `recover_stale_jobs` resets `running` rows, never `failed`
  ones. Nodes with no recorded debt run it as a no-op.

### Changed

- `[O]` `just harness-gate` scores answer quality against a `QUALITY_FLOOR`
  ratchet instead of a flat 70/70. Permission and Signal stay hard 70/70 —
  those are disclosure and retrieval-liveness checks and do not depend on how
  derived the database is. The 70/70 quality baseline was recorded on a fully
  derived node; the gate seeds a throwaway DB where the vector and cluster
  layers are switched off by design, so a share of the catalog is
  unanswerable there by construction and the gate could never go green. The
  floor is the high-water mark on that environment (now 42/70, measured on
  main at `39bf20b`) and the recipe says so when a run beats it.

## [1.3.8] — 2026-08-09

### Added

- `[O]` Open-transaction watchdog behind `TOPOS_DB_TXN_WATCHDOG=1`
  (threshold via `TOPOS_DB_TXN_WATCHDOG_SECONDS`, default 5s). The existing
  ungated-write diagnostic only fires at commit time, so a caller that
  writes and NEVER commits was invisible — while still holding SQLite's
  RESERVED lock, which makes every other connection's first write wait out
  the full 30s `busy_timeout` and fail `database is locked`. That blind
  spot hid `StatisticsJob._should_promote` (an ungated, never-committed
  `UPDATE stat_state`) wedging the whole signal lane on repeat ingests, and
  diagnosing it took a throwaway `sqlite3.Connection` subclass plus a
  polling thread. Now a daemon thread reports any connection in-transaction
  past the threshold outside the gate, naming the call site that opened it,
  one rate-limited WARNING per site. Default off: it costs +1.6us per
  statement on Python 3.10 (83% of that sqlite3's own trace-callback
  trampoline), and a diagnostic that taxes the hot path becomes its own
  outage. Off, it is one boolean test per connection open.

### Fixed

- `[O]` The graph refresher no longer bumps the dirty generation on a
  connection another thread owns. `mark_graph_dirty` persists from a
  default-executor thread on the stated promise that the thread "fetches its
  own connection" — true for a file-backed database, and impossible
  otherwise: an in-memory database hands out the OWNER's connection, because
  a per-thread copy would be empty, and tests inject a single handle of their
  own. Writing anyway raced whoever already held it. A `sqlite3.Connection`
  carries exactly ONE transaction state, and `with_db_write` serializes
  writers, not readers, so nothing excluded the other thread; on CPython 3.12
  that took the whole test lane down with SIGSEGV — a crash, not an
  exception, with no failing test to point at. The ownership check now lives
  where the SQL is: the write proceeds on this thread's own connection, or on
  the owner thread, and is skipped otherwise. A file-backed node is always
  the first case, so the persist stays off the event loop exactly as before.
  This had been latent since the refresher landed; moving ingest and
  enrichment writes onto worker threads changed the timing enough to make it
  land nearly every run.

- `[O]` In-app update works for DMG installs. It never had: a GUI-launched
  app inherits `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, `uv` lives in
  `~/.local/bin`, so the update subprocess raised `FileNotFoundError`, the
  worker stamped `last_result="failed"`, nothing reached the log, and the
  tray silently reverted to "Update to vX" — reported as "I click it and
  nothing happens". `resolve_uv_binary()` now checks `TOPOS_UV_BIN` (set by
  the macOS shell from 0.2.13), then PATH, then the usual install
  locations, and finally the app's own bundled uv at
  `/Applications/Topos.app/Contents/Resources/uv` — the only copy a user
  who never installed uv by hand actually has. Failures log the reason and
  the PATH searched instead of vanishing, and the tray offers "Update
  failed — click to retry".
- `[O]` Restore the control-plane pong deadline (20s interval, 20s
  deadline). `ping_timeout=None` was a workaround for handlers blocking the
  client loop; giving the client its own thread removed that reason, but
  the workaround stayed and was strictly worse — with no deadline a
  silently dead socket is never noticed, so the node reports "connected"
  indefinitely while the control plane holds no socket for it. Observed
  live 2026-08-08: wedged 25 minutes, every proxied request 503, no
  recovery without a restart. The deadline sits inside the control plane's
  own 120s eviction, so the node notices first and reconnects itself.
- `[O]` The `statistics` signal job no longer fails repeat ingests with
  `always-run migration 'wiki_mvp_phase1' failed: database is locked`. Two
  faults compounded. Its debounce counter (`_should_promote`) issued an
  `UPDATE stat_state` outside the write gate and never committed it, so the
  event loop's connection took SQLite's write lock at execute time and held
  it for the rest of the pipeline; every other thread's first write then
  waited out the full 30s `busy_timeout` and failed. And
  `ensure_migrations_applied` re-applied every `always_run` step on each
  call — it runs from `AdapterFactory.create`, i.e. per batch, per worker
  thread, per connection — so an ingest issued hundreds of gated write
  transactions to re-assert schema that was already there, giving that
  stuck lock the widest possible blast radius. The counter update is now
  gated and committed, and the runner memoizes connections already at the
  registry head, keyed on `PRAGMA schema_version` so that any DDL re-arms the
  `always_run` steps — they are not just re-assertions, they ALTER tables
  that legacy DDL creates *after* the first run (`CanonicalTablesManager`
  builds `ai_chat_conversations` without the provenance columns and
  `wiki_mvp_phase1` adds them on the next pass). Measured on a three-ingest
  dev node: 14 `database is locked` → 0, two failed signal batches → 0, and
  ingests 2–3 went from 0 signal jobs in 308s/171s to all 10 in 30s/21s.

### Changed

- `[O]` CI no longer runs the Home chat Wave A retrieval-quality harness:
  it drove scripts under the gitignored `demo/`, so it failed on every run
  from 2026-07-05 and masked the privacy-eval, package-metadata and
  fresh-install smoke steps behind it. The harness moves to
  `just harness-gate`; no test coverage was lost (its pytest files are in
  the public lane).

## [1.3.7] — 2026-08-08

### Fixed

- `[O]` The write-gate / event-loop deadlock class is gone. The control-plane
  websocket client now runs on its own thread and event loop, so a busy app
  loop can never starve the keepalive and make a healthy node look offline;
  `get_device_info` is answered from a snapshot when the app loop is
  saturated. Previously one long write hold could freeze the node until
  SIGKILL (observed 2026-08-07).
- `[O]` The entity-graph rebuild runs in a subprocess. The 100–160s rebuild
  used to starve the event loop through the GIL even with the gate released —
  healthchecks went dark for ~100s per rebuild. Verified live: a full 120s
  rebuild with zero missed healthchecks. The rebuild also folds edges in
  memory and swaps them under one short gate hold, and the rebuild endpoint
  itself runs off the loop.
- `[O]` Every SQLite write+commit path in the tree (73 files, including the
  migration runner and all enrichment/feature writers) now holds the process
  write gate around its statements *and* its commit. Ungated writes inverted
  lock order against gate holders and stretched writes into 30s busy-timeout
  standoffs ("database is locked" batches). Diagnostics now warn on gate
  acquisition from the event-loop thread and on commits whose statements ran
  ungated, so regressions name their call site.
- `[O]` Startup DB work (stage9 column renames + source-install rehydration)
  runs on one dedicated worker thread, cancellation-safe, with pipeline
  recovery armed only after it completes — startup no longer blocks the loop.
  The owner-connection validate/evict/swap in `core.state` is serialized
  behind a lock, fixing a cross-thread use-after-close that could crash the
  process (SIGBUS) when the database path changed at runtime.
- `[O]` Enricher gate holds shrunk: goal-embedding compute moved outside the
  gate hold, dossier refresh chunked per entity, and slow holds are named in
  the log instead of appearing as anonymous multi-second stalls.
- `[O]` Dev tray (`topos/cli/tray.py`): quit means quit — quitting stops the
  node it attached to instead of leaving it running; the node row shows the
  Topos's own name.
- `[O]` The 1.3.0 `backfill-attention-triage` upgrade step was a guaranteed
  no-op on every node: `attention_triage` lives in `SIGNAL_DERIVATION_JOBS`,
  but the runner routed it through `run_canonical()`, which filters
  `job_names` against `CANONICAL_JOBS` — so the step ledgered `done` with
  `jobs_run=0` and wrote zero `triage_verdicts`. The runner now splits
  manifest-declared jobs by owning registry and routes signal-registry jobs
  through the narrow signal lane (`signal_job_names` →
  `run_post_canonical_pipeline(job_names=..., run_enrichment=False)`),
  without the full `include_signal` LLM fan-out. Unknown job names are
  logged and recorded in the ledger detail instead of silently no-opping.
  A `backfill-attention-triage-redo` manifest step heals nodes that already
  upgraded through 1.3.0–1.3.6 with the bogus green ledger row (measured
  ~0.7s per day of history; no LLM, no network; idempotent upserts). The
  upgrade-matrix CI job now asserts `triage_verdicts > 0` instead of
  carrying the step in `KNOWN_NO_OP_STEPS`.

## [1.3.6] — 2026-08-06

### Fixed

- `[O]` Never leave a SQLite transaction open after 0-row UPDATEs in legacy
  isolation mode (derivation retry, privacy layer, contact-name resolve, PII
  redaction, startup app_id migration). An open transaction was poisoning the
  writer so topic clustering `BEGIN IMMEDIATE` failed every batch and derived
  topics never landed.
- `[O]` Coalesce queued `inbox_deferred_enrichment` jobs for the same source
  into one derive batch (cap 100), so a post-downtime backlog pays per-batch
  work once.
- `[O]` Run `TopicClusterJob.enrich` off the event loop so control-plane
  keepalive stays responsive during k-means / label-pool work.

### Added

- `[O] [P]` Sanitization prewarm reports download/load progress (`phase`,
  `cached_bytes`, `first_run`, per-model status) on `/v1/upgrade/status` and
  `/v1/shell/status`, so a first install's ~2.9GB Hugging Face fetch is visible
  instead of looking hung.
- `[O] [P]` `system.computer_name` from macOS `ComputerName` (cached once per
  process) so fleet/UI can name a node after the machine the owner recognises.

## [1.3.5] — 2026-08-06

### Fixed

- `[D]` Pin `torch<2.13`. torch 2.13.0 — a fresh resolve inside the previous
  `>=2.8,<3` range — segfaults (`SIGSEGV` in `mps copy_cast_kernel`) when the
  embedder, privacy filter and NSFW classifier touch MPS concurrently, which is
  exactly what a node does while answering its first query. Every new install on
  Apple silicon crashed on its first chat message; nothing before that point
  failed, so boot and control-plane checks all passed first. Reproduced with two
  encode threads plus a pipeline load (exit 139 on 2.13.0, clean on 2.11.0) and
  guarded by the `a8b` check in the `topos-install-qa` skill.

## [1.3.4] — 2026-07-29

- `[S1] [O]` Coverage `spec_version` columns + catalog `JOB_SPEC_VERSIONS`
  (PLAN_NODE_RELEASE_MIGRATIONS M3); writers stamp; anti-join treats NULL as 0.
- `[O]` Upgrade step kinds `canonical_reprocess` / `derived_rebuild` /
  `reembed`; `consent: prompt` → `pending_consent` + `/v1/upgrade/consent`.
- `[O]` Upgrade-matrix CI fixture builder + catch-up runner (M4); seeded-DB
  release smoke; device_info upgrade summary fields for fleet (M5).

## [1.3.3] — 2026-07-29

### Added

- `[S1] [O]` Single migration registry (`MIGRATIONS`), `PRAGMA user_version`
  fast-path, fail-loud schema migrations, pre-migration backup under
  `~/.topos/backups/`, downgrade guard when `user_version` is ahead of this
  build (PLAN_NODE_RELEASE_MIGRATIONS M0–M2).
- `[O]` `scripts/cut_release.py`, `check_release_artifacts.py`,
  `sync_migration_checksums.py`, `RELEASING.md`.
- `[P] [O]` Attention triage: `signal_pin_intent` / `signal_retire_intent`
  control-plane handlers.

## [1.3.2] — 2026-07-29

### Added

- `[S1] [O]` Entity black hole owner controls + turn-level taint.
- `[S1] [O]` Complexity data-page engine (summary / timeline / topics / influence).
- `[O]` App-mode shell contract (attach-don't-double-start, file logs, tray over HTTP).
- `[O]` Unified timeline daily reads; scope-registry sync.

## [1.3.1] — 2026-07-29

### Fixed

- `[O]` Prioritize UI bootstrap over upgrade reprocess (patch anchor; no
  derived-layer invalidation).

## [1.3.0] — 2026-07-29

### Added

- `[S1] [E:attention_triage]` Attention triage (daily 2×2 verdicts, badges/ranks, dashboard).
- `[O]` Negotiable time-signal availability (flex / rhythm / commitments).
- `[S1]` Conversation context tags (work / personal).
- `[O]` Claim verification / fact verdicts; facts-LLM think gating.

## [1.2.7] — 2026-07-22

### Fixed

- `[O]` Temporal coherence and client-local now for relative dates.

## [1.2.0] — 2026-07-10

### Changed

- `[E:entities] [D]` NER wordpiece stitcher — re-extract entities + rebuild
  entity graph on upgrade (see manifests.json `1.2.0`).
- `[S1]` Temporal entity graph layers, provenance roles, Louvain neighborhoods.

## [1.1.0] — 2026-06-01

### Added

- `[S1] [D]` Dense intelligence upgrade (stats engine, entity spine, fact
  store, incremental clustering, query planner). Anchors the upgrade ladder.
