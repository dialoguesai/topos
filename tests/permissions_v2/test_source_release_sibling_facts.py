"""P2a raw source release against facts outside the reviewed closure.

A released message discloses every claim any fact draws from it. These tests
write a second fact over the reviewed message (``message-1``) the way other
producers do, then drive the signed P2a adapter from test_release. The
recipient always sees one refusal; the adapter's reason is asserted so a
withhold for an unrelated cause cannot pass.
"""
import json
import sqlite3
import time

import pytest

from tests.permissions_v2.test_evidence import attest, corpus, owner  # noqa: F401 (fixture)
from tests.permissions_v2.test_release import dispatch, issue, release_setup  # noqa: F401 (fixture)
from topos.features.facts.store import FactStore
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.signing import EnvelopeBody, request_digest, sign_envelope

MESSAGE = "I enjoy reading history books."
RELEASED = [{"record_id": "message-1", "source_id": "source-1", "canonical_table": "conversation_messages",
             "content": MESSAGE}]
DATASET_REF = {"table": "conversation_messages", "dataset_id": "dataset-1", "source_id": "source-1",
               "record_id": "message-1"}


def sibling(setup, *, disclosure="owner_only", refs=(DATASET_REF,), raw_refs=None, payload=None, closed=False):
    """A second fact over the reviewed message, written by the native fact store."""
    with sqlite3.connect(setup[5][0].path) as conn:
        fact = FactStore(conn).assert_fact(subject_entity_id="self", predicate="lives_in",
            object_value="Contoso City", dimension="places", disclosure=disclosure,
            source_refs=list(refs), asserted_by="owner")
        object_id = fact["object_id"]
        if raw_refs is not None:
            conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (raw_refs, object_id))
        if payload is not None:
            conn.execute("UPDATE signal_objects SET payload_json=? WHERE object_id=?", (payload(object_id, conn), object_id))
        if closed:
            conn.execute("UPDATE signal_objects SET valid_to='2026-01-01T00:00:00+00:00' WHERE object_id=?", (object_id,))
    return object_id


def read(setup, *, request_id="read-1", envelope=None):
    """Returns (released records, adapter reason). Nothing is sent on a refusal."""
    sent = []
    if envelope is None:
        envelope, payload = issue(setup, request_id=request_id)
    else:
        envelope, payload = envelope
    try:
        dispatch(setup, envelope, payload, request_id=request_id, send=lambda result, output: sent.append(output))
    except PolicyError as exc:
        assert sent == []
        return None, exc.code
    [output] = sent
    return output["records"], None


def next_envelope(setup, request_id):
    """A fresh CP issuance under the grant already active on this node."""
    service, _, cp_key, _, now, corpus = setup
    with owner():
        authority = service.protocol.ledger.authority_snapshot("grant-1", now=now[0])
    payload = {"query": "fact:" + corpus[2]}
    body = EnvelopeBody.parse({**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key",
        "request_id": request_id, "request_type": "permissions.v2.read",
        "request_hash": request_digest("permissions.v2.read", payload), "issued_at": now[0], "expires_at": now[0] + 100})
    return sign_envelope(body, cp_key), payload


# --- withheld ----------------------------------------------------------------

@pytest.mark.parametrize("refs", [
    [{"table": "conversation_messages", "source_id": "source-1", "record_id": "message-1"}],
    [{"table": "conversation_messages", "record_id": "message-1"}],
    [{"record_id": "message-1"}],
    [{"table": "entities", "record_id": "owner-entity"}, {"table": "conversation_messages", "record_id": "message-1"}],
], ids=["no_dataset", "no_source", "no_table", "second_ref"])
def test_an_owner_only_sibling_without_dataset_identity_withholds_the_message(release_setup, refs):
    sibling(release_setup, refs=refs)
    assert read(release_setup) == (None, "owner_only")


