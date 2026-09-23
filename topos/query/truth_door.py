"""Truth door — who may call verify_claim, truth_prompts and truth_seed_fact.

The fun-mode aperture (verify_modes.py) bounds WHAT these three types can touch;
this module bounds WHO may ask. The answer comes from the channel-verified
principal (topos/principal.py), never from the payload: ``caller_app_id`` is
whatever the caller sent, so it names an app for the audit line and gates
nothing.

- truth_seed_fact WRITES an owner-stated fact (``asserted_by="owner"``), so only
  the owner class may author one: OWNER_APP, which exists on the 0600 owner
  socket and under a verified relay stamp — never behind a bearer on TCP.
- verify_claim and truth_prompts READ the owner's fun facts. Besides the owner
  class they admit:
    * CP_RELAY — the CP truth door (``/v1/truth/*``) checks an owner credential
      and its TRUTH_APP_ALLOWLIST before relaying, and relays unstamped. That is
      the production lane.
    * an enrolled client (``tpk_<client_id>``) on the local door whose client_id
      the owner lists in TOPOS_TRUTH_CLIENT_ALLOWLIST — the local twin of the
      CP's list. Unset or empty admits no client.
- Everything else gets the dispatcher's uniform ``owner_mode_required``: the
  shared TOPOS_KEY (third party, or no principal at all in legacy mode), the
  owner key presented over TCP (demoted to third party by design), any other
  enrolled client, relay-stamped third parties, and the routine executor.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, FrozenSet, Optional

from ..principal import CP_RELAY, OWNER_APP, THIRD_PARTY

logger = logging.getLogger(__name__)

OWNER_MODE_REQUIRED = "owner_mode_required"

#: Comma-separated enrolled client ids, read per call like the other fabric
#: switches (TOPOS_CP_STAMP_PUBKEY, TOPOS_UDS_PATH).
ALLOWLIST_ENV = "TOPOS_TRUTH_CLIENT_ALLOWLIST"

#: The only truth types a non-owner lane may reach. The write, and any truth
#: type added later without a decision here, stays owner-class only.
_READ_TYPES = frozenset({"verify_claim", "truth_prompts"})


def truth_client_allowlist() -> FrozenSet[str]:
    raw = os.environ.get(ALLOWLIST_ENV, "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def truth_door_admits(principal: Optional[Any], msg_type: str) -> bool:
    cls = getattr(principal, "cls", None)
    if cls == OWNER_APP:
        return True
    if msg_type not in _READ_TYPES:
        return False
    if cls == CP_RELAY:
        return True
    if cls == THIRD_PARTY and getattr(principal, "channel", "") == "local_http":
        # Named clients only: the shared key and the TCP-demoted owner key
        # resolve with no client_id, so no list entry can admit them.
        client_id = str(getattr(principal, "client_id", "") or "")
        return bool(client_id) and client_id in truth_client_allowlist()
    return False


def truth_door_refusal(
    principal: Optional[Any], msg_type: str, req_id: Any = None
) -> Optional[Dict[str, Any]]:
    """None when ``principal`` may proceed, else the dispatcher's 403 shape."""
    if truth_door_admits(principal, msg_type):
        return None
    logger.info(
        "truth door refused type=%s cls=%s channel=%s client=%s",
        msg_type,
        getattr(principal, "cls", None),
        getattr(principal, "channel", None),
        getattr(principal, "client_id", "") or "-",
    )
    return {"id": req_id, "status": "error", "code": 403, "error": OWNER_MODE_REQUIRED}
