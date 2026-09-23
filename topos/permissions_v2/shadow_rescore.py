"""Re-scoring one permitted read on the node, for the owner's shadow audit (confidence program C6).

The control plane samples permitted reads and asks, later and out of band, whether each one should really have been
released. The question has to be answered here: the sample row carries no content by design, and the content is the
owner's, on the owner's machine. Only a verdict goes back.

  agree           the second labeler would also have released this
  candidate_miss  it would not have, so the owner should look
  unresolved      nobody can say, and the reason why

**The default answer is `unresolved`, never `agree`.** A node with no second labeler configured, a release whose
records it can no longer resolve, a pointer that will not open: each is a hole with a name. Answering `agree`
because nothing objected would manufacture evidence -- the report card's whole output is a count of items that were
checked, and an item nobody checked must not be in it. This is the single property of this module that matters
most; `test_E5` pins it.

**The reply carries a verdict, never a rationale.** There is no field for the labeler's text. A rationale is a
summary of the owner's own records, and the control plane keeps this reply in a row that a route reads back.

**It runs under the owner's own authority.** The handler is `owner_only`, reached over the relay only with an owner
principal whose acting user is this node's owner, and it reads the owner's rows directly, never through a
recipient path.
"""
from __future__ import annotations

import logging
from typing import Literal

from .canonical import PolicyError
from .contract import Hash, Identifier, StrictModel

logger = logging.getLogger(__name__)

VERSION = "topos-permissions-shadow-rescore/v1"
MESSAGE_TYPE = "permissions_v2_shadow_rescore"


class RescoreRequest(StrictModel):
    """The control plane's question. An identity; there is nothing else it could usefully send."""

    version: Literal["topos-permissions-shadow-rescore/v1"] = VERSION
    request_id: Identifier
    grant_id: Identifier
    capability: Identifier
    output_sha256: Hash
    labeler_mode: Literal["local", "hosted"]


class RescoreResult(StrictModel):
    """The node's answer. A verdict and its provenance; no records, no text, no rationale."""

    version: Literal["topos-permissions-shadow-rescore/v1"] = VERSION
    request_id: Identifier
    verdict: Literal["agree", "candidate_miss", "unresolved"]
    labeler: Identifier
    family: Identifier | None
    primary_family: Identifier | None
    output_sha256: Hash
    reason: Identifier | None


def unresolved(request: RescoreRequest, reason: str, *, labeler: str = "none") -> RescoreResult:
    return RescoreResult(request_id=request.request_id, verdict="unresolved", labeler=labeler, family=None,
                         primary_family=None, output_sha256=request.output_sha256, reason=reason)


def resolve_labeler(mode: str):
    """The node's second labeler, or None when it has none configured.

    A seam, deliberately: which model a node re-scores with is the owner's choice and a deployment fact, not
    something this module should decide. `None` is the honest default -- a node that has not been given a second
    labeler answers `unresolved` / `labeler_unavailable` and the owner sees a hole rather than a number.

    A labeler is anything with `id`, `family`, and `score(records) -> "agree" | "candidate_miss" | "unresolved"`.
    The hosted mode is reached only when the control plane has already checked the owner's standing consent; this
    function does not second-guess that, but a node that has no hosted labeler still answers `unresolved`.
    """
    from .shadow_labelers import registered
    return registered(mode)


def primary_family_of(capability: str) -> str | None:
    """Which model family the node's OWN decision used for this capability.

    None for every capability this release serves: p2a and p2b decide by explicit scope and owner review, with no
    primary labeler in the path at all, so there is no family for a second opinion to differ from (design session,
    22 Sep). When a machine-label capability ships, this is where its family is named, and the control plane will
    refuse a same-family re-score as `same_family`.
    """
    return None


def resolve_records(runtime, request: RescoreRequest) -> list[dict] | None:
    """The records this release named, with their content, or None when the node can no longer say.

    None is not an error: a release whose index row aged out, whose grant's record key has been forgotten (a
    revoked grant forgets its key, which is correct), or which was never indexed because the index was off, is a
    read nobody can audit. It comes back `records_unavailable`, and `shadow_index.failures()` says how many of
    those this process caused itself.
    """
    from . import shadow_index
    from .opaque_ids import RecordKeys
    from .release import record_keys_root

    ledger = runtime.protocol.ledger
    with ledger._transaction() as conn:
        rows = shadow_index.released(conn, request_id=request.request_id)
    if not rows:
        return None
    key = RecordKeys(record_keys_root(runtime.protocol.canonical_database)).get(request.grant_id, create=False)
    if key is None:
        return None
    records = []
    for row in rows:
        pointer = shadow_index.open_pointer(key, opaque_id=row["opaque_record_id"], sealed=row["sealed_pointer"])
        if pointer is None:
            return None
        records.append({"record_id": pointer["record_id"], "canonical_table": pointer["canonical_table"],
                        "source_id": row["source_id"]})
    return records


def rescore(runtime, raw_request) -> RescoreResult:
    """Answer one re-score question. Every path that cannot answer returns `unresolved` with its reason."""
    request = RescoreRequest.parse(raw_request)
    labeler = resolve_labeler(request.labeler_mode)
    if labeler is None:
        return unresolved(request, "labeler_unavailable")
    try:
        records = resolve_records(runtime, request)
    except Exception:  # noqa: BLE001 -- a node that cannot look is a hole, not an error the CP should see
        logger.warning("permissions v2 shadow re-score: the released records could not be resolved")
        records = None
    if not records:
        return unresolved(request, "records_unavailable", labeler=getattr(labeler, "id", "none"))
    primary = primary_family_of(request.capability)
    family = getattr(labeler, "family", None)
    if primary is not None and family == primary:
        # The control plane refuses this too; refusing it here as well means a node that is misconfigured cannot
        # produce a same-family agreement even against an older control plane.
        return unresolved(request, "same_family", labeler=getattr(labeler, "id", "none"))
    try:
        verdict = labeler.score(records)
    except Exception:  # noqa: BLE001
        logger.warning("permissions v2 shadow re-score: the second labeler failed")
        return unresolved(request, "labeler_failed", labeler=getattr(labeler, "id", "none"))
    if verdict not in {"agree", "candidate_miss", "unresolved"}:
        return unresolved(request, "labeler_verdict_invalid", labeler=getattr(labeler, "id", "none"))
    return RescoreResult(request_id=request.request_id, verdict=verdict, labeler=getattr(labeler, "id", "none"),
                         family=family, primary_family=primary, output_sha256=request.output_sha256,
                         reason=None if verdict != "unresolved" else "labeler_unresolved")
