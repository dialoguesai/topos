"""Recipient reads at the node: the owner-only handlers and the generic dispatch never answer a recipient.

The control plane relays a recipient's read as one of four message types (search, its batch form, answer submit
and answer fetch); each is answered only by its own bounded transport. Held here, through the real handler hub:

  F2  a third_party stamp naming the OWNER as acting user stays third_party: it reaches no owner-only handler,
      and the generic dispatch returns the one refusal for every recipient type, never a payload

The locator and fact doors carried the rest of this suite (their principal door F1 and their frame uniformity
U1) until N8 removed them with their transports. The search door's own versions of both live in
`test_message_search_refusals.py`; each evidence refusal those cases named is asserted where its check lives
(`test_evidence.py`, `test_evidence_reviews.py`, `test_closed_fact_floor.py`, `test_source_release_*.py`).
`refusal` and `sign_stamp` stay here for the transport fuzz lane.
"""
import base64
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from topos.permissions_v2.canonical import canonical_bytes
from topos.principal import THIRD_PARTY, Principal
from topos.relay_stamp import canonical_signing_payload, verify_relay_stamp

#: Every message type a recipient's read can arrive as.
RECIPIENT_TYPES = ["permissions_v2_message_search", "permissions_v2_message_search_batch",
                   "permissions_v2_answer_submit", "permissions_v2_answer_fetch"]


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


@pytest.mark.asyncio
async def test_F2_third_party_stamp_naming_the_owner_reaches_no_owner_only_handler(monkeypatch):
    import topos.core.handlers as hub
    from topos.core.handlers.registry import HANDLERS, OWNER_ONLY_MESSAGE_TYPES
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode())
    # Registered owner-only types plus the signal_* family the dispatcher gates by prefix.
    owner_only = sorted(OWNER_ONLY_MESSAGE_TYPES | {name for name in HANDLERS if name.startswith("signal_")})
    assert len(owner_only) > len(OWNER_ONLY_MESSAGE_TYPES) > 0
    for message_type in owner_only + RECIPIENT_TYPES:
        # A token whose sub is the owner, used by a third-party client, is
        # stamped third_party by the CP; the verified principal must stay that.
        message = sign_stamp({"id": "owner-sub-1", "type": message_type, "payload": {"owner_id": "owner-1", "mode": "owner"}},
                             key, acting_user="owner-1", client_id="client-1")
        principal = verify_relay_stamp(message)
        assert principal == Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="client-1", acting_user="owner-1")
        response = await hub.handle_control_plane_request(message, principal=principal)
        assert response["status"] == "error" and response["code"] == 403, (message_type, response)
        assert "payload" not in response
