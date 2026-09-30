"""p2c-v1 node adapter: permitted-set message search under a signed grant.

Order of work (plan §4):
  1. no gate   parse the intent and the signed search envelope; recipient relay principal
  2. gate      protection sync; ledger admission (authority, signature, request hash, replay)
  3. no gate   the grant's policy; grant-level bounds (k, window); sweep; load the grant's index
  4. no gate   embed the query (never through the shared query-embedding cache)
  5. no gate   rank inside P with the closed lanes
  6. gate      one canonical read: authority and floor re-checked as p2a does; for each candidate, its
               witness fact re-qualified and re-decided by release.source_message_decision, the very
               function the locator door uses; only `permit` releases; window/NSFW/size from the same row
  7. gate      closed view, byte budget, one set decision, checkpoint (receipt v3)
  8. no gate   sign; the caller sends after this returns, with every gate released

Discovery is a subset of access by construction: nothing reaches the output
unless step 6 decided `permit` for its fact in the read that is checkpointed.
The linearization point is the checkpoint, not the send (design §7 R12).
No model, fallback query engine or owner query lane is reachable here.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from topos.principal import THIRD_PARTY, current_principal
from topos.disclosure.content_policy import is_record_nsfw
from topos.storage.db.write_gate import with_db_write

from .canonical import PolicyError, canonical_bytes, digest
from .evidence import _key
from .fact_eligibility import canonical_utc_microseconds
from .forwarding import ReleaseBody, sign_node_result
from .identity import SUBJECT_CONTRACT_BY_CAPABILITY
from .opaque_ids import opaque_record_id
from .protection_clock import clock_state
from .contract import VIEW, VIEW_OPAQUE, MessageDisclosure
from .registry import OpaqueMessageDisclosure
from .release import MAX_DISCLOSURE_BYTES, source_message_decision
from .search_contract import (CAPABILITY_SEARCH, MAX_RECORD_CHARS, MAX_SEARCH_BYTES, REQUEST_TYPE_SEARCH, VIEW_SEARCH,
    MessageSearchResult, SearchIntent, SearchMemberBinding, SearchSetDecision, signed_payload,
    CAPABILITY_MESSAGE_SEARCH, SEARCH_CAPABILITIES, DirectSearchMemberBinding, search_decision_class, search_evaluator)
from .search_contract import CAPABILITY_KNOWLEDGE_SEARCH, DIRECT_SEARCH_CAPABILITIES
from .knowledge_contract import KnowledgeSearchResult, KnowledgeMemberBinding
from .search_index import SearchVerification, index_path, unseal
from .search_lanes import rank
from .signing import (AuthorityBinding, SearchRequestContext, SignedSearchEnvelope, parse_authority, parse_envelope,
    verify_current_signature)

CANDIDATE_FLOOR = 50
MAX_BATCH_ITEMS = 6  # design §2.2: the FE's MAX_SEARCHES; the CP refuses more before issuance


def parse_search_envelope(raw) -> SignedSearchEnvelope:
    envelope = parse_envelope(raw)
    if not isinstance(envelope, SignedSearchEnvelope):
        raise PolicyError("unsupported_capability")
    return envelope


def default_embedder(query: str, model: str):
    """The node's own embedding model, query role. Never the process-wide query cache."""
    from topos.engine.backends.huggingface import HuggingFaceAdapter

    result = HuggingFaceAdapter().run_inference({"text": query}, {"subtype": "embedding", "model": model,
                                                                   "input_role": "query"})
    vectors = result.get("vectors") or []
    return [float(value) for value in vectors[0]] if vectors else None


