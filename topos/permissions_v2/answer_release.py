"""One local generation at a time, after a signed ask for one permitted share.

Questions and bodies live only in this process. The ledger keeps hashes and
counts; a restart loses pending work and a second fetch cannot replay a body.
"""
from __future__ import annotations

import asyncio
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from topos.principal import THIRD_PARTY, current_principal
from topos.storage.db.write_gate import with_db_write

from . import answer_gate, switches
from .answer_generation import (CheckedAnswer, build_prompt, owner_party_words, people_words, post_check_answer,
    question_lacks_permitted_anchor)
from .answer_protocol import (ASK, FETCH, K_ANSWER, VERSION, AnswerPending, AskIntent, FetchIntent,
    NoAnswer, effective_mode, parse_answer_output, same_answer_authority)
from .canonical import PolicyError, digest
from .forwarding import ReleaseBody, sign_node_result
from .signing import AnswerRequestContext, AuthorityBinding, SignedKnowledgeAnswerEnvelope, parse_authority, parse_envelope

MAX_JOBS = 3
MAX_WAIT_SECONDS = 60
MAX_END_SECONDS = 110
UNFETCHED_SECONDS = 600

# The closed vocabulary of the private receipt's `reason` (A2A-4 §7.1, `_receipt`): counts-only words, never
# content. The first group is the contract's list with amendment 2's `question_not_supported`. The second (BL-156,
# 1.5.2) is what the retrieval inside a job (`retrieve_for_answer`, its index load and its boundary) refuses with:
# carried as the refusal's own code, so a dark share reads as the index it lacks and not as a move of authority.
# `authority_moved` is kept for the one case it names: the permitted set changed between retrieval and the body.
# A refusal with any other code is recorded as `refused`: the receipt never carries a word outside this set.
RECEIPT_REASONS = frozenset({
    "answered", "nothing_matched", "question_protected", "question_not_supported", "all_sentences_dropped",
    "answer_protected", "authority_moved", "body_invalid", "model_unavailable", "model_error", "queue_deadline",
    "deadline",
    "search_index_missing", "search_index_stale", "search_index_over_cap", "search_index_integrity",
    "search_index_unavailable", "search_index_binding", "search_verification_closed", "review_database_binding",
    "grant_inactive", "policy_time", "authority_stale", "entity_protection_lineage_unavailable", "refused",
})
REASON_OTHER = "refused"


def receipt_reason(code: str) -> str:
    """The receipt word for a refusal's code: the code itself when it is in the closed vocabulary, else `refused`."""
    return code if code in RECEIPT_REASONS else REASON_OTHER


def answer_jobs_active() -> bool:
    """A job is queued or running: the background assessments wait (A2A-4 Q4, `answer_gate`)."""
    return answer_gate.active()


_active_delta = answer_gate.delta


@dataclass
class Job:
    answer_id: str
    request_id: str
    grant_id: str
    assignment_id: str
    actor_id: str
    client_id: str
    admitted_authority: object
    mode: str
    question: str | None
    question_hash: str
    accepted_at: int
    state: str = "queued"
    body: object | None = None
    ended_at: int | None = None
    records_used: int = 0
    records_digest: str | None = None
    output_digest: str | None = None
    set_decision: dict | None = None


class AnswerBusy(Exception):
    """The only answer failure the control plane reports as busy, after issuance."""


