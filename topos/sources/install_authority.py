"""Who may install or replace a source definition on this node.

A source definition decides how every later record of that source is parsed,
which canonical table it lands in, what its fields map to and its posture — and
through the table and the posture, whether its text reads as the owner's own
(``journal_entries`` rows are authored by construction). Installing one writes
no rows. It changes how the owner's data is read from then on, and ``REGISTRY``
is process-wide.

Until 2026-09 ``start_ingestion`` put ``payload["source_definition"]`` into the
queued job and the import worker installed it, so any relay sender could
redefine a bundled source for every later import until the process restarted.
The install doors (relay ``post_source_install`` / ``patch_source_install``,
HTTP ``/v1/source-install``) installed for any authenticated caller, and those
installs persist and rehydrate at every boot.

Two rules:

- **An ingest payload never replaces a bundled source.** The CP reads the
  definition it sends back from this node's own install rows (``get_sources``)
  or from its mirror of the bundled registry, so for a bundled source the copy
  in the payload was never the authority: the engine's definition is. For a
  source the engine does not bundle, the payload installs only when the import
  is the owner's (its writer class is an owner class).
- **Installing or replacing a definition takes the owner:** the 0600 socket, or
  a relay message carrying a verified ``owner_app`` stamp. No principal at all
  (local HTTP on a node with no owner key, or an in-process caller) is the
  install-flow legacy of ``topos/principal.py`` and passes.

**A node with no pinned CP stamp key refuses unstamped relay installs too.**
Nothing on its relay can prove the owner, and a definition outlives the request
that installed it. The owner installs over the socket, or once the node has
pinned the key (it autopins at boot when the CP publishes one). This is the same
answer ``owner_only`` gives on beta/permissions-v2, which refuses ``cp_relay``
whether or not a key is pinned.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

OWNER_MODE_REQUIRED = "owner_mode_required"


def principal_may_install(principal: Any) -> bool:
    """True for the owner's socket or a verified owner stamp, and for legacy (None)."""
    if principal is None:
        return True
    from ..principal import OWNER_APP

    return str(getattr(principal, "cls", "") or "") == OWNER_APP


def install_refusal(req_id: Any) -> Optional[Dict[str, Any]]:
    """The relay error for a caller that may not install, or None when it may."""
    from ..principal import current_principal

    if principal_may_install(current_principal()):
        return None
    return {"id": req_id, "status": "error", "code": 403, "error": OWNER_MODE_REQUIRED}


def ingest_definition_to_install(source_definition: Any, *, writer_class: Optional[str]) -> Optional[Dict[str, Any]]:
    """The definition an import may install before it runs, or None.

    ``writer_class`` is the class the import's door recorded
    (``features/provenance/writer_class.py``). A job queued before writer
    classes existed has none, and installs nothing.
    """
    if not isinstance(source_definition, dict) or not source_definition:
        return None
    from ..features.provenance.writer_class import OWNER_WRITER_CLASSES, normalize_writer_class
    from .registry import BUNDLED_REGISTRY

    source_id = str(source_definition.get("source_id") or "").strip()
    if source_id in BUNDLED_REGISTRY:
        logger.debug("[SOURCE_INSTALL] ingest payload definition ignored: %s is bundled", source_id)
        return None
    if normalize_writer_class(writer_class) not in OWNER_WRITER_CLASSES:
        logger.info(
            "[SOURCE_INSTALL] ingest payload definition not installed: source_id=%s writer_class=%s is not the owner",
            source_id,
            writer_class or None,
        )
        return None
    return source_definition
