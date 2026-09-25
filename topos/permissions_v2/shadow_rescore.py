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
import re
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


VERDICTS = ("agree", "candidate_miss", "unresolved")
# The control plane's reason grammar. A reason is a code: one outside the grammar is not filed and is not logged.
REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def assessed(labeler, records, policy) -> tuple:
    """The labeler's verdict and, when it is `unresolved`, its reason.

    A labeler that can say why (`assess`, as `shadow_labeler_local` does) is filed by its own code, so a host that
    could not be asked, a model nobody reviewed and a model that looked and abstained stop sharing one reason. A
    labeler that only scores is filed `labeler_unresolved`, as the seam always has.
    """
    assess = getattr(labeler, "assess", None)
    if callable(assess):
        outcome = assess(records, policy)
        return outcome.verdict, outcome.reason
    return labeler.score(records, policy), None


def filed_reason(reason) -> str:
    """The reason an unresolved row carries: the labeler's own code, or `labeler_unresolved`.

    A reason outside the grammar is dropped, not repaired and not logged: it could be anything, and the one thing
    this reply must never carry is text.
    """
    if isinstance(reason, str) and REASON.fullmatch(reason):
        return reason
    if reason is not None:
        logger.warning("permissions v2 shadow re-score: the labeler's reason is not a code; filed labeler_unresolved")
    return "labeler_unresolved"


LABELER_SETTING = "TOPOS_PERMISSIONS_V2_SHADOW_LABELER"
# The values this node's environment may name. Unset is off, which is the default everywhere.
LABELER_CHOICES = {"local": "shadow_labeler_local"}


def configured_labeler(mode: str):
    """The labeler this node's environment asks for, or None. Off unless it says otherwise.

    One setting, `TOPOS_PERMISSIONS_V2_SHADOW_LABELER`, beside the index's own switch, so a node re-scores only
    when its deployment says so. `local` binds the local rubric labeler; unset or empty binds nothing.

    A value that is neither is **not** quietly treated as off: it logs and stays off, because a typo in a
    deployment variable that silently disables an instrument is how a node ends up reporting holes for a week
    while somebody believes it is measuring.
    """
    import os
    value = (os.environ.get(LABELER_SETTING) or "").strip().lower()
    if not value:
        return None
    if value not in LABELER_CHOICES or mode != "local":
        if value not in LABELER_CHOICES:
            logger.warning("permissions v2 shadow audit: %s is set to a labeler this node does not have; "
                           "no second labeler is bound", LABELER_SETTING)
        return None
    from .shadow_labeler_local import LocalRubricLabeler
    return LocalRubricLabeler()


def resolve_labeler(mode: str):
    """The node's second labeler, or None when it has none configured.

    A seam, deliberately: which model a node re-scores with is the owner's choice and a deployment fact, not
    something this module should decide. `None` is the honest default -- a node that has not been given a second
    labeler answers `unresolved` / `labeler_unavailable` and the owner sees a hole rather than a number.

    An explicitly registered labeler wins over the setting, so a lab or a test can inject one without touching
    the environment; the setting is how an ordinary node gets one.

    A labeler is anything with `id`, `family`, and
    `score(records, policy) -> "agree" | "candidate_miss" | "unresolved"`. The policy is passed because the
    question "should this have been released" belongs to the policy, not to the model: a labeler re-derives the
    labels and the policy's own predicates decide, so a disagreement means the labels differed, never that a
    model was asked to interpret a rule.
    The hosted mode is reached only when the control plane has already checked the owner's standing consent; this
    function does not second-guess that, but a node that has no hosted labeler still answers `unresolved`.
    """
    from .shadow_labelers import registered
    return registered(mode) or configured_labeler(mode)


def primary_family_of(capability: str) -> str | None:
    """Which model family the node's OWN decision used for this capability.

    None for every capability this release serves: p2a and p2b decide by explicit scope and owner review, with no
    primary labeler in the path at all, so there is no family for a second opinion to differ from (design session,
    22 Sep). When a machine-label capability ships, this is where its family is named, and the control plane will
    refuse a same-family re-score as `same_family`.
    """
    return None


