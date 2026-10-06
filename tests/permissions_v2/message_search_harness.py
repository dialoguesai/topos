"""An in-process node for search tests: a generated corpus, a real ledger, real signatures.

It carries two grants over the same rules: the search grant under test and a p2a-v2 locator grant. The locator
door is removed from the node (N8); the adapter here is the test-only copy (`retired_doors.py`), kept as the
access oracle of the retired-profile suites: discovery is a subset of access exactly when every record the
search grant returns is released, byte for byte, by that adapter for its fact.

Only p2c-v3 can build an index or answer. A node under p2c-v1 or p2c-v2 is built only inside the
`retired_search_profile` fixture (`conftest.py`), which lifts the retirement for that test.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401 (import check only)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.permissions_v2 import message_search_corpus as mc
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.forwarding import verify_node_result
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from tests.permissions_v2.retired_doors import SourceMessageRelease
from topos.permissions_v2 import search_index as search_index_module
from topos.permissions_v2.search_index import SearchIndexService
from topos.permissions_v2.search_release import MessageSearchRelease
from topos.permissions_v2.signing import parse_envelope, request_digest, sign_envelope
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal


@contextmanager
def as_principal(**fields):
    token = set_principal(Principal(**fields))
    try:
        yield
    finally:
        reset_principal(token)


def owner():
    return as_principal(cls=OWNER_APP, channel="uds", acting_user=mc.OWNER_ID)


def recipient(actor="actor-1", client="client-2"):
    return as_principal(cls=THIRD_PARTY, channel="cp_relay", acting_user=actor, client_id=client)


def fake_embedder(query: str, model: str):
    """Deterministic bag-of-words vectors; the same function embeds members in `embed_corpus`."""
    from topos.permissions_v2.search_index import tokenize
    vector = [0.0] * 32
    for token in tokenize(query):
        vector[hash_token(token) % 32] += 1.0
    return vector if any(vector) else None


def hash_token(token: str) -> int:
    import hashlib
    return int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:4], "big")


def embed_corpus(corpus: mc.Corpus, *, model: str = "fake-model", skip_every: int = 0) -> None:
    """Write signal_embeddings rows for every message (hidden ones too), as the embeddings job would."""
    with sqlite3.connect(corpus.path) as conn:
        rows = conn.execute("SELECT message_id, source_id, content FROM conversation_messages").fetchall()
        for number, (message_id, source_id, content) in enumerate(rows):
            if skip_every and number % skip_every == 0:
                continue
            vector = fake_embedder(content, model) or [0.0] * 32
            conn.execute("INSERT INTO signal_embeddings(embedding_id, record_id, source_id, model, dims, vector_blob, "
                         "vector_format, chunk_index, search_text) VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"emb-{number}", message_id, source_id, model, 32, json.dumps(vector), "json", 0, content))


class Node:
    def __init__(self, corpus: mc.Corpus, root: Path, *, model: str | None = "fake-model", search_raw=None,
                 now: int = mc.NOW, frontend_client_id: str = "client-2"):
        search_raw = search_raw or mc.search_policy()
        # Only p2c-v3 can build an index or answer (N8). A node under a retired profile refuses every search at
        # the envelope parser, so a test asserting some OTHER refusal on it would pass without reaching that
        # check. Read at call time: the `retired_search_profile` fixture is the one thing that widens it.
        if search_raw["versions"]["capability"] not in search_index_module.RELEASABLE_SEARCH_CAPABILITIES:
            raise AssertionError("retired search profile: build a p2c-v3 node (direct_search_twins.build), or take "
                                 "the `retired_search_profile` fixture in a suite that exercises a retired profile")
        self.corpus, self.now = corpus, [now]
        root.mkdir(parents=True, exist_ok=True)
        self.cp_key, self.node_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32))), Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
        self.cp_keys = {"cp-key": self.cp_key.public_key().public_bytes_raw()}
        resolver = corpus.resolver
        with resolver._read() as (_, floor):
            self.ledger = PolicyLedger(root / "ledger.db", identity=NodeIdentity.parse(resolver.binding.model_dump()),
                                       protection_revision=floor, trusted_keys=self.cp_keys)
        self.protocol = NodePolicyProtocol(self.ledger, canonical_database=resolver.path, cp_issuer_id="cp-issuer",
            frontend_client_id=frontend_client_id, trusted_cp_keys=self.cp_keys, node_signing_kid="node-key",
            node_signing_key=self.node_key)
        # Where the runtime puts it (runtime.message_search_index), so lifecycle hooks find it.
        from topos.permissions_v2.opaque_ids import private_directory
        from topos.permissions_v2.search_index import root_for
        private_directory(root_for(resolver.path).parent)
        self.index = SearchIndexService(ledger=self.ledger, resolver=resolver, reviews=corpus.reviews,
                                        root=root_for(resolver.path), embedding_model=(lambda: model))
        self.search = MessageSearchRelease(protocol=self.protocol, resolver=resolver, reviews=corpus.reviews,
                                           index=self.index, clock=lambda: self.now[0], embedder=fake_embedder)
        self.locator = SourceMessageRelease(protocol=self.protocol, resolver=resolver, reviews=corpus.reviews,
                                            clock=lambda: self.now[0])
        self.search_raw = search_raw
        self.p2a_raw = mc.p2a_v2_policy()
        self.activate(self.search_raw)
        self.activate(self.p2a_raw)
        self.requests = 0

    def epoch(self):
        with self.ledger._transaction() as conn:
            self.protocol._sync_protection(conn)
            return self.ledger._node(conn)["epoch"]

    def activate(self, raw, *, generation=1):
        with owner():
            self.ledger.activate(raw, grant_generation=generation, assignment_generation=generation,
                                 expected_epoch=self.epoch(), command_id=f"activate-{raw['binding']['grant_id']}-{generation}",
                                 now=self.now[0])

    def rebuild(self):
        with owner():
            return self.index.rebuild_all(now=self.now[0])

    def _envelope(self, grant_id, request_type, payload, request_id):
        with self.ledger._transaction() as conn:
            self.protocol._sync_protection(conn)
        with owner():
            authority = self.ledger.authority_snapshot(grant_id, now=self.now[0])
        body = parse_envelope({**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key",
            "request_id": request_id, "request_type": request_type, "request_hash": request_digest(request_type, payload),
            "issued_at": self.now[0], "expires_at": self.now[0] + 100}, signed=False)
        return sign_envelope(body, self.cp_key)

    def next_id(self, prefix):
        self.requests += 1
        return f"{prefix}-{self.requests}"

    def search_request(self, query, *, k=25, window=None, grant_id=None, request_id=None, actor="actor-1", client=None):
        """(output, None) on answer or (None, reason). Verifies the node signature on every answer."""
        grant_id = grant_id or self.search_raw["binding"]["grant_id"]
        client = client or self.search_raw["binding"]["client_id"]
        payload = {"query": query, "k": k} if window is None else {"query": query, "k": k, "window": window}
        request_id = request_id or self.next_id("search")
        envelope = self._envelope(grant_id, "permissions.v2.search", payload, request_id)
        try:
            with recipient(actor, client):
                result, output = self.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id)
        except PolicyError as exc:
            return None, exc.code
        verify_node_result(result, trusted_keys={"node-key": self.node_key.public_key().public_bytes_raw()},
                           envelope=envelope, output=output, now=self.now[0])
        return output, None

    def search_batch_request(self, queries, *, k=25, grant_id=None, batch_id=None, actor="actor-1", client=None):
        """A batch (OD-36) of distinct queries: ([output, ...], None) on answer or (None, reason). Every item's
        node signature is verified against its own envelope, as `search_request` verifies a single answer."""
        grant_id = grant_id or self.search_raw["binding"]["grant_id"]
        client = client or self.search_raw["binding"]["client_id"]
        batch_id = batch_id or self.next_id("batch")
        payloads = [{"query": query, "k": k} for query in queries]
        envelopes = [self._envelope(grant_id, "permissions.v2.search", payload, f"{batch_id}:{number}")
                     for number, payload in enumerate(payloads)]
        items = [{"envelope": envelope.model_dump(), "payload": payload, "request_id": envelope.request_id}
                 for envelope, payload in zip(envelopes, payloads)]
        try:
            with recipient(actor, client):
                answered = self.search.dispatch_batch(items=items)
        except PolicyError as exc:
            return None, exc.code
        trusted = {"node-key": self.node_key.public_key().public_bytes_raw()}
        for envelope, (result, output) in zip(envelopes, answered):
            verify_node_result(result, trusted_keys=trusted, envelope=envelope, output=output, now=self.now[0])
        return [output for _result, output in answered], None

    def locator_read(self, fact_id):
        """The access oracle: the test-only locator adapter for one fact under the p2a-v2 grant with the same rules."""
        request_id = self.next_id("read")
        payload = {"query": "fact:" + fact_id}
        envelope = self._envelope(self.p2a_raw["binding"]["grant_id"], "permissions.v2.read", payload, request_id)
        sent = []
        try:
            with recipient("actor-1", "client-1"):
                self.locator.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id,
                                      send=lambda result, output: sent.append(output))
        except PolicyError:
            return None
        return sent[0]


def pin_record_key(node: "Node", key: bytes = bytes(range(100, 132)), grant_id: str | None = None) -> None:
    """Twin nodes share one id key, so their recipient-visible bytes can be compared exactly."""
    grant_id = grant_id or node.search_raw["binding"]["grant_id"]
    node.index.keys.delete(grant_id)
    with node.index.keys._db() as db:
        db.execute("INSERT INTO p2c_record_keys VALUES (?, ?)", (grant_id, key))


def twin(tmp_path, name, *, seed, counts, hidden_messages=0, extra_withheld=None, model="fake-model"):
    corpus = mc.build(tmp_path / name / "corpus", seed=seed, counts=counts, hidden_messages=hidden_messages,
                      extra_withheld=extra_withheld)
    embed_corpus(corpus)
    node = Node(corpus, tmp_path / name, model=model)
    pin_record_key(node)
    node.rebuild()
    return node
