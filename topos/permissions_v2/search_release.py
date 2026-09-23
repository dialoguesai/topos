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
from .contract import VIEW, VIEW_OPAQUE, MessageDisclosure
from .registry import OpaqueMessageDisclosure
from .release import MAX_DISCLOSURE_BYTES, source_message_decision
from .search_contract import (CAPABILITY_SEARCH, MAX_RECORD_CHARS, MAX_SEARCH_BYTES, REQUEST_TYPE_SEARCH, VIEW_SEARCH,
    MessageSearchResult, SearchIntent, SearchMemberBinding, SearchSetDecision, signed_payload)
from .search_index import unseal
from .search_lanes import rank
from .signing import (AuthorityBinding, SearchRequestContext, SignedSearchEnvelope, parse_authority, parse_envelope,
    verify_current_signature)

CANDIDATE_FLOOR = 50


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

    def _stage(self, name: str, started: float) -> float:
        now = time.perf_counter()
        if self.observe is not None:
            self.observe(name, now - started)
        return now

    def _refuse(self, lease, grant_id: str) -> None:
        """Any refusal after admission leaves one deny receipt where the ledger still accepts one, as p2a's deny does."""
        try:
            with self.protocol.ledger._transaction() as db:
                policy_hash = self.protocol.ledger._authority(db, grant_id, self.clock())[0].policy_hash
        except Exception:  # noqa: BLE001 -- authority gone: the admitted lease simply expires
            raise PolicyError("permission_denied") from None
        decision = SearchSetDecision.parse({"stage": "output_release", "verdict": "deny", "policy_hash": policy_hash,
            "candidate_revision": digest([]), "evaluator_version": "hard-rules/p2c-v1", "matched_allow_clause_ids": [],
            "matched_deny_clause_ids": [], "reason_code": "set_refused", "required_projection_id": None,
            "member_count": 0, "missing_context_codes": []})
        try:
            self.protocol.ledger.checkpoint_set_decision(lease, decision.model_dump(), candidate_revision=digest([]),
                                                         output=None, members=[], now=self.clock())
        except Exception:  # noqa: BLE001
            pass
        raise PolicyError("permission_denied")

    def dispatch(self, *, envelope: dict, payload: dict, request_id: str) -> tuple[dict, dict]:
        started = time.perf_counter()
        principal = current_principal()
        if (principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay"
            or not principal.acting_user or not principal.client_id):
            raise PolicyError("recipient_relay_required")
        intent = SearchIntent.parse(payload)
        signed = parse_search_envelope(envelope)
        if signed.request_type != REQUEST_TYPE_SEARCH or signed.capability_version != CAPABILITY_SEARCH:
            raise PolicyError("unsupported_query")
        contract = SUBJECT_CONTRACT_BY_CAPABILITY[CAPABILITY_SEARCH]
        ledger = self.protocol.ledger
        request = SearchRequestContext.parse({**ledger.identity.model_dump(), "actor_id": principal.acting_user,
            "client_id": principal.client_id, "grant_id": signed.grant_id, "assignment_id": signed.assignment_id,
            "request_id": request_id, "request_type": REQUEST_TYPE_SEARCH})
        signed_authority = parse_authority({field: getattr(signed, field) for field in AuthorityBinding.model_fields})

        # 2. Admission under the gate, exactly as the locator door admits.
        with with_db_write():
            with ledger._transaction() as db:
                self.protocol._sync_protection(db)
            lease = ledger.admit(envelope, request=request, payload=signed_payload(intent), now=self.clock())
        started = self._stage("admit", started)
        try:
            current, output, started = self._decide(lease, signed, signed_authority, intent, contract, started)
        except Exception:  # noqa: BLE001 -- every failure after admission is one refusal with one receipt
            self._refuse(lease, signed.grant_id)

        # 8. Sign with every gate released; the transport sends after this returns.
        checked_at = self.clock()
        verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=checked_at)
        result = sign_node_result(ReleaseBody(version="topos-node-disclosure/v1", kid=self.protocol.node_signing_kid,
            envelope_hash=digest(signed.model_dump()), request_id=request_id, request_hash=signed.request_hash,
            authority=current, output_hash=digest(output.model_dump()), checked_at=checked_at,
            expires_at=signed.expires_at), self.protocol.node_signing_key)
        self._stage("sign", started)
        return result.model_dump(), output.model_dump()

    def _decide(self, lease, signed, signed_authority, intent, contract, started):
        ledger = self.protocol.ledger

        # 3. Grant-level bounds and the grant's own index. Nothing here reads a canonical row.
        now = self.clock()
        with ledger._transaction() as db:
            authority, policy = ledger._authority(db, signed.grant_id, now)
        if authority != signed_authority or policy.versions.capability != CAPABILITY_SEARCH:
            raise PolicyError("authority_stale")
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
        # Only this grant's own file is checked here (O(|R(g)|)); the whole-root sweep runs owner-side
        # and on the daemon, so other grants' sizes never enter this request's time.
        self.index.check_own(signed.grant_id, authority, now=now)
        loaded = self.index.load(signed.grant_id, authority)
        key = self.index.keys.get(signed.grant_id, create=False)
        if key is None:
            raise PolicyError("search_index_missing")
        started = self._stage("index_load", started)

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
                # The grant's rolling window, from the clock of this very read (never the earlier one).
                read_now = self.clock()
                lower_us = max(lower_us, (read_now - window.max_age_seconds) * 1_000_000)
                upper_us = min(upper_us, read_now * 1_000_000)
                decided: dict[str, tuple | None] = {}
                records, bindings, revisions = [], [], []
                with self.reviews._db() as review_db:
                    for opaque in order:
                        if len(records) == intent.k:
                            break
                        accepted = self._accept(conn, floor, review_db, key, signed.grant_id, opaque, by_id[opaque],
                                                policy, contract, tables, decided, lower_us, upper_us,
                                                policy.search.release_event_time)
                        if accepted is None:
                            continue
                        record, binding, revision = accepted
                        trial = records + [record]
                        if len(canonical_bytes({"family": "canonical_record", "operation": "search",
                                                "view_id": VIEW_SEARCH, "records": trial})) > MAX_SEARCH_BYTES:
                            break
                        records, bindings, revisions = trial, bindings + [binding], revisions + [revision]
                output = MessageSearchResult.parse({"family": "canonical_record", "operation": "search",
                                                    "view_id": VIEW_SEARCH, "records": records})
                candidate_revision = digest(sorted(revisions, key=lambda item: item["record_key_digest"]))
                decision = SearchSetDecision.parse({"stage": "output_release", "verdict": "permit",
                    "policy_hash": current.policy_hash, "candidate_revision": candidate_revision,
                    "evaluator_version": "hard-rules/p2c-v1",
                    "matched_allow_clause_ids": sorted({binding["allow_clause_id"] for binding in bindings}),
                    "matched_deny_clause_ids": [], "reason_code": "rule_permit",
                    "required_projection_id": VIEW_SEARCH, "member_count": len(records), "missing_context_codes": []})
                started = self._stage("recheck", started)
                if self.observe is not None:
                    self.observe("recheck_facts", float(len(decided)))
                ledger.checkpoint_set_decision(lease, decision.model_dump(), candidate_revision=candidate_revision,
                                               output=output.model_dump(), members=bindings, now=self.clock())
        started = self._stage("checkpoint", started)
        return current, output, started

    def _accept(self, conn, floor, review_db, key, grant_id, opaque, member, policy, contract, tables, decided,
                lower_us, upper_us, precision="none"):
        """One candidate: released only if one of its witness facts is `permit` right now."""
        try:
            sealed = unseal(key, opaque, member.sealed)
        except PolicyError:
            return None
        for fact_id in sealed.get("facts", ()):
            if fact_id not in decided:
                try:
                    qualified, rows = self.resolver._qualified_bundle(conn, floor, fact_id, self.reviews, review_db,
                                                                      contract=contract, discloses_sources=True)
                    decision = source_message_decision(policy, qualified)
                    # The locator door refuses a permitted fact whose whole disclosure cannot be built
                    # (over 100 leaves, over its byte budget, a non-text leaf); search refuses it too.
                    decided[fact_id] = ((qualified, rows, decision)
                                        if decision.verdict == "permit"
                                        and _locator_disclosable(qualified, rows, key, grant_id) else None)
                except PolicyError:
                    decided[fact_id] = None
            entry = decided[fact_id]
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
            event_us = canonical_utc_microseconds(row.get("event_at"))
            content = row.get("content")
            if (event_us is None or not lower_us <= event_us <= upper_us or is_record_nsfw(row)
                    or not isinstance(content, str) or len(content) > MAX_RECORD_CHARS):
                return None
            record = {"record_id": opaque, "source_id": identity.source_id, "canonical_table": identity.table,
                      "content": content}
            # The window filtered on full precision above; the view carries only what the grant releases.
            if precision == "second":
                record["event_at"] = event_us // 1_000_000
            elif precision == "day":
                record["event_at"] = event_us // 86_400_000_000 * 86_400
            binding = SearchMemberBinding.parse({"table": identity.table, "source_id": identity.source_id,
                "record_id": identity.record_id, "fact_id": fact_id, "allow_clause_id": decision.matched_allow_clause_ids[0],
                "member_decision_hash": digest(decision.model_dump())}).model_dump()
            revision = {"record_key_digest": digest(_key(identity)), "fact_id": fact_id,
                        "snapshot_digest": digest(qualified.snapshot.model_dump()),
                        "review_revision": qualified.review_revision, "member_decision_hash": binding["member_decision_hash"]}
            return record, binding, revision
        return None
