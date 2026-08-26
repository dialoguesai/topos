"""Channel-verified request principal — WHO is asking, as a client class.

Identity (whose data, which uuid) and principal (which kind of client) are
different facts, and conflating them is how special-class fact content nearly
reached third-party connectors on 2026-08-26: every OAuth client carries the
owner's sub, so identity alone cannot distinguish the owner's own surface from
ChatGPT wielding the owner's token. The principal is established at the channel
door — which credential authenticated, on which transport — and is never read
from a payload. Payload identity fields remain audit data only.

P1 (this module): two credentials on the HTTP door. TOPOS_OWNER_KEY, held only
by first-party surfaces, resolves to OWNER_APP; the legacy shared TOPOS_KEY
resolves to THIRD_PARTY. Relay messages carry CP_RELAY — the CP applies its own
client-class policy before forwarding (owner ids are stamped for Topos-native
clients only), so the engine keeps honoring the forwarded-id equality test on
that channel until P3 replaces it with signed per-frame principal stamps.

Migration invariant (install flow): while TOPOS_OWNER_KEY is unconfigured, the
HTTP door resolves to None ("legacy") and every consumer behaves byte-for-byte
as before — an upgraded engine must never demote the owner's own app before the
app has learned the new key. Enforcement activates when the key exists.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Optional

#: Client classes. OWNER_APP and THIRD_PARTY are decisions; CP_RELAY is a
#: deferral — "the CP already classified this caller, trust its stamping".
OWNER_APP = "owner_app"
THIRD_PARTY = "third_party"
CP_RELAY = "cp_relay"


@dataclass(frozen=True)
class Principal:
    cls: str
    channel: str  # "local_http" | "cp_relay" | "internal"
    client_id: str = ""
    acting_user: str = ""


RELAY_PRINCIPAL = Principal(cls=CP_RELAY, channel="cp_relay")

_current: contextvars.ContextVar[Optional[Principal]] = contextvars.ContextVar(
    "topos_request_principal", default=None
)


def current_principal() -> Optional[Principal]:
    return _current.get()


def set_principal(principal: Optional[Principal]) -> contextvars.Token:
    """Set for the current context; caller must reset with the returned token."""
    return _current.set(principal)


def reset_principal(token: contextvars.Token) -> None:
    _current.reset(token)
