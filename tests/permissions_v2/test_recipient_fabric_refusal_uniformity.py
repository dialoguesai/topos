"""Recipient v2 reads at the node: principal door and refusal uniformity.

The CP relays exactly two recipient message types. The node half is held to
these properties through the real WebSocket dispatch functions, with real
SQLite evidence, owner reviews and a real policy ledger:

  F1  a relayed read with no stamp, a tampered stamp, or any stamp other than a
      verified third_party one is refused before the runtime (and so any
      evidence, review or ledger row) is touched
  F2  a third_party stamp naming the OWNER as acting user stays third_party: it
      reaches no owner-only handler and the adapter grants it no owner reading
  U1  every refusal class leaves the node as byte-identical frames whatever the
      internal reason; the reason is recorded per class so uniformity is not the
      trivial "everything failed the same early check"

Wall time per class is printed (run with -s). It is a report, not an assertion:
a gap measured in this harness is not a claim about the network.
"""
import base64
import sqlite3
import statistics
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.permissions_v2.test_evidence import corpus, owner, edit, payload as change_fact
from tests.permissions_v2.test_fact_release import fact_setup, timed, projection_service
from tests.permissions_v2.test_release import release_setup
from topos.permissions_v2 import fact_release_transport, release_transport
from topos.permissions_v2.canonical import PolicyError, canonical_bytes
from topos.permissions_v2.signing import EnvelopeBody, FactEnvelopeBody, request_digest, sign_envelope
from topos.principal import THIRD_PARTY, Principal, reset_principal, set_principal
from topos.relay_stamp import canonical_signing_payload, verify_relay_stamp

RUNS = 12
PROFILES = {
    "source": dict(transport=release_transport, dispatch=release_transport.dispatch_source_message,
        flag="TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED", adapter="SourceMessageRelease",
        request_type="permissions.v2.read", body=EnvelopeBody, setup="release_setup"),
    "fact": dict(transport=fact_release_transport, dispatch=fact_release_transport.dispatch_fact_message,
        flag=fact_release_transport.FLAG, adapter="FactProjectionRelease",
        request_type="permissions.v2.fact.read", body=FactEnvelopeBody, setup="fact_setup"),
}
DOOR = ["no_stamp", "tampered_signature", "tampered_actor_after_signing", "tampered_type_after_signing",
        "unpinned_signer", "expired_stamp", "owner_app_stamp", "owner_automation_stamp", "cp_admin_stamp", "node_disabled"]
# Each class's own internal reason. The CP twin (topos-control-plane
# tests/control_plane/test_permissions_v2_recipient_fabric.py NODE_REASONS)
# replays these through the real transport; keep the two in step.
REASONS = {"no_assignment": "grant_inactive", "nonexistent_fact": "evidence_missing", "owner_only_fact": "owner_only",
    "rule_deny": "permission_denied", "unreviewed_fact": "owner_review_required", "stale_review": "review_stale",
    "other_recipient_fact": "permission_denied", "superseded_fact": "evidence_deleted",
    "revoked_grant": "grant_inactive", "expired_grant": "policy_time"}
EVIDENCE = list(REASONS)
TIMINGS = {}


def refusal(request_id, message_type):
    # The only frame the node may emit when it refuses a recipient read.
    return canonical_bytes({"id": request_id, "type": message_type, "status": "error",
                            "code": 403, "error": "permission_denied"}).decode("ascii")


def sign_stamp(message, key, **changes):
    now = time.time()
    fields = {"v": 1, "cls": "third_party", "client_id": "client-1", "acting_user": "actor-1", "iat": now, "exp": now + 100}
    fields.update(changes)
    fields["sig"] = base64.b64encode(key.sign(canonical_signing_payload(fields, msg_id=message["id"], msg_type=message["type"]))).decode()
    message["principal_stamp"] = fields
    return message