# Every hole `resolve_records` can meet, as the reply's reason. A bare `records_unavailable` (all 30 samples on the
# beta stack, 24 Sep) could not say whether the index, the key, the pointer or the row was missing, and the cause --
# pointers that never held the row's id -- hid behind it. Codes only, in the control plane's reason grammar
# (`^[a-z][a-z0-9_]{0,63}$`; anything else is filed `unspecified`), and each names this node's own state for its
# owner: none describes a record.
RECORDS_UNAVAILABLE_REASONS = tuple("records_unavailable_" + why for why in (
    "not_indexed",          # no index row for this request: the index was off, the row aged out, or the write failed
    "key_forgotten",        # the grant's record key is gone (a revoked grant forgets it, which is correct)
    "pointer_unopenable",   # the sealed pointer does not open under the grant's key
    "pointer_opaque",       # a pointer sealed before this fix: it holds the opaque id, never the row's
    "index_integrity",      # the pointer and its index row disagree on table or source
    "row_missing",          # no row answers to the released identity any more (a deleted message is a missing row)
    "row_ambiguous",        # two rows answer to one identity
    "lookup_failed",        # the canonical database could not be read
    "resolver_failed",      # anything else went wrong on the way (logged by exception class, never by text)
))


class RecordsUnavailable(Exception):
    """A release the node can no longer re-score, and which of the holes above it fell into."""

    def __init__(self, why: str):
        reason = "records_unavailable_" + why
        if reason not in RECORDS_UNAVAILABLE_REASONS:
            raise ValueError(reason)
        super().__init__(reason)
        self.reason = reason


