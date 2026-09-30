"""Writer class: which door wrote a canonical row, recorded when it is written.

``sender_type`` says who SPOKE in a chat row. It says nothing about who WROTE
the row into this node, and until 2026-09 the role gate read it as though it
did: any caller holding a write grant could send ``sender_type`` 'human' (or no
role at all, which defaulted to 'human') and fact extraction asserted the text
as the owner's own words.

The writer class is taken from the channel-verified principal
(``topos/principal.py``) at the moment of the write — never from the payload.
Only an owner class licenses the owner's authorship:

- ``owner_app``     — the owner's own surface: the 0600 socket, or a relay
                      message carrying a verified ``owner_app`` stamp (the CP
                      mints one for the owner's own requests and for the
                      owner's attested capture apps, e.g. the ChatGPT extension).
- ``owner_import``  — a file import started by one of those surfaces.
- ``local_legacy``  — the local HTTP door on a node with no owner key
                      configured, where the shared key is the only credential
                      (the principal module's install-flow invariant).

Every other class is recorded and can never be the owner's speech:

- ``cp_relay``          — an unstamped relay message: the CP did not name the
                          caller, so a UMA grantee and the owner look alike.
- ``third_party``       — a stamped third party, the shared key once an owner
                          key exists, an enrolled ``tpk_`` client, or the owner
                          key over TCP (TCP demotion).
- ``owner_automation``  — the routine lane: the owner's automation, not the
                          owner typing.

A row with NO writer class (NULL) predates this column or came from an internal
path (reprocess, upgrade replay) that has no door. It keeps the behaviour it had
before, which is why :func:`is_owner_writer` answers True for it. Those rows are
not re-classified retroactively; the ones a grantee wrote before this shipped
are indistinguishable from the owner's and stay as they are.

Pure at import time: stdlib only. :func:`current_writer_class` imports the
principal contextvar lazily, and that module is stdlib-only too.
"""

from __future__ import annotations

from typing import Any, Optional

WRITER_OWNER_APP = "owner_app"
WRITER_OWNER_IMPORT = "owner_import"
WRITER_LOCAL_LEGACY = "local_legacy"
WRITER_CP_RELAY = "cp_relay"
WRITER_THIRD_PARTY = "third_party"
WRITER_OWNER_AUTOMATION = "owner_automation"

OWNER_WRITER_CLASSES = frozenset({WRITER_OWNER_APP, WRITER_OWNER_IMPORT, WRITER_LOCAL_LEGACY})

#: Passed by a caller that has no door and must NOT fall back to the ambient
#: principal — a queued job runs in a worker task that inherited the context of
#: whichever request first started the worker. Stored as NULL.
WRITER_UNRECORDED = ""

# Principal classes (topos/principal.py, topos/relay_stamp.py), duplicated so
# this module stays importable from the pure roles module.
_PRINCIPAL_OWNER_APP = "owner_app"
_PRINCIPAL_CP_RELAY = "cp_relay"
_PRINCIPAL_OWNER_AUTOMATION = "owner_automation"


def normalize_writer_class(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text or None


def is_owner_writer(writer_class: Any) -> bool:
    """True when the writer may author the owner's speech.

    NULL is the legacy answer (see the module docstring), not a trust decision
    anyone makes at write time: every door that has a principal records one.
    """
    normalized = normalize_writer_class(writer_class)
    return normalized is None or normalized in OWNER_WRITER_CLASSES


def writer_class_for_principal(principal: Any, *, owner_class: str = WRITER_OWNER_APP) -> str:
    """The writer class a door records for the channel-verified principal.

    ``principal`` is a ``topos.principal.Principal`` or None. None is only ever
    the local HTTP door in legacy mode (no owner key) or an in-process caller
    with no channel; the relay always supplies a principal. ``owner_class``
    lets an import door record ``owner_import`` for the owner's own surface.
    """
    if principal is None:
        return WRITER_LOCAL_LEGACY
    cls = str(getattr(principal, "cls", "") or "").strip().lower()
    if cls == _PRINCIPAL_OWNER_APP:
        return owner_class
    if cls == _PRINCIPAL_CP_RELAY:
        return WRITER_CP_RELAY
    if cls == _PRINCIPAL_OWNER_AUTOMATION:
        return WRITER_OWNER_AUTOMATION
    return WRITER_THIRD_PARTY


def writer_app_for_principal(principal: Any) -> Optional[str]:
    """The capture app an ``owner_app`` relay write came through, or None.

    Only a verified relay stamp names an app: the CP signs ``client_id`` into
    the stamp, and under rule C (``control_plane/owner_write_stamp.py``) it
    stamps ``app_ingest`` only for the owner's own attested capture apps. A
    socket write, an unstamped relay write and every non-owner class name none,
    so no caller can claim an app by sending one.
    """
    if principal is None:
        return None
    cls = str(getattr(principal, "cls", "") or "").strip().lower()
    channel = str(getattr(principal, "channel", "") or "").strip().lower()
    app = str(getattr(principal, "client_id", "") or "").strip()
    if cls != _PRINCIPAL_OWNER_APP or channel != _PRINCIPAL_CP_RELAY or not app:
        return None
    return app


def current_writer_app_id() -> Optional[str]:
    """Capture app for the principal the dispatcher scoped onto this context."""
    from ...principal import current_principal

    return writer_app_for_principal(current_principal())


def current_writer_class(*, owner_class: str = WRITER_OWNER_APP) -> str:
    """Writer class for the principal the dispatcher scoped onto this context."""
    from ...principal import current_principal

    return writer_class_for_principal(current_principal(), owner_class=owner_class)
