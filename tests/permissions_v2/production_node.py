"""A node with a real ledger and real signatures over the production-DDL corpus.

The locator door (`SourceMessageRelease`) with one p2a grant on the attested
subject rule: the `work` domain over imessage messages, which every positive unit
of `production_corpus` carries. Mirrors `test_source_release_attested.node`, but
over the production schema.
"""
from __future__ import annotations

from copy import deepcopy

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.permissions_v2 import production_corpus as pc
from tests.permissions_v2.test_contract_and_ledger import sample_policy
from tests.permissions_v2.test_fact_attested_subject import SUBJECT_BINDING
from tests.permissions_v2.test_release import dispatch
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.forwarding import verify_node_result
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.release import SourceMessageRelease, VOCABULARY
from topos.permissions_v2.signing import parse_envelope, request_digest, sign_envelope

V2 = "permissions-beta/p2a-v2"
NOW = 1101


def work_policy(binding, *, capability=V2, evaluator="hard-rules/p2a-v2", view=None, grant_id="grant-1"):
    raw = sample_policy()
    raw["binding"].update(binding.model_dump())
    raw["binding"]["grant_id"] = grant_id
    raw["versions"] = {"vocabulary": VOCABULARY, "capability": capability, "subject_binding": deepcopy(SUBJECT_BINDING)}
    raw["evaluator"] = {"kind": "hard_rules", "version": evaluator}
    raw["source_universe"]["source_ids"] = [pc.SOURCE]
    rule = raw["rules"][0]
    rule["evidence_use"]["sources"]["values"] = [pc.SOURCE]
    work = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": ["work"]}
    rule["evidence_use"]["predicate"], rule["release"]["predicate"] = work, deepcopy(work)
    if view is not None:
        for form in rule["release"]["forms"]:
            form["view_id"] = view
    return raw


class Node:
    def __init__(self, corpus: pc.Corpus, tmp_path, *, policy=work_policy):
        self.corpus = corpus
        self.cp_key, self.node_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        self.cp_keys = {"cp-key": self.cp_key.public_key().public_bytes_raw()}
        with corpus.resolver._read() as (_, floor):
            self.ledger = PolicyLedger(tmp_path / "ledger.db", identity=NodeIdentity.parse(corpus.resolver.binding.model_dump()),
                                       protection_revision=floor, trusted_keys=self.cp_keys)
        self.protocol = NodePolicyProtocol(self.ledger, canonical_database=corpus.resolver.path, cp_issuer_id="cp-issuer",
            frontend_client_id="owner-ui", trusted_cp_keys=self.cp_keys, node_signing_kid="node-key",
            node_signing_key=self.node_key)
        self.now = [NOW]
        self.service = SourceMessageRelease(protocol=self.protocol, resolver=corpus.resolver, reviews=corpus.reviews,
                                            clock=lambda: self.now[0])
        self.policy = policy(corpus.resolver.binding)
        self.grants = 0
        self.activate(self.policy)

    @property
    def setup(self):
        """The tuple test_release.dispatch reads."""
        return (self.service, self.policy, self.cp_key, self.node_key, self.now, None)

    def activate(self, raw):
        epoch = self._epoch()
        with pc.owner():
            self.ledger.activate(raw, grant_generation=1, assignment_generation=1, expected_epoch=epoch,
                                 command_id=f"activate-{self.grants}", now=self.now[0])
        self.grants += 1

    def _epoch(self) -> int:
        with self.ledger._transaction() as conn:
            self.protocol._sync_protection(conn)
            return self.ledger._node(conn)["epoch"]

    def issue(self, fact_id: str, *, request_id: str, grant_id: str = "grant-1"):
        with self.ledger._transaction() as conn:
            self.protocol._sync_protection(conn)
        with pc.owner():
            authority = self.ledger.authority_snapshot(grant_id, now=self.now[0])
        payload = {"query": "fact:" + fact_id}
        body = parse_envelope({**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key",
            "request_id": request_id, "request_type": "permissions.v2.read",
            "request_hash": request_digest("permissions.v2.read", payload), "issued_at": self.now[0],
            "expires_at": self.now[0] + 100}, signed=False)
        return sign_envelope(body, self.cp_key), payload

    def read(self, fact_id: str, *, request_id: str, grant_id: str = "grant-1", send=None):
        """((result, output), None) on release or (None, reason)."""
        envelope, payload = self.issue(fact_id, request_id=request_id, grant_id=grant_id)
        sent = []
        try:
            dispatch(self.setup, envelope, payload, request_id=request_id,
                     send=send or (lambda result, output: sent.append((result, output))))
        except PolicyError as exc:
            return None, exc.code
        if send is not None:
            return None, None
        [released] = sent
        verify_node_result(released[0], trusted_keys={"node-key": self.node_key.public_key().public_bytes_raw()},
                           envelope=envelope, output=released[1], now=self.now[0])
        return released, None