class AnswerService:
    def __init__(self, runtime, *, clock=None, generate=None):
        self.runtime = runtime
        self.clock = clock or (lambda: int(time.time()))
        self.generate = generate or _generate_local
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="permissions-answer")

    def close(self):
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _enabled(self):
        return switches.on(switches.ANSWERS) and switches.on(switches.MESSAGE_SEARCH)

    def _verified(self, envelope, payload, request_id, request_type):
        principal = current_principal()
        if (principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay"
                or not principal.acting_user or not principal.client_id or not self._enabled()):
            raise PolicyError("permission_denied")
        signed = parse_envelope(envelope)
        if (not isinstance(signed, SignedKnowledgeAnswerEnvelope) or signed.request_type != request_type
                or signed.request_id != request_id):
            raise PolicyError("permission_denied")
        ledger = self.runtime.protocol.ledger
        context = AnswerRequestContext.parse({**ledger.identity.model_dump(), "actor_id": principal.acting_user,
            "client_id": principal.client_id, "grant_id": signed.grant_id,
            "assignment_id": signed.assignment_id, "request_id": request_id, "request_type": request_type})
        with with_db_write():
            with ledger._transaction() as db:
                self.runtime.protocol._sync_protection(db)
            admission = ledger.verify(envelope, request=context, payload=payload, now=self.clock())
        return principal, signed, admission

    def _current(self, signed):
        ledger = self.runtime.protocol.ledger
        with with_db_write():
            with ledger._transaction() as db:
                self.runtime.protocol._sync_protection(db)
                authority, policy = ledger._authority(db, signed.grant_id, self.clock())
        requested = parse_authority({field: getattr(signed, field) for field in AuthorityBinding.model_fields})
        if authority != requested:
            raise PolicyError("permission_denied")
        return authority, policy

    def _signed(self, signed, output):
        ledger = self.runtime.protocol.ledger
        output = parse_answer_output(output, request_type=signed.request_type,
                                     mode="only" if isinstance(output, AnswerPending) else self._mode(signed))
        now = self.clock()
        if signed.expires_at <= now:
            raise PolicyError("permission_denied")
        authority, _ = self._current(signed)
        result = sign_node_result(ReleaseBody(version="topos-node-disclosure/v1",
            kid=self.runtime.protocol.node_signing_kid, envelope_hash=digest(signed.model_dump()),
            request_id=signed.request_id, request_hash=signed.request_hash, authority=authority,
            output_hash=digest(output.model_dump()), checked_at=now, expires_at=signed.expires_at),
            self.runtime.protocol.node_signing_key)
        return result.model_dump(), output.model_dump()

    def _mode(self, signed):
        _authority, policy = self._current(signed)
        return effective_mode(policy, frontend_client_id=self.runtime.protocol.frontend_client_id)

    def _clean(self, now):
        for key, job in list(self._jobs.items()):
            if job.state == "ended" and job.ended_at is not None and now >= job.ended_at + UNFETCHED_SECONDS:
                job.question = None
                job.body = None
                del self._jobs[key]

    def submit(self, *, envelope, payload, request_id):
        intent = AskIntent.parse(payload)
        _principal, signed, admission = self._verified(envelope, intent.model_dump(), request_id, ASK)
        ledger = self.runtime.protocol.ledger
        try:
            _authority, policy = self._current(signed)
            mode = effective_mode(policy, frontend_client_id=self.runtime.protocol.frontend_client_id)
            if mode not in ("only", "with_sources") or policy.versions.capability != "permissions-beta/p2c-v3":
                raise PolicyError("answers_mode_mismatch")
            now = self.clock()
            with self._lock:
                self._clean(now)
                active = [job for job in self._jobs.values() if job.state in ("queued", "running")]
                if len(active) >= MAX_JOBS or any(job.grant_id == signed.grant_id for job in active):
                    ledger.refuse(admission, now=now)
                    raise AnswerBusy()
                ledger.admit_answer(admission, now=now, charge=True)
                answer_id = "ans_" + secrets.token_hex(16)
                job = Job(answer_id, request_id, signed.grant_id, signed.assignment_id, signed.actor_id,
                          signed.client_id, parse_authority({field: getattr(signed, field) for field in AuthorityBinding.model_fields}), mode, intent.question,
                          signed.request_hash, now)
                self._jobs[answer_id] = job
                _active_delta(1)
                try:
                    future = self._executor.submit(self._run, job)
                    future.add_done_callback(lambda _done: _active_delta(-1))
                except Exception:
                    _active_delta(-1)
                    self._jobs.pop(answer_id, None)
                    raise
            return self._signed(signed, AnswerPending(version=VERSION, state="pending", answer_id=answer_id))
        except AnswerBusy:
            raise
        except Exception:
            ledger.refuse(admission, now=self.clock())
            raise PolicyError("permission_denied") from None

    def fetch(self, *, envelope, payload, request_id):
        intent = FetchIntent.parse(payload)
        _principal, signed, admission = self._verified(envelope, intent.model_dump(), request_id, FETCH)
        ledger = self.runtime.protocol.ledger
        try:
            with self._lock:
                self._clean(self.clock())
                job = self._jobs.get(intent.answer_id)
                if (job is None or job.grant_id != signed.grant_id or job.assignment_id != signed.assignment_id
                        or job.actor_id != signed.actor_id or job.client_id != signed.client_id):
                    raise PolicyError("answer_unknown")
                current, policy = self._current(signed)
                if (not same_answer_authority(current, job.admitted_authority)
                        or effective_mode(policy, frontend_client_id=self.runtime.protocol.frontend_client_id) != job.mode):
                    job.question = None
                    job.body = None
                    self._jobs.pop(intent.answer_id, None)
                    raise PolicyError("authority_stale")
                body = job.body if job.state == "ended" else AnswerPending(version=VERSION, state="pending",
                                                                             answer_id=job.answer_id)
                if body is None:
                    raise PolicyError("answer_unknown")
                if job.state == "ended" and job.question is not None and getattr(body, "outcome", None) == "answered":
                    _authority, _policy, still, _decision = self.runtime.message_search().retrieve_for_answer(
                        grant_id=job.grant_id, question=job.question,
                        admitted_authority=job.admitted_authority)
                    if (len(still.records) != job.records_used or
                            digest(sorted(record.record_id for record in still.records)) != job.records_digest or
                            digest(still.model_dump()) != job.output_digest):
                        job.question = None
                        job.body = None
                        self._jobs.pop(intent.answer_id, None)
                        raise PolicyError("authority_moved")
                ledger.admit_answer(admission, now=self.clock(), charge=False)
                result = self._signed(signed, body)
                if job.state == "ended":
                    job.question = None
                    job.body = None
                    self._jobs.pop(intent.answer_id, None)
                return result
        except Exception:
            ledger.refuse(admission, now=self.clock())
            raise PolicyError("permission_denied") from None

    def _run(self, job: Job):
        try:
            started = self.clock()
            if started >= job.accepted_at + MAX_WAIT_SECONDS:
                checked, reason = None, "queue_deadline"
            else:
                with self._lock:
                    if job.answer_id not in self._jobs:
                        return
                    job.state = "running"
                checked, reason = self._compute(job)
            ended = self.clock()
            if ended >= job.accepted_at + MAX_END_SECONDS:
                checked, reason = None, "deadline"
            body = checked.body if checked is not None else NoAnswer(version=VERSION, outcome="no_answer")
            try:
                parse_answer_output(body, request_type=FETCH, mode=job.mode)
            except PolicyError:
                checked, reason = None, "body_invalid"
                body = NoAnswer(version=VERSION, outcome="no_answer")
            receipt = self._receipt(job, checked, reason, started, ended)
            # A body is never available to fetch unless its counts-only receipt committed.
            self.runtime.protocol.ledger.checkpoint_answer_receipt(job.request_id, receipt,
                decision=job.set_decision, now=ended)
            with self._lock:
                if job.answer_id in self._jobs:
                    job.body = body
                    job.state = "ended"
                    job.ended_at = ended
        except Exception:
            with self._lock:
                self._jobs.pop(job.answer_id, None)

    def _compute(self, job: Job):
        try:
            adapter = self.runtime.message_search()
            question = job.question
            with adapter.resolver._read() as (conn, _floor):
                boundary = adapter.resolver.entity_boundary(conn)
                if boundary.mentions_protected(question):
                    return None, "question_protected"
                owner_words = owner_party_words(conn, boundary)
                people = people_words(conn)
            domains: dict = {}
            current, policy, output, decision = adapter.retrieve_for_answer(grant_id=job.grant_id, question=question,
                                                                   admitted_authority=job.admitted_authority,
                                                                   domains=domains)
            records = list(output.records)
            if not records:
                return None, "nothing_matched"
            job.records_used = len(records)
            job.records_digest = digest(sorted(record.record_id for record in records))
            job.output_digest = digest(output.model_dump())
            job.set_decision = decision.model_dump()
            prompt = build_prompt(question, records, precision=policy.search.release_event_time, owner_words=owner_words,
                                  item_domains=[domains.get(record.record_id, ()) for record in records], people=people)
            if question_lacks_permitted_anchor(prompt):
                return None, "question_not_supported"
            try:
                raw = asyncio.run(self.generate(prompt, deadline=job.accepted_at + MAX_END_SECONDS))
            except PolicyError as exc:
                return None, exc.code if exc.code in ("model_unavailable", "model_error") else "model_error"
            with adapter.resolver._read() as (conn, _floor):
                try:
                    checked = post_check_answer(raw, records, prompt, mode=job.mode,
                                                boundary=adapter.resolver.entity_boundary(conn))
                except PolicyError:
                    return None, "body_invalid"
            if self.clock() >= job.accepted_at + MAX_END_SECONDS:
                return None, "deadline"
            _new, _policy, still, _decision = adapter.retrieve_for_answer(grant_id=job.grant_id, question=question,
                                                                 admitted_authority=job.admitted_authority)
            if digest(still.model_dump()) != digest(output.model_dump()):
                return None, "authority_moved"
            return checked, checked.reason
        except PolicyError as exc:
            # BL-156: the refusal's own code (the index missing, stale or not ready; the grant inactive; the
            # authority stale), never `authority_moved` for all of them. `receipt_reason` keeps it counts-only.
            return None, receipt_reason(exc.code)
        except Exception:
            return None, "model_error"

    @staticmethod
    def _receipt(job, checked, reason, started, ended):
        from .answer_checks import TEMPLATE_VERSION
        from .shadow_labeler_local import MODEL, MODEL_REVISION
        return {"version": "topos-local-receipt/answer-v1", "request_id": job.request_id,
            "answer_id": job.answer_id, "grant_id": job.grant_id,
            "policy_hash": job.admitted_authority.policy_hash,
            "protection_revision": job.admitted_authority.protection_revision,
            "node_epoch": job.admitted_authority.node_epoch, "mode": job.mode,
            "question_hash": job.question_hash, "records_used": job.records_used,
            "records_digest": job.records_digest or digest([]), "records_cited": 0 if checked is None else checked.cited,
            "model": {"tag": MODEL, "digest": MODEL_REVISION}, "template_version": TEMPLATE_VERSION,
            "outcome": "answered" if checked is not None and checked.body.outcome == "answered" else "no_answer",
            "reason": reason, "sentences": {"generated": 0 if checked is None else checked.generated,
                "kept": 0 if checked is None else checked.kept,
                "dropped_citation": 0 if checked is None else checked.dropped_citation,
                "dropped_copy": 0 if checked is None else checked.dropped_copy,
                "dropped_question_echo": 0 if checked is None else checked.dropped_question_echo,
                "dropped_relevance": 0 if checked is None else checked.dropped_relevance,
                "dropped_scrub": 0 if checked is None else checked.dropped_scrub},
            "accepted_at": job.accepted_at, "started_at": started, "finished_at": ended}