def resolve_records(runtime, request: RescoreRequest) -> list[dict]:
    """The records this release named, with their content; `RecordsUnavailable` when the node can no longer say.

    A hole is not an error: a release whose index row aged out, whose grant's record key has been forgotten (a
    revoked grant forgets its key, which is correct), or which was never indexed because the index was off, is a
    read nobody can audit. It comes back `unresolved` with the hole's own reason, and `shadow_index.failures()`
    says how many of those this process caused itself.

    The content is read here, on the owner's node, under the owner's own authority, from the owner's own
    canonical database -- the same two tables and the same keying the evidence path reads (`evidence._load`: a
    conversation row by message, source and dataset). It goes to the labeler and nowhere else: no caller of this
    module puts a record on any wire.
    """
    import sqlite3

    from . import shadow_index
    from .opaque_ids import RecordKeys
    from .release import record_keys_root

    ledger = runtime.protocol.ledger
    with ledger._transaction() as conn:
        rows = shadow_index.released(conn, request_id=request.request_id)
    if not rows:
        raise RecordsUnavailable("not_indexed")
    key = RecordKeys(record_keys_root(runtime.protocol.canonical_database)).get(request.grant_id, create=False)
    if key is None:
        raise RecordsUnavailable("key_forgotten")
    pointers = []
    for row in rows:
        pointer = shadow_index.open_pointer(key, opaque_id=row["opaque_record_id"], sealed=row["sealed_pointer"])
        if not isinstance(pointer, dict):
            raise RecordsUnavailable("pointer_unopenable")
        if set(pointer) == {"record_id", "canonical_table"}:
            raise RecordsUnavailable("pointer_opaque")
        if (set(pointer) != shadow_index.POINTER_FIELDS
                or pointer["canonical_table"] not in ("conversation_messages", "ai_chat_messages")
                or (pointer["canonical_table"], pointer["source_id"]) != (row["canonical_table"], row["source_id"])):
            raise RecordsUnavailable("index_integrity")
        pointers.append(pointer)
    records = []
    try:
        canonical = sqlite3.connect(runtime.protocol.canonical_database.as_uri() + "?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        raise RecordsUnavailable("lookup_failed") from None
    canonical.row_factory = sqlite3.Row
    try:
        for pointer in pointers:
            table = pointer["canonical_table"]
            sql, args = f"SELECT * FROM {table} WHERE message_id=? AND source_id=?", [pointer["record_id"], pointer["source_id"]]
            if table == "conversation_messages":
                sql += " AND dataset_id=?"
                args.append(pointer["dataset_id"])
            try:
                found = canonical.execute(sql, args).fetchmany(2)
            except sqlite3.Error:
                raise RecordsUnavailable("lookup_failed") from None
            # The row is gone, or two rows answer to one identity. Either way this release can no longer be
            # re-scored against what it released, which is a hole with a name and not a verdict.
            if not found:
                raise RecordsUnavailable("row_missing")
            if len(found) > 1:
                raise RecordsUnavailable("row_ambiguous")
            records.append({"record_id": pointer["record_id"], "canonical_table": table,
                            "source_id": pointer["source_id"], "content": found[0]["content"]})
    finally:
        canonical.close()
    return records


def resolve_policy(runtime, request: RescoreRequest):
    """The signed policy this grant reads under, from the node's own ledger. None when it cannot be had."""
    try:
        with runtime.protocol.ledger._transaction() as conn:
            _authority, policy = runtime.protocol.ledger._authority(conn, request.grant_id, now_seconds())
        return policy
    except Exception:  # noqa: BLE001 -- an unreadable policy is a hole, not a verdict
        return None


def now_seconds() -> int:
    import time
    return int(time.time())


def rescore(runtime, raw_request) -> RescoreResult:
    """Answer one re-score question. Every path that cannot answer returns `unresolved` with its reason."""
    request = RescoreRequest.parse(raw_request)
    labeler = resolve_labeler(request.labeler_mode)
    if labeler is None:
        return unresolved(request, "labeler_unavailable")
    try:
        records = resolve_records(runtime, request)
    except RecordsUnavailable as hole:
        # Logged, because the hole that hid the p2a-v3 pointer bug returned None and logged nothing. A code, never
        # an id: the log is the operator's, and the reason already names the node's own state and nothing else.
        logger.info("permissions v2 shadow re-score: unresolved, %s", hole.reason)
        return unresolved(request, hole.reason, labeler=getattr(labeler, "id", "none"))
    except Exception as exc:  # noqa: BLE001 -- a node that cannot look is a hole, not an error the CP should see
        logger.warning("permissions v2 shadow re-score: the released records could not be resolved (%s)",
                       type(exc).__name__)
        return unresolved(request, "records_unavailable_resolver_failed", labeler=getattr(labeler, "id", "none"))
    if not records:
        return unresolved(request, "records_unavailable", labeler=getattr(labeler, "id", "none"))
    primary = primary_family_of(request.capability)
    family = getattr(labeler, "family", None)
    if primary is not None and family == primary:
        # The control plane refuses this too; refusing it here as well means a node that is misconfigured cannot
        # produce a same-family agreement even against an older control plane.
        return unresolved(request, "same_family", labeler=getattr(labeler, "id", "none"))
    policy = resolve_policy(runtime, request)
    if policy is None:
        return unresolved(request, "policy_unavailable", labeler=getattr(labeler, "id", "none"))
    try:
        verdict, reason = assessed(labeler, records, policy)
    except Exception:  # noqa: BLE001
        logger.warning("permissions v2 shadow re-score: the second labeler failed")
        return unresolved(request, "labeler_failed", labeler=getattr(labeler, "id", "none"))
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        return unresolved(request, "labeler_verdict_invalid", labeler=getattr(labeler, "id", "none"))
    if verdict != "unresolved":
        reason = None
    else:
        reason = filed_reason(reason)
        # The code and nothing else. The log is the operator's, and it is the tell that separates a labeler that
        # never ran from one that looked and abstained; an id or a record has no business in it.
        if reason == "labeler_unresolved":
            logger.info("permissions v2 shadow re-score: unresolved, %s", reason)
        else:
            logger.warning("permissions v2 shadow re-score: unresolved, %s", reason)
    return RescoreResult(request_id=request.request_id, verdict=verdict, labeler=getattr(labeler, "id", "none"),
                         family=family, primary_family=primary, output_sha256=request.output_sha256, reason=reason)