class Lane:
    """One profile's real node services, a pinned CP stamp key and a spied runtime."""

    def __init__(self, profile, setup, monkeypatch):
        self.spec = PROFILES[profile]
        self.release, self.policy, self.cp_key, self.now, self.corpus = setup[0], setup[1], setup[2], setup[4], setup[5]
        self.transport = self.spec["transport"]
        self.message_type = self.transport.MESSAGE_TYPE
        self.runtime_reads, self.reasons = [], []
        monkeypatch.setenv(self.spec["flag"], "true")
        monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(self.cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode())
        monkeypatch.setattr(self.transport.time, "time", lambda: self.now[0])
        if profile == "source":
            runtime = SimpleNamespace(protocol=self.release.protocol,
                evidence_reviews=lambda **kw: SimpleNamespace(resolver=self.release.resolver, reviews=self.release.reviews))
        else:
            runtime = SimpleNamespace(protocol=self.release.protocol, projection_reviews=lambda **kw: self.release.projections)
        def get_runtime():
            # Every evidence, review and ledger read goes through this runtime.
            self.runtime_reads.append(True)
            return runtime
        monkeypatch.setattr(self.transport, "get_runtime", get_runtime)
        original, reasons = getattr(self.transport, self.spec["adapter"]), self.reasons
        class Recording(original):
            def dispatch(self, **kwargs):
                try:
                    return super().dispatch(**kwargs)
                except Exception as exc:
                    reasons.append(getattr(exc, "code", type(exc).__name__))
                    raise
        monkeypatch.setattr(self.transport, self.spec["adapter"], Recording)

    def activate(self, policy=None, *, command_id="activate-1"):
        policy = deepcopy(policy or self.policy)
        ledger = self.release.protocol.ledger
        with ledger._transaction() as db:
            # Issue against current authority, as the CP would after a resync.
            self.release.protocol._sync_protection(db)
        with ledger._transaction() as db:
            epoch = ledger._node(db)["epoch"]
        with owner():
            ledger.activate(policy, grant_generation=1, assignment_generation=1,
                expected_epoch=epoch, command_id=command_id, now=self.now[0])
            return self.release.protocol.ledger.authority_snapshot(policy["binding"]["grant_id"], now=self.now[0])

    def message(self, authority, request_id, *, fact_id=None, lifetime=100, **identity):
        payload = {"query": "fact:" + (fact_id or self.corpus[2])}
        body = self.spec["body"].parse({**authority.model_dump(), **identity, "version": "topos-grantee-envelope/v2",
            "kid": "cp-key", "request_id": request_id, "request_type": self.spec["request_type"],
            "request_hash": request_digest(self.spec["request_type"], payload),
            "issued_at": self.now[0], "expires_at": self.now[0] + lifetime})
        message = {"id": request_id, "type": self.message_type,
                   "payload": {"envelope": sign_envelope(body, self.cp_key).model_dump(), "intent": payload}}
        return sign_stamp(message, self.cp_key, client_id=identity.get("client_id", "client-1"),
                          acting_user=identity.get("actor_id", "actor-1"))

    async def send(self, message):
        sent = []
        async def raw(value):
            sent.append(value)
        started = time.perf_counter()
        await self.spec["dispatch"](SimpleNamespace(send=raw), message)
        return sent, time.perf_counter() - started


def open_lane(request, profile, monkeypatch):
    return Lane(profile, request.getfixturevalue(PROFILES[profile]["setup"]), monkeypatch)


def restricted(policy, *, domain):
    policy = deepcopy(policy)
    for rule in policy["rules"]:
        rule["evidence_use"]["predicate"]["values"] = [domain]
        rule["release"]["predicate"]["values"] = [domain]
    return policy


def new_unreviewed_fact(lane):
    from topos.features.facts.store import FactStore
    with sqlite3.connect(lane.corpus[0].path) as conn:
        valid_from = conn.execute("SELECT valid_from FROM signal_objects WHERE object_id=?", (lane.corpus[2],)).fetchone()[0]
        created = FactStore(conn).assert_fact(subject_entity_id="self", predicate="prefers", object_value="poetry",
            disclosure="scoped", asserted_by="owner", valid_from=valid_from,
            source_refs=[{"table": "conversation_messages", "dataset_id": "dataset-1", "source_id": "source-1", "record_id": "message-1"}])
    return created["object_id"]


def prepare(lane, kind):
    """RUNS signed messages refused for `kind`; state changes land after issuance."""
    policy = deepcopy(lane.policy)
    if kind == "rule_deny":
        deny = deepcopy(policy["rules"][0])
        deny.update(rule_id="deny-reading", effect="deny")
        policy["rules"].append(deny)
    elif kind == "expired_grant":
        policy["validity"]["expires_at"] = lane.now[0] + 50
    elif kind == "superseded_fact":
        # The owner's correction closes the fact; the CP then issues against the
        # node's current authority, so the refusal is the supersession itself.
        from topos.features.facts.verdicts import edit_fact
        with sqlite3.connect(lane.corpus[0].path) as conn:
            edit_fact(conn, lane.corpus[2], object_value="science books", note="synthetic correction")
    authority = lane.activate(policy)
    identity, fact_id = {}, None
    if kind == "no_assignment":
        # Well-formed and CP-signed, for a recipient the node holds no grant for.
        identity = dict(actor_id="actor-2", client_id="client-2", grant_id="grant-2", assignment_id="assignment-2")
    elif kind == "other_recipient_fact":
        # actor-2 holds its own active grant that does not cover this fact, which
        # actor-1's grant would release.
        other = restricted(policy, domain="finance")
        other["policy_version_id"] = "policy-other-recipient"
        other["binding"].update(actor_id="actor-2", client_id="client-2", grant_id="grant-2", assignment_id="assignment-2")
        authority = lane.activate(other, command_id="activate-2")
        identity = dict(actor_id="actor-2", client_id="client-2")
    elif kind == "nonexistent_fact":
        fact_id = "nonexistent-synthetic-fact"
    elif kind == "unreviewed_fact":
        fact_id = new_unreviewed_fact(lane)
    lifetime = 50 if kind == "expired_grant" else 100
    messages = [lane.message(authority, f"read-{index}", fact_id=fact_id, lifetime=lifetime, **identity) for index in range(RUNS)]
    if kind == "owner_only_fact":
        change_fact(lane.corpus, disclosure="owner_only")
    elif kind == "stale_review":
        edit(lane.corpus, "UPDATE conversation_messages SET content='A different synthetic sentence.'")
    elif kind == "revoked_grant":
        with owner():
            ledger = lane.release.protocol.ledger
            with ledger._transaction() as db:
                epoch = ledger._node(db)["epoch"]
            ledger.revoke("grant-1", expected_epoch=epoch, command_id="revoke-1")
    elif kind == "expired_grant":
        lane.now[0] += 50
    return messages