def _locator_disclosable(qualified, rows, key, grant_id) -> bool:
    """Exactly the output checks the locator door makes after a permit (release.py).

    BOTH message views, not just one. The locator door builds the view its signed
    capability names (`release.SOURCE_VIEWS`) and measures THAT against the budget.
    p2a-v1/v2 name `canonical.message_disclosure.v1`, whose `record_id` is the
    canonical id; p2a-v3 -- the only source capability the node still releases under
    -- names v2, whose `record_id` is the 66-character opaque id. The same closure is
    therefore a different number of bytes in each view, and a canonical id may be
    anything up to `Identifier`'s 200 characters, so neither view is always the
    larger. Clearing one budget and not the other is exactly the band in which the
    locator door refuses a fact and search would still release one of its records,
    which breaks discovery-subset-access. Requiring both to fit closes it whichever
    view the sibling locator grant names.

    The opaque ids are derived under THIS grant's key. A locator grant would use its
    own key and so its own ids, but every opaque id is the same length by
    construction, and length is all a byte budget reads.
    """
    leaves = [(ref.identity, rows[_key(ref.identity)].get("content")) for ref in qualified.snapshot.leaves]
    if not leaves:
        return False
    shapes = ((VIEW, MessageDisclosure, [identity.record_id for identity, _ in leaves]),
              (VIEW_OPAQUE, OpaqueMessageDisclosure,
               [opaque_record_id(key, grant_id=grant_id, table=identity.table, source_id=identity.source_id,
                                 dataset_id=identity.dataset_id, record_id=identity.record_id)
                for identity, _ in leaves]))
    for view, model, record_ids in shapes:
        records = [{"record_id": record_id, "source_id": identity.source_id, "canonical_table": identity.table,
                    "content": content} for record_id, (identity, content) in zip(record_ids, leaves)]
        try:
            output = model.parse({"family": "canonical_record", "operation": "read", "view_id": view,
                                  "records": records})
        except PolicyError:
            return False
        if len(canonical_bytes(output.model_dump())) > MAX_DISCLOSURE_BYTES:
            return False
    return True


def _bounds(policy, intent, now: int) -> tuple[int, int]:
    """The query's time range inside the grant's rolling window at `now`, or a refusal (k, window)."""
    window = policy.search.window
    lower_us = (now - window.max_age_seconds) * 1_000_000
    upper_us = now * 1_000_000
    if intent.k > policy.search.max_k:
        raise PolicyError("search_k_above_grant")
    if intent.window is not None:
        if intent.window.after < now - window.max_age_seconds or intent.window.before > now + 1:
            raise PolicyError("search_window_outside_grant")
        lower_us = max(lower_us, intent.window.after * 1_000_000)
        upper_us = min(upper_us, intent.window.before * 1_000_000 - 1)
    return lower_us, upper_us


