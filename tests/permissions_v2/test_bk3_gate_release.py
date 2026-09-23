"""R12: the locator door checkpoints under the node write gate, releases it, then sends.

The linearization point is the checkpoint. After it, and with no gate held, the
door re-syncs protection and re-reads the grant's authority in one brief ledger
transaction; anything that moved refuses the send. So:

  G1  an owner write waiting for the gate no longer waits out the send
  G2  a revoke committed between the checkpoint and the re-read stops the send
  G3  an Off-limits (black hole) write in that window stops the send too, because
      the re-read runs the protection sync first
  G4  a revoke after the re-read does not retract bytes already handed to send:
      the documented linearization
  G5  every branch consumes the request; nothing is resendable
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from tests.permissions_v2 import production_corpus as pc
from tests.permissions_v2.production_node import Node
from topos.permissions_v2 import release
from topos.permissions_v2.canonical import PolicyError
from topos.storage.db.write_gate import with_db_write


@pytest.fixture
def node(tmp_path):
    corpus = pc.build(tmp_path / "corpus", seed=21, positives=3)
    return Node(corpus, tmp_path)


def revoke(node):
    with pc.owner():
        node.ledger.revoke("grant-1", expected_epoch=node._epoch(), command_id="revoke-1")


def test_G1_an_owner_write_does_not_wait_out_the_send(node):
    entered, release_send = threading.Event(), threading.Event()
    outcome = {}

    def blocking_send(result, output):
        entered.set()
        release_send.wait(10)
        outcome["sent"] = output

    reader = threading.Thread(target=lambda: outcome.setdefault(
        "read", node.read(node.corpus.positives[0], request_id="read-1", send=blocking_send)))
    reader.start()
    assert entered.wait(10)
    acquired = threading.Event()

    def owner_write():
        with with_db_write():
            acquired.set()
    writer = threading.Thread(target=owner_write)
    writer.start()
    try:
        assert acquired.wait(2), "the node write gate is still held through the send"
    finally:
        release_send.set()
        reader.join(10)
        writer.join(10)
    assert outcome["sent"]["records"]


def test_G2_a_revoke_before_the_re_read_stops_the_send(node, monkeypatch):
    real = release.SourceMessageRelease._authority_after_checkpoint

    def revoked_first(self, signed):
        revoke(node)
        return real(self, signed)
    monkeypatch.setattr(release.SourceMessageRelease, "_authority_after_checkpoint", revoked_first)
    sent = []
    released, reason = node.read(node.corpus.positives[0], request_id="read-1", send=lambda *args: sent.append(args))
    assert sent == [] and reason is not None


def test_G3_an_off_limits_write_before_the_re_read_stops_the_send(node, monkeypatch):
    real = release.SourceMessageRelease._authority_after_checkpoint

    def protected_first(self, signed):
        with sqlite3.connect(node.corpus.path) as conn:
            conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) VALUES('conversation_messages','imessage:1')")
        return real(self, signed)
    monkeypatch.setattr(release.SourceMessageRelease, "_authority_after_checkpoint", protected_first)
    sent = []
    released, reason = node.read(node.corpus.positives[0], request_id="read-1", send=lambda *args: sent.append(args))
    assert sent == [] and reason == "authority_stale"


def test_G4_a_revoke_after_the_re_read_does_not_retract_the_send(node, monkeypatch):
    real = release.SourceMessageRelease._authority_after_checkpoint

    def revoked_after(self, signed):
        authority = real(self, signed)
        revoke(node)
        return authority
    monkeypatch.setattr(release.SourceMessageRelease, "_authority_after_checkpoint", revoked_after)
    sent = []
    node.read(node.corpus.positives[0], request_id="read-1", send=lambda *args: sent.append(args))
    assert len(sent) == 1


@pytest.mark.parametrize("branch", ["released", "stopped_after_checkpoint", "send_failed"])
def test_G5_every_branch_consumes_the_request(node, monkeypatch, branch):
    from tests.permissions_v2.test_release import dispatch
    if branch == "stopped_after_checkpoint":
        real = release.SourceMessageRelease._authority_after_checkpoint

        def revoked_first(self, signed):
            revoke(node)
            return real(self, signed)
        monkeypatch.setattr(release.SourceMessageRelease, "_authority_after_checkpoint", revoked_first)

    def send(result, output):
        if branch == "send_failed":
            raise RuntimeError("socket closed")
    envelope, payload = node.issue(node.corpus.positives[0], request_id="read-1")
    try:
        dispatch(node.setup, envelope, payload, request_id="read-1", send=send)
    except (PolicyError, RuntimeError):
        pass
    monkeypatch.undo()
    with sqlite3.connect(node.ledger.path) as conn:
        assert conn.execute("SELECT status FROM p2a_requests WHERE request_id='read-1'").fetchone() == ("checkpointed",)
    if branch != "stopped_after_checkpoint":  # a revoked grant refuses at admission before the replay check
        with pytest.raises(PolicyError, match="request_replay"):
            dispatch(node.setup, envelope, payload, request_id="read-1", send=send)