def door(lane, kind, message):
    """Mutate one validly stamped message into a door-refusal class."""
    stamp = message["principal_stamp"]
    if kind == "no_stamp":
        del message["principal_stamp"]
    elif kind == "tampered_signature":
        stamp["sig"] = base64.b64encode(b"\x00" * 64).decode()
    elif kind == "tampered_actor_after_signing":
        stamp["acting_user"] = "actor-2"
    elif kind == "tampered_type_after_signing":
        # Relabelled to the other read type: the transport's type check refuses it.
        message["type"] = "permissions_v2_source_read" if lane.message_type != "permissions_v2_source_read" else "permissions_v2_fact_read"
    elif kind == "unpinned_signer":
        sign_stamp(message, Ed25519PrivateKey.generate())
    elif kind == "expired_stamp":
        sign_stamp(message, lane.cp_key, iat=lane.now[0] - 700, exp=lane.now[0] - 1)
    elif kind == "owner_app_stamp":
        sign_stamp(message, lane.cp_key, cls="owner_app", acting_user="owner-1", client_id="topos_home_chat")
    elif kind == "owner_automation_stamp":
        sign_stamp(message, lane.cp_key, cls="owner_automation", acting_user="owner-1", client_id="routines")
    elif kind == "cp_admin_stamp":
        sign_stamp(message, lane.cp_key, cls="cp_admin", acting_user="owner-1", client_id="cp-admin")
    return message


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", sorted(PROFILES))
@pytest.mark.parametrize("kind", DOOR)
async def test_F1_non_recipient_principal_is_refused_before_any_runtime_read(request, monkeypatch, profile, kind):
    lane = open_lane(request, profile, monkeypatch)
    authority = lane.activate()
    message = door(lane, kind, lane.message(authority, "read-0"))
    if kind == "node_disabled":
        monkeypatch.setenv(lane.spec["flag"], "false")
    if kind not in {"no_stamp", "node_disabled", "tampered_type_after_signing"}:
        assert verify_relay_stamp(message) is None or verify_relay_stamp(message).cls != THIRD_PARTY
    sent, _ = await lane.send(message)
    assert sent == [refusal("read-0", lane.message_type)]
    # Refused at the door: no runtime, so no ledger admission, evidence or review read.
    assert lane.runtime_reads == [] and lane.reasons == []
    with lane.release.protocol.ledger._transaction() as db:
        assert db.execute("SELECT count(*) FROM p2a_requests").fetchone()[0] == 0


@pytest.mark.parametrize("profile", sorted(PROFILES))
@pytest.mark.parametrize("principal", [
    dict(cls="owner_app", channel="cp_relay", acting_user="owner-1", client_id="topos_home_chat"),
    dict(cls="owner_app", channel="uds", acting_user="owner-1", client_id="topos_home_chat"),
    dict(cls="cp_relay", channel="cp_relay", acting_user="actor-1", client_id="client-1"),
    dict(cls="cp_admin", channel="cp_relay", acting_user="owner-1", client_id="cp-admin"),
    dict(cls="owner_automation", channel="cp_relay", acting_user="owner-1", client_id="routines"),
    dict(cls=THIRD_PARTY, channel="local_http", acting_user="actor-1", client_id="client-1"),
    dict(cls=THIRD_PARTY, channel="cp_relay", acting_user="", client_id="client-1"),
    dict(cls=THIRD_PARTY, channel="cp_relay", acting_user="actor-1", client_id=""),
])
def test_F1_adapter_itself_refuses_before_ledger_or_evidence(request, monkeypatch, profile, principal):
    # Defense in depth under the transport: the adapters re-check the principal
    # before admission, so an in-process caller cannot skip the relay door.
    lane = open_lane(request, profile, monkeypatch)
    message = lane.message(lane.activate(), "read-0")
    touched = []
    ledger = lane.release.protocol.ledger
    monkeypatch.setattr(ledger, "admit", lambda *a, **k: touched.append("admit"))
    reader = lane.release.resolver if profile == "source" else lane.release.projections
    name = "with_qualified" if profile == "source" else "with_reviewed"
    monkeypatch.setattr(reader, name, lambda *a, **k: touched.append(name))
    token = set_principal(Principal(**principal))
    try:
        with pytest.raises(PolicyError, match="recipient_relay_required"):
            lane.release.dispatch(envelope=message["payload"]["envelope"], payload=message["payload"]["intent"],
                                  request_id="read-0", send=lambda *_: pytest.fail("sent"))
    finally:
        reset_principal(token)
    assert touched == []