@pytest.mark.parametrize("disclosure", ["owner_only", "SCOPED", "", None], ids=repr)
def test_only_an_exactly_scoped_sibling_disclosure_lets_the_message_through(release_setup, disclosure):
    def rewrite(object_id, conn):
        raw = json.loads(conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?", (object_id,)).fetchone()[0])
        if disclosure is None:
            del raw["disclosure"]
        else:
            raw["disclosure"] = disclosure
        return json.dumps(raw)
    sibling(release_setup, disclosure="scoped", payload=rewrite)
    assert read(release_setup) == (None, "owner_only")


def test_an_unreadable_sibling_payload_withholds(release_setup):
    # Closed, so the active-claim copy check cannot be what refuses it.
    sibling(release_setup, payload=lambda *_: '{"disclosure": "scoped"', closed=True)
    assert read(release_setup) == (None, "owner_only")


def test_a_closed_owner_only_sibling_still_withholds(release_setup):
    """Closing a fact retires the claim, not what the message says."""
    object_id = sibling(release_setup, closed=True)
    with sqlite3.connect(release_setup[5][0].path) as conn:
        assert conn.execute("SELECT valid_to FROM signal_objects WHERE object_id=?", (object_id,)).fetchone()[0]
    assert read(release_setup) == (None, "owner_only")


@pytest.mark.parametrize("raw_refs", [
    '[{"table": "conversation_messages", "record_id": "message-1"',
    'conversation_messages/message-1',
    '{"table": "conversation_messages", "record_id": "message-1"}',
    '["conversation_messages:message-1"]',
    '[{"table": "conversation_messages", "record_id": "message\\u002d1"}]',
    '[{"table": "conversation_messages", "record_id": "message-1", "record_id": "other"}]',
    '[{"table": "conversation_messages", "record_id": "message\\u002d1"',
], ids=["truncated", "not_json", "not_a_list", "string_ref", "escaped_id", "duplicate_key", "truncated_escaped_id"])
def test_unreadable_sibling_refs_that_name_the_message_withhold(release_setup, raw_refs):
    sibling(release_setup, raw_refs=raw_refs)
    assert read(release_setup) == (None, "owner_only")


@pytest.mark.parametrize("refs", [
    [{"table": " conversation_messages", "record_id": "message-1"}],
    [{"table": "conversation_messages", "record_id": " message-1 "}],
    [{"table": "conversation_messages", "record_id": "", "id": "message-1"}],
    [{"table": "conversation_messages", "id": "message-1"}],
    [{"table": "canonical", "record_id": "message-1"}],
    [{"table": ["conversation_messages"], "record_id": "message-1"}],
], ids=["padded_table", "padded_id", "empty_record_id", "id_key", "generic_table", "table_not_text"])
def test_a_reference_other_readers_resolve_to_the_message_withholds(release_setup, refs):
    """The node's provenance reader strips both fields and reads `id` when `record_id` is empty.

    A table label that names no other evidence table (the derivation job falls
    back to "canonical") still counts: only a different evidence table has its
    own record id namespace.
    """
    sibling(release_setup, raw_refs=json.dumps(refs))
    assert read(release_setup) == (None, "owner_only")


def test_an_escaped_slash_cannot_hide_a_message_id(release_setup):
    """JSON may spell `/` as `\\/`, so the stored text of such a reference never contains the id."""
    corpus = release_setup[5]
    with sqlite3.connect(corpus[0].path) as conn:
        conn.execute("UPDATE conversation_messages SET message_id='mail/message-1'")
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?",
                     (json.dumps([{**DATASET_REF, "record_id": "mail/message-1"}]), corpus[2]))
    attest(corpus, review_id="review-2")
    assert read(release_setup, request_id="read-1") == ([{**RELEASED[0], "record_id": "mail/message-1"}], None)
    sibling(release_setup, raw_refs='[{"table": "conversation_messages", "record_id": "mail\\/message-1"}]')
    assert read(release_setup, request_id="read-2", envelope=next_envelope(release_setup, "read-2")) == (None, "owner_only")


def test_a_sibling_written_after_the_review_withholds_the_very_next_read(release_setup):
    assert read(release_setup, request_id="read-1") == (RELEASED, None)
    sibling(release_setup, refs=[{"table": "conversation_messages", "source_id": "source-1", "record_id": "message-1"}])
    # Same grant, same review, same protection revision: only the sibling changed.
    assert read(release_setup, request_id="read-2", envelope=next_envelope(release_setup, "read-2")) == (None, "owner_only")


# --- released ----------------------------------------------------------------

def test_a_scoped_sibling_does_not_withhold(release_setup):
    sibling(release_setup, disclosure="scoped", refs=[{"table": "conversation_messages", "record_id": "message-1"}])
    assert read(release_setup) == (RELEASED, None)


@pytest.mark.parametrize("sibling_refs", [
    {"refs": [{"table": "ai_chat_messages", "source_id": "source-1", "record_id": "message-1"}]},
    {"refs": [{"table": " signal_objects ", "record_id": "message-1"}]},
    {"refs": [{"table": "conversation_messages", "record_id": "message-10"}]},
    {"refs": [{"table": "canonical", "record_id": "message-2", "id": "message-10"}]},
    {"refs": [{"table": "conversation_messages", "record_id": "message-2"}]},
    {"raw_refs": '[{"table": "conversation_messages", "record_id": "message-2"'},
    {"raw_refs": '[{"table": "conversation_messages", "record_id": "caf\\u00e9"}]'},
], ids=["other_table", "padded_other_table", "longer_id", "other_ids", "other_id", "unreadable_other_id", "escaped_other_id"])
def test_an_owner_only_fact_over_a_different_record_does_not_withhold(release_setup, sibling_refs):
    sibling(release_setup, **sibling_refs)
    assert read(release_setup) == (RELEASED, None)


def test_the_sibling_floor_holds_over_fifty_thousand_unrelated_facts(release_setup):
    """Scale control: one scan, parse only what could name a leaf. Timing is reported, not asserted tightly."""
    from topos.permissions_v2.evidence import EvidenceResolver
    rows = []
    for index in range(50_000):
        refs = [{"table": "conversation_messages", "dataset_id": "dataset-1", "source_id": "source-1",
                 "record_id": f"northwind-message-{index}"}]
        if index % 50 == 0:
            refs[0]["note"] = "Café Fabrikam"  # a \u00 escape in the stored text: parsed, not matched
        rows.append((f"synthetic-fact-{index}", "profile", "fact", f"synthetic:{index}",
                     json.dumps({"subject_entity_id": "contoso-person", "predicate": "prefers",
                                 "object_value": f"northwind item {index}", "disclosure": "owner_only"}),
                     json.dumps(refs), "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    with sqlite3.connect(release_setup[5][0].path) as conn:
        conn.executemany("INSERT INTO signal_objects(object_id,signal_dimension,object_type,object_key,payload_json,"
                         "source_refs_json,valid_from,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", rows)
    timings = []
    real = EvidenceResolver._source_sibling_floor

    def timed(self, conn, snapshot):
        started = time.perf_counter()
        try:
            return real(self, conn, snapshot)
        finally:
            timings.append(time.perf_counter() - started)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(EvidenceResolver, "_source_sibling_floor", timed)
        assert read(release_setup, request_id="read-1") == (RELEASED, None)
        sibling(release_setup)
        assert read(release_setup, request_id="read-2", envelope=next_envelope(release_setup, "read-2")) == (None, "owner_only")
    assert len(timings) == 2 and max(timings) < 2.0, timings