class MessageSearchRelease:
    def __init__(self, *, protocol, resolver, reviews, index, clock: Callable[[], int],
                 embedder: Callable[[str, str], list | None] | None = default_embedder,
                 observe: Callable[[str, float], None] | None = None):
        if (protocol.ledger.identity.model_dump() != resolver.binding.model_dump()
            or protocol.canonical_database != resolver.path.resolve(strict=True)
            or reviews.binding != resolver.binding or index.resolver is not resolver or index.reviews is not reviews):
            raise PolicyError("release_service_binding")
        self.protocol, self.resolver, self.reviews, self.index = protocol, resolver, reviews, index
        self.clock, self.embedder, self.observe = clock, embedder, observe

    def _stage(self, name: str, started: float, **fields) -> float:
        now = time.perf_counter()
        if self.observe is not None:
            self._report(name, now - started, **fields)
        return now

    def _report(self, name: str, seconds: float, **fields) -> None:
        """One timing reading. A batch's lines carry `n` or `item`; an observer that takes no fields gets none."""
        if not fields:
            self.observe(name, seconds)
            return
        try:
            self.observe(name, seconds, **fields)
        except TypeError:
            self.observe(name, seconds)

    def _tombstone(self, admission) -> None:
        """Spend the request id with nothing but the replay row. A no-op once it is spent."""
        try:
            self.protocol.ledger.refuse(admission, now=self.clock())
        except Exception:  # noqa: BLE001 -- best effort; it must not mask the refusal being raised
            pass

    def _refuse(self, admission, grant_id: str) -> None:
        """Any refusal after verification spends the id and leaves one deny receipt where the ledger still accepts one.
        Always raises `permission_denied`; `_spend` does the writing.

        E2: the row it spends the id with is the tombstone, written in the same
        transaction as the receipt, so a refused search costs the owner ~120 bytes
        rather than the ~2.8 KB envelope. When no receipt can be written -- the
        authority is gone, or the checkpoint itself refuses -- the id is still spent,
        because under the old order admission had already written the row.
        """
        self._spend(admission, grant_id)
        raise PolicyError("permission_denied")

    def _spend(self, admission, grant_id: str) -> None:
        """`_refuse`'s writes without the raise, so a batch can spend every verified id before it refuses once."""
        try:
            with self.protocol.ledger._transaction() as db:
                authority = self.protocol.ledger._authority(db, grant_id, self.clock())[0]
                policy_hash = authority.policy_hash
        except Exception:  # noqa: BLE001 -- authority gone: no receipt is possible, the id is spent anyway
            self._tombstone(admission)
            return
        decision = search_decision_class(authority.capability_version).parse({"stage": "output_release", "verdict": "deny", "policy_hash": policy_hash,
            "candidate_revision": digest([]), "evaluator_version": search_evaluator(authority.capability_version), "matched_allow_clause_ids": [],
            "matched_deny_clause_ids": [], "reason_code": "set_refused", "required_projection_id": None,
            "member_count": 0, "missing_context_codes": []})
        try:
            self.protocol.ledger.refuse(admission, decision.model_dump(), candidate_revision=digest([]), members=[],
                                        now=self.clock())
        except Exception:  # noqa: BLE001 -- the receipt rolled back with its row; spend the id alone
            self._tombstone(admission)

    def _load_index(self, grant_id: str, authority, now: int, verified):
        """check_own then load, timed apart for IF-3 v1.3 (`index_load`'s fields). Timing never changes the answer."""
        laps = {} if self.observe is not None else None
        lap = time.perf_counter()
        self.index.check_own(grant_id, authority, now=now, digest_point="index_load_digest", verified=verified,
                             laps=laps)
        check_own = time.perf_counter() - lap
        lap = time.perf_counter()
        loaded = self.index.load(grant_id, authority)
        if laps is None:
            return loaded, {}
        return loaded, {"check_own_ms": check_own * 1000, "load_ms": (time.perf_counter() - lap) * 1000,
                        **{f"{part}_ms": seconds * 1000 for part, seconds in laps.items()}}

    def verification(self) -> SearchVerification:
        """One search's verified boundary and review digest (search_index.SearchVerification), for all its stages.

        The transport makes one per search and hands it to `dispatch` and to its send-time
        `check_own`, then closes it. It never outlives the search.
        """
        return SearchVerification(self.resolver, self.reviews)

    def dispatch(self, *, envelope: dict, payload: dict, request_id: str,
                 verified: SearchVerification | None = None) -> tuple[dict, dict]:
        if verified is not None:
            return self._dispatch(envelope, payload, request_id, verified)
        with self.verification() as own:
            return self._dispatch(envelope, payload, request_id, own)

    def _dispatch(self, envelope: dict, payload: dict, request_id: str, verified: SearchVerification):
        started = time.perf_counter()
        principal = current_principal()
        if (principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay"
            or not principal.acting_user or not principal.client_id):
            raise PolicyError("recipient_relay_required")
        intent = SearchIntent.parse(payload)
        signed = parse_search_envelope(envelope)
        if signed.request_type != REQUEST_TYPE_SEARCH or signed.capability_version not in SEARCH_CAPABILITIES:
            raise PolicyError("unsupported_query")
        contract = SUBJECT_CONTRACT_BY_CAPABILITY[signed.capability_version]
        ledger = self.protocol.ledger
        request = SearchRequestContext.parse({**ledger.identity.model_dump(), "actor_id": principal.acting_user,
            "client_id": principal.client_id, "grant_id": signed.grant_id, "assignment_id": signed.assignment_id,
            "request_id": request_id, "request_type": REQUEST_TYPE_SEARCH})
        signed_authority = parse_authority({field: getattr(signed, field) for field in AuthorityBinding.model_fields})

        # 2. Admission under the gate, exactly as the locator door admits.
        with with_db_write():
            with ledger._transaction() as db:
                self.protocol._sync_protection(db)
            # E2, as the locator door: verified here, claimed inside `_decide` right
            # before the set checkpoint, so every grant-level refusal below costs the
            # tombstone instead of the envelope. The stage keeps its name.
            admission = ledger.verify(envelope, request=request, payload=signed_payload(intent), now=self.clock())
        started = self._stage("admit", started)
        try:
            current, output, started = self._decide(admission, signed, signed_authority, intent, contract, started,
                                                    verified)
        except Exception:  # noqa: BLE001 -- every failure after verification is one refusal with one receipt
            self._refuse(admission, signed.grant_id)

        # 8. Sign with every gate released; the transport sends after this returns.
        checked_at = self.clock()
        verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=checked_at)
        result = sign_node_result(ReleaseBody(version="topos-node-disclosure/v1", kid=self.protocol.node_signing_kid,
            envelope_hash=digest(signed.model_dump()), request_id=request_id, request_hash=signed.request_hash,
            authority=current, output_hash=digest(output.model_dump()), checked_at=checked_at,
            expires_at=signed.expires_at), self.protocol.node_signing_key)
        self._stage("sign", started)
        return result.model_dump(), output.model_dump()

    # -- batched search (OD-36, design §3.4): one verification pass, N queries -------------------

    def dispatch_batch(self, *, items: list[dict], verified: SearchVerification | None = None,
                       past_deadline: Callable[[], bool] | None = None) -> list[tuple[dict, dict]]:
        """Answer 1..MAX_BATCH_ITEMS searches under ONE grant, snapshot and verification, or refuse them all.

        `items` are `{"envelope", "payload", "request_id"}` in batch order; the transport has bound each
        envelope to its position. Shared, once per batch: protection sync, the authority read, the
        index check and load, the gated recheck (`_current`) and the checkpoint transaction. Per query,
        exactly as a single search: envelope verification (signature, request hash, replay), the k and
        window bounds, embedding, ranking, the candidate walk and its set decision, the receipt (v3)
        and the signed result. `past_deadline` is the CP's advisory `respond_by`; past it the batch
        refuses rather than spend gate time on an answer nobody is waiting for. It grants nothing:
        each envelope's signed `expires_at` stays the authority bound.
        """
        if verified is not None:
            return self._dispatch_batch(items, verified, past_deadline)
        with self.verification() as own:
            return self._dispatch_batch(items, own, past_deadline)

    def _dispatch_batch(self, items, verified: SearchVerification, past_deadline):
        started = time.perf_counter()
        principal = current_principal()
        if (principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay"
            or not principal.acting_user or not principal.client_id):
            raise PolicyError("recipient_relay_required")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH_ITEMS:
            raise PolicyError("batch_binding")
        ledger = self.protocol.ledger
        parsed = []
        for item in items:
            intent = SearchIntent.parse(item["payload"])
            signed = parse_search_envelope(item["envelope"])
            if signed.request_type != REQUEST_TYPE_SEARCH or signed.capability_version not in SEARCH_CAPABILITIES:
                raise PolicyError("unsupported_query")
            request = SearchRequestContext.parse({**ledger.identity.model_dump(), "actor_id": principal.acting_user,
                "client_id": principal.client_id, "grant_id": signed.grant_id, "assignment_id": signed.assignment_id,
                "request_id": item["request_id"], "request_type": REQUEST_TYPE_SEARCH})
            parsed.append((intent, signed, request))
        # One grant, one authority: every envelope must carry the first one's binding, or nothing is shared.
        signed_authority = parse_authority({field: getattr(parsed[0][1], field) for field in AuthorityBinding.model_fields})
        for _intent, signed, _request in parsed:
            if parse_authority({field: getattr(signed, field) for field in AuthorityBinding.model_fields}) != signed_authority:
                raise PolicyError("batch_binding")
        if len({signed.request_id for _i, signed, _r in parsed}) != len(parsed):
            raise PolicyError("batch_binding")
        grant_id = signed_authority.grant_id
        contract = SUBJECT_CONTRACT_BY_CAPABILITY[signed_authority.capability_version]
        count = len(parsed)

        # 2. Admission: one protection sync, then each envelope verified exactly as a single search's.
        admissions = []
        try:
            with with_db_write():
                with ledger._transaction() as db:
                    self.protocol._sync_protection(db)
                for (intent, _signed, request), item in zip(parsed, items):
                    admissions.append(ledger.verify(item["envelope"], request=request, payload=signed_payload(intent),
                                                    now=self.clock()))
        except Exception:  # noqa: BLE001 -- the items verified so far are spent; the failed one wrote nothing
            self._spend_all(admissions, grant_id)
            raise PolicyError("permission_denied") from None
        started = self._stage("admit", started, n=count)
        try:
            currents, outputs, started = self._decide_batch(admissions, parsed, signed_authority, contract, started,
                                                            verified, past_deadline)
        except Exception:  # noqa: BLE001 -- one refusal for the batch; every item past verify is spent with its receipt
            self._spend_all(admissions, grant_id)
            raise PolicyError("permission_denied") from None

        # 8. Sign each item with every gate released; the transport sends after this returns.
        checked_at = self.clock()
        results = []
        for number, ((_intent, signed, _request), current, output) in enumerate(zip(parsed, currents, outputs)):
            verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=checked_at)
            result = sign_node_result(ReleaseBody(version="topos-node-disclosure/v1", kid=self.protocol.node_signing_kid,
                envelope_hash=digest(signed.model_dump()), request_id=signed.request_id, request_hash=signed.request_hash,
                authority=current, output_hash=digest(output.model_dump()), checked_at=checked_at,
                expires_at=signed.expires_at), self.protocol.node_signing_key)
            results.append((result.model_dump(), output.model_dump()))
            started = self._stage("sign", started, item=number)
        return results

    def _spend_all(self, admissions, grant_id: str) -> None:
        for admission in admissions:
            self._spend(admission, grant_id)

    def _decide_batch(self, admissions, parsed, signed_authority, contract, started, verified, past_deadline):
        ledger = self.protocol.ledger
        grant_id = signed_authority.grant_id
        count = len(parsed)

        def expired() -> None:
            if past_deadline is not None and past_deadline():
                raise PolicyError("release_cancelled_or_expired")

        # 3. Once: the grant's authority and policy, then each query's own bounds against them.
        now = self.clock()
        with ledger._transaction() as db:
            authority, policy = ledger._authority(db, grant_id, now)
        if authority != signed_authority or policy.versions.capability not in SEARCH_CAPABILITIES:
            raise PolicyError("authority_stale")
        window = policy.search.window
        bounds = [_bounds(policy, intent, now) for intent, _signed, _request in parsed]
        loaded, split = self._load_index(grant_id, authority, now, verified)
        key = self.index.keys.get(grant_id, create=False)
        if key is None:
            raise PolicyError("search_index_missing")
        started = self._stage("index_load", started, n=count, **split)

        # 4-5. Per query: its vector (a failure is lexical-only for that query alone), then its ranking inside P.
        from .search_lanes import within
        orders = []
        for number, ((intent, _signed, _request), (lower_us, upper_us)) in enumerate(zip(parsed, bounds)):
            query_vector = None
            if within(loaded, lower_us, upper_us).vectors and loaded.model and self.embedder is not None:
                try:
                    query_vector = self.embedder(intent.query, loaded.model)
                except Exception:  # noqa: BLE001 -- lexical-only for this query
                    query_vector = None
            started = self._stage("embed", started, item=number)
            orders.append(rank(loaded, intent.query, query_vector, limit=max(4 * intent.k, CANDIDATE_FLOOR),
                               lower_us=lower_us, upper_us=upper_us, precision=policy.search.release_event_time))
            started = self._stage("rank", started, item=number)
        by_id = {member.opaque_id: member for member in loaded.members}
        expired()

        # 6-7. One gated read for the batch: authority, floor and `_current` once; then each query's walk.
        tables = set(policy.search.tables)
        walks, accepts = [], []
        with with_db_write():
            before = verified.canonical_token()
            if (self.reviews.binding != self.resolver.binding
                    or self.reviews.canonical_file_revision != self.resolver._file_revision()):
                raise PolicyError("review_database_binding")
            with self.resolver._read() as (conn, floor):
                self.reviews._observe_clock(conn)
                with ledger._transaction() as db:
                    self.protocol._sync_protection(db)
                    current, policy = ledger._authority(db, grant_id, self.clock())
                if current != signed_authority or floor is None or floor != current.protection_revision:
                    raise PolicyError("authority_stale")
                if not self.index._current(index_path(self.index.root, grant_id), grant_id, current, clock_state(conn),
                                           conn, deep=False, verified=verified, before=before):
                    raise PolicyError("search_index_stale")
                read_now = self.clock()
                decided: dict[str, tuple | None] = {}
                with self.reviews._db() as review_db:
                    for number, ((intent, _signed, _request), (lower_us, upper_us), order) in enumerate(
                            zip(parsed, bounds, orders)):
                        if number:
                            expired()
                        walked = time.perf_counter()
                        lower_us = max(lower_us, (read_now - window.max_age_seconds) * 1_000_000)
                        upper_us = min(upper_us, read_now * 1_000_000)
                        walks.append(self._walk(conn, floor, review_db, key, grant_id, order, by_id, policy, contract,
                                                tables, decided, lower_us, upper_us, intent.k, current))
                        accepts.append(time.perf_counter() - walked)
                started = self._stage("recheck", started, n=count)
                if self.observe is not None:
                    self._report("recheck_facts", float(len(decided)), n=count)
                # One ledger transaction claims every id and writes every item's own receipt v3, or none.
                ledger.admit_and_checkpoint_search_batch(
                    [(admission, decision.model_dump(), candidate_revision, output.model_dump(), bindings)
                     for admission, (output, decision, candidate_revision, bindings) in zip(admissions, walks)],
                    now=self.clock())
        started = self._stage("checkpoint", started, n=count)
        if self.observe is not None:  # the walks' lines, written once the gate is released
            for number, seconds in enumerate(accepts):
                self._report("accept", seconds, item=number)
        return [current] * count, [output for output, _d, _c, _b in walks], started

    def _decide(self, admission, signed, signed_authority, intent, contract, started, verified):
        ledger = self.protocol.ledger

        # 3. Grant-level bounds and the grant's own index. Nothing here reads a canonical row.
        now = self.clock()
        with ledger._transaction() as db:
            authority, policy = ledger._authority(db, signed.grant_id, now)
        if authority != signed_authority or policy.versions.capability not in SEARCH_CAPABILITIES:
            raise PolicyError("authority_stale")
        window = policy.search.window
        lower_us, upper_us = _bounds(policy, intent, now)
        # Only this grant's own file is checked here (O(|R(g)|)); the whole-root sweep runs owner-side
        # and on the daemon, so other grants' sizes never enter this request's time.
        loaded, split = self._load_index(signed.grant_id, authority, now, verified)
        key = self.index.keys.get(signed.grant_id, create=False)
        if key is None:
            raise PolicyError("search_index_missing")
        started = self._stage("index_load", started, **split)

        # 4. The query vector, only against the model the index was built with.
        query_vector = None
        from .search_lanes import within
        if within(loaded, lower_us, upper_us).vectors and loaded.model and self.embedder is not None:
            try:
                query_vector = self.embedder(intent.query, loaded.model)
            except Exception:  # noqa: BLE001 -- lexical-only for this request
                query_vector = None
        started = self._stage("embed", started)

        # 5. Rank inside P.
        order = rank(loaded, intent.query, query_vector, limit=max(4 * intent.k, CANDIDATE_FLOOR),
                     lower_us=lower_us, upper_us=upper_us, precision=policy.search.release_event_time)
        by_id = {member.opaque_id: member for member in loaded.members}
        started = self._stage("rank", started)

        # 6-7. One read, under the gate: re-decide every candidate with the locator door's function.
        tables = set(policy.search.tables)
        with with_db_write():
            before = verified.canonical_token()  # before the read's snapshot is established
            if (self.reviews.binding != self.resolver.binding
                    or self.reviews.canonical_file_revision != self.resolver._file_revision()):
                raise PolicyError("review_database_binding")
            with self.resolver._read() as (conn, floor):
                self.reviews._observe_clock(conn)
                with ledger._transaction() as db:
                    self.protocol._sync_protection(db)
                    current, policy = ledger._authority(db, signed.grant_id, self.clock())
                if current != signed_authority or floor is None or floor != current.protection_revision:
                    raise PolicyError("authority_stale")
                # Alias/contact/context changes can occur without advancing the
                # grant clock. Ranking must still describe the current permitted
                # set after embedding/ranking, not merely at index load.
                # The same boundary serves this check and every re-decision below (one closure per read),
                # and is the one index load verified when no commit has landed since.
                if not self.index._current(index_path(self.index.root, signed.grant_id), signed.grant_id,
                        current, clock_state(conn), conn, deep=False, verified=verified, before=before):
                    raise PolicyError("search_index_stale")
                # The grant's rolling window, from the clock of this very read (never the earlier one).
                read_now = self.clock()
                lower_us = max(lower_us, (read_now - window.max_age_seconds) * 1_000_000)
                upper_us = min(upper_us, read_now * 1_000_000)
                decided: dict[str, tuple | None] = {}
                with self.reviews._db() as review_db:
                    output, decision, candidate_revision, bindings = self._walk(
                        conn, floor, review_db, key, signed.grant_id, order, by_id, policy, contract, tables, decided,
                        lower_us, upper_us, intent.k, current)
                started = self._stage("recheck", started)
                if self.observe is not None:
                    self.observe("recheck_facts", float(len(decided)))
                lease = ledger.admit_verified(admission, now=self.clock())
                ledger.checkpoint_set_decision(lease, decision.model_dump(), candidate_revision=candidate_revision,
                                               output=output.model_dump(), members=bindings, now=self.clock())
        started = self._stage("checkpoint", started)
        return current, output, started

    def _walk(self, conn, floor, review_db, key, grant_id, order, by_id, policy, contract, tables, decided,
              lower_us, upper_us, k, current):
        """One query's candidate walk and set decision on the gated read: `_accept` until k, under the byte cap.

        `decided` caches a witness's decision by fact id (or the direct message's opaque id). That
        decision depends on the record, the grant and the read's snapshot, never on the query, so the
        queries of one batch may share it; the window, the table and the precision are applied after
        the lookup, per query.
        """
        view_id = policy.search.view_id
        records, bindings, revisions = [], [], []
        for opaque in order:
            if len(records) == k:
                break
            accepted = self._accept(conn, floor, review_db, key, grant_id, opaque, by_id[opaque],
                                    policy, contract, tables, decided, lower_us, upper_us,
                                    policy.search.release_event_time)
            if accepted is None:
                continue
            record, binding, revision = accepted
            trial = records + [record]
            if len(canonical_bytes({"family": "canonical_record", "operation": "search",
                                    "view_id": view_id, "records": trial})) > MAX_SEARCH_BYTES:
                break
            records, bindings, revisions = trial, bindings + [binding], revisions + [revision]
        output_model = KnowledgeSearchResult if policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH else MessageSearchResult
        output = output_model.parse({"family": "canonical_record", "operation": "search",
                                            "view_id": view_id, "records": records})
        candidate_revision = digest(sorted(revisions, key=lambda item: item["record_key_digest"]))
        decision = search_decision_class(policy.versions.capability).parse({"stage": "output_release", "verdict": "permit",
            "policy_hash": current.policy_hash, "candidate_revision": candidate_revision,
            "evaluator_version": search_evaluator(policy.versions.capability),
            "matched_allow_clause_ids": sorted({binding["allow_clause_id"] for binding in bindings}),
            "matched_deny_clause_ids": [], "reason_code": "rule_permit",
            "required_projection_id": view_id, "member_count": len(records), "missing_context_codes": []})
        return output, decision, candidate_revision, bindings

    @staticmethod
    def _journal_member(row, identity, opaque, qualified, decision, policy, automatic, lower_us, upper_us, precision):
        """A journal entry releases only as its own kind (IF-5 §2-§3): under a knowledge grant that signs
        `journal_entry`, inside the window by every instant its stated day can denote, not NSFW-flagged, and
        dated at most by that day. No other view or kind ever carries one."""
        from .evidence_families import released, within
        content = row.get("content")
        if (not automatic or "journal_entry" not in policy.search.result_types
                or not within(identity.table, row, lower_us, upper_us) or is_record_nsfw(row)
                or not isinstance(content, str) or len(content) > 8000):
            return None
        record = dict(kind="journal_entry", record_id=opaque, content=content, source_ids=[identity.source_id],
                      citations=[dict(record_id=opaque, source_id=identity.source_id, content=content)],
                      event_at=released(identity.table, row, precision))
        revision = digest({"snapshot": qualified.snapshot.model_dump(), "review": qualified.review_revision})
        binding = KnowledgeMemberBinding(kind="journal_entry", record_id=opaque, source_ids=[identity.source_id],
            evidence_tables=[identity.table], evidence_revision=revision, projection_revision=digest(record),
            allow_clause_id=decision.matched_allow_clause_ids[0], member_decision_hash=digest(decision.model_dump()))
        return record, binding.model_dump(), dict(record_key_digest=digest(_key(identity)),
                                                  evidence_revision=revision, projection_revision=digest(record))

    def _accept(self, conn, floor, review_db, key, grant_id, opaque, member, policy, contract, tables, decided,
                lower_us, upper_us, precision="none"):
        """One candidate: released only if one of its witness facts is `permit` right now."""
        try:
            sealed = unseal(key, opaque, member.sealed)
        except PolicyError:
            return None
        automatic = policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH
        direct = policy.versions.capability in DIRECT_SEARCH_CAPABILITIES
        if automatic and sealed.get('projection'):
            from .knowledge_projections import qualify_projection
            descriptor=sealed['projection']
            try:
                projected=qualify_projection(self.resolver,conn,floor,self.reviews,review_db,
                    descriptor['table'],descriptor['record_id'],policy,lower_us,upper_us)
                if projected.kind not in policy.search.result_types or projected.revision!=descriptor['revision']:
                    return None
                record=projected.output(key=key,grant_id=grant_id,precision=precision)
                if record['record_id']!=opaque:
                    return None
                # Enforce every family schema and citation bound before signing.
                record=KnowledgeSearchResult.parse(dict(family='canonical_record',operation='search',
                    view_id=policy.search.view_id,records=[record])).records[0].model_dump()
                evidence_revision=digest([dict(snapshot=q.snapshot.model_dump(),review=q.review_revision)
                                          for q,_rows in projected.sources])
                projection_revision=digest(record)
                binding=KnowledgeMemberBinding(kind=projected.kind,record_id=opaque,source_ids=record['source_ids'],
                    evidence_tables=sorted({q.snapshot.message.identity.table for q,_ in projected.sources}),
                    evidence_revision=evidence_revision,projection_revision=projection_revision,
                    allow_clause_id=projected.allow_clause_id,member_decision_hash=digest(dict(
                        policy_hash=digest(policy.model_dump()),evidence=evidence_revision,
                        projection=projection_revision,allow_clause_id=projected.allow_clause_id)))
                return record,binding.model_dump(),dict(record_key_digest=digest(opaque),
                    evidence_revision=evidence_revision,projection_revision=projection_revision)
            except PolicyError:
                return None
        witnesses = (["message"] if sealed.get("message") else []) if direct else sealed.get("facts", ())
        for fact_id in witnesses:
            cache_key = ("message", opaque) if direct else fact_id
            if cache_key not in decided:
                try:
                    if direct:
                        from .message_evidence import qualify_message, qualify_automatic_message
                        from .evidence import EvidenceIdentity
                        identity = EvidenceIdentity.parse(sealed["message"])
                        qualify = qualify_automatic_message if automatic else qualify_message
                        qualified, rows = qualify(self.resolver, conn, floor, identity, self.reviews, review_db)
                    else:
                        qualified, rows = self.resolver._qualified_bundle(conn, floor, fact_id, self.reviews, review_db,
                                                                          contract=contract, discloses_sources=True)
                    decision = source_message_decision(policy, qualified)
                    # The locator door refuses a permitted fact whose whole disclosure cannot be built
                    # (over 100 leaves, over its byte budget, a non-text leaf); search refuses it too.
                    decided[cache_key] = ((qualified, rows, decision)
                                        if decision.verdict == "permit"
                                        and (direct or _locator_disclosable(qualified, rows, key, grant_id)) else None)
                except PolicyError:
                    decided[cache_key] = None
            entry = decided[cache_key]
            if entry is None:
                continue
            qualified, rows, decision = entry
            leaf = next((leaf for leaf in qualified.snapshot.leaves
                         if (leaf.identity.table, leaf.identity.source_id, leaf.identity.dataset_id, leaf.identity.record_id)
                         == (sealed["table"], sealed["source_id"], sealed["dataset_id"], sealed["record_id"])), None)
            if leaf is None or leaf.identity.table not in tables:
                continue
            identity = leaf.identity
            if opaque_record_id(key, grant_id=grant_id, table=identity.table, source_id=identity.source_id,
                                dataset_id=identity.dataset_id, record_id=identity.record_id) != opaque:
                continue
            row = rows[_key(identity)]
            if identity.table == "journal_entries":
                return self._journal_member(row, identity, opaque, qualified, decision, policy, automatic,
                                            lower_us, upper_us, precision)
            event_us = canonical_utc_microseconds(row.get("event_at"))
            from .reconciliation_provenance import native_time_within
            content = row.get("content")
            if (event_us is None or not lower_us <= event_us <= upper_us or not native_time_within(row, lower_us, upper_us) or is_record_nsfw(row)
                    or not isinstance(content, str) or len(content) > MAX_RECORD_CHARS):
                return None
            record = {"record_id": opaque, "source_id": identity.source_id, "canonical_table": identity.table,
                      "content": content}
            # The window filtered on full precision above; the view carries only what the grant releases.
            if precision == "second":
                record["event_at"] = event_us // 1_000_000
            elif precision == "day":
                record["event_at"] = event_us // 86_400_000_000 * 86_400
            if automatic:
                if 'message' not in policy.search.result_types or len(content) > 8000:
                    continue
                record = dict(kind='message',record_id=opaque,content=content,source_ids=[identity.source_id],
                    citations=[dict(record_id=opaque,source_id=identity.source_id,content=content)],
                    event_at=record.get('event_at'))
                revision = digest({"snapshot":qualified.snapshot.model_dump(),"review":qualified.review_revision})
                binding = KnowledgeMemberBinding(kind='message',record_id=opaque,source_ids=[identity.source_id],
                    evidence_tables=[identity.table],evidence_revision=revision,projection_revision=digest(record),
                    allow_clause_id=decision.matched_allow_clause_ids[0],member_decision_hash=digest(decision.model_dump()))
                return record,binding.model_dump(),dict(record_key_digest=digest(_key(identity)),
                    evidence_revision=revision,projection_revision=digest(record))
            member_class = DirectSearchMemberBinding if direct else SearchMemberBinding
            witness = {"message_review_revision": qualified.review_revision} if direct else {"fact_id": fact_id}
            binding = member_class.parse({"table": identity.table, "source_id": identity.source_id,
                "record_id": identity.record_id, **witness, "allow_clause_id": decision.matched_allow_clause_ids[0],
                "member_decision_hash": digest(decision.model_dump())}).model_dump()
            revision = {"record_key_digest": digest(_key(identity)), **witness,
                        "snapshot_digest": digest(qualified.snapshot.model_dump()),
                        "review_revision": qualified.review_revision, "member_decision_hash": binding["member_decision_hash"]}
            return record, binding, revision
        return None