@pytest.mark.asyncio
async def test_F2_third_party_stamp_naming_the_owner_reaches_no_owner_only_handler(monkeypatch):
    import topos.core.handlers as hub
    from topos.core.handlers.registry import HANDLERS, OWNER_ONLY_MESSAGE_TYPES
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode())
    # Registered owner-only types plus the signal_* family the dispatcher gates by prefix.
    owner_only = sorted(OWNER_ONLY_MESSAGE_TYPES | {name for name in HANDLERS if name.startswith("signal_")})
    assert len(owner_only) > len(OWNER_ONLY_MESSAGE_TYPES) > 0
    for message_type in owner_only + ["permissions_v2_source_read", "permissions_v2_fact_read"]:
        # A token whose sub is the owner, used by a third-party client, is
        # stamped third_party by the CP; the verified principal must stay that.
        message = sign_stamp({"id": "owner-sub-1", "type": message_type, "payload": {"owner_id": "owner-1", "mode": "owner"}},
                             key, acting_user="owner-1", client_id="client-1")
        principal = verify_relay_stamp(message)
        assert principal == Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="client-1", acting_user="owner-1")
        response = await hub.handle_control_plane_request(message, principal=principal)
        assert response["status"] == "error" and response["code"] == 403, (message_type, response)
        assert "payload" not in response


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", sorted(PROFILES))
async def test_F2_owner_as_acting_user_gets_no_owner_reading_from_the_adapter(request, monkeypatch, profile):
    # The stamp names the owner, but the node holds no grant bound to the owner
    # as recipient: the read is a recipient read like any other and is refused.
    lane = open_lane(request, profile, monkeypatch)
    authority = lane.activate()
    message = lane.message(authority, "read-0", actor_id="owner-1")
    sent, _ = await lane.send(message)
    assert sent == [refusal("read-0", lane.message_type)]
    assert lane.runtime_reads and lane.reasons and lane.reasons[0] != "recipient_relay_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", sorted(PROFILES))
@pytest.mark.parametrize("kind", ["permitted"] + EVIDENCE + DOOR)
async def test_U1_every_refusal_class_emits_identical_frames(request, monkeypatch, profile, kind):
    lane = open_lane(request, profile, monkeypatch)
    if kind in DOOR:
        authority = lane.activate()
        messages = [door(lane, kind, lane.message(authority, f"read-{index}")) for index in range(RUNS)]
        if kind == "node_disabled":
            monkeypatch.setenv(lane.spec["flag"], "false")
    elif kind == "permitted":
        authority = lane.activate()
        messages = [lane.message(authority, f"read-{index}") for index in range(RUNS)]
    else:
        messages = prepare(lane, kind)
    durations = []
    for index, message in enumerate(messages):
        sent, elapsed = await lane.send(message)
        durations.append(elapsed)
        if kind == "permitted":
            assert len(sent) == 1 and '"status":"ok"' in sent[0]
        else:
            assert sent == [refusal(f"read-{index}", lane.message_type)], (kind, sent)
    if kind in EVIDENCE:
        # A real, class-specific refusal inside the adapter, never the door.
        assert lane.reasons == [REASONS[kind]] * RUNS
    elif kind in DOOR:
        assert lane.reasons == [] and lane.runtime_reads == []
    TIMINGS[(profile, kind)] = (lane.reasons[0] if lane.reasons else ("-" if kind == "permitted" else "door"), durations)


@pytest.fixture(scope="module", autouse=True)
def timing_report():
    yield
    if not TIMINGS:
        return
    print("\nnode refusal timing (ms, real dispatch incl. worker thread), runs=%d" % RUNS)
    for (profile, kind), (reason, durations) in sorted(TIMINGS.items()):
        ms = sorted(d * 1000 for d in durations)
        print(f"  {profile:6} {kind:30} {reason:32} median={statistics.median(ms):7.2f} min={ms[0]:7.2f} max={ms[-1]:7.2f}")