async def _generate_local(prompt, *, deadline: int):
    """The pinned checking model on this machine; no pull, fallback or hosted call."""
    from .shadow_labeler_local import MODEL, assessment_base_url, open_transport
    client = open_transport(base_url=assessment_base_url())
    try:
        try:
            await client.verify()
        except Exception:
            raise PolicyError("model_unavailable") from None
        remaining = max(1, min(100, deadline - int(time.time())))
        try:
            response = await client.client.post(client.base_url + "/api/chat", timeout=remaining, json={
                "model": MODEL, "stream": False, "think": False,
                # Ollama's host default can be 32K on this Mac. The answer
                # template has at most eight clipped items; an 8K request
                # context retains them while avoiding that host-wide cost.
                "options": {"temperature": 0.2, "num_predict": 512, "num_ctx": 8192},
                "messages": [{"role": "system", "content": prompt.system},
                             {"role": "user", "content": prompt.user}]})
            response.raise_for_status()
            body = response.json()
            raw = (body.get("message") or {}).get("content") if isinstance(body, dict) else None
            if body.get("model") != MODEL or body.get("done") is not True or not isinstance(raw, str) or not raw.strip():
                raise ValueError("incomplete")
            return raw
        except Exception:
            raise PolicyError("model_error") from None
    finally:
        await client.client.aclose()
