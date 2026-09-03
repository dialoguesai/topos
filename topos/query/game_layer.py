"""Game layer reveal strategies (PRD §8.6)."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List

from .types import FORBIDDEN_INFERENCE_PUBLIC_KEYS, PublicResult


class RevealStrategy(str, Enum):
    DIRECT = "direct"
    PROGRESSIVE = "progressive"
    SUMMARY_FIRST = "summary_first"
    INFERENCE_GATED = "inference_gated"
    OWNER_CONFIRM = "owner_confirm"
    MINIMUM_DISCLOSURE = "minimum_disclosure"


_LIST_QUERY_RE = re.compile(r"\b(who|whom|people|person|collaborate|collaborators|contacts)\b", re.I)
_WHO_QUERY_RE = re.compile(r"\bwho\b", re.I)
_WHAT_QUERY_RE = re.compile(r"\bwhat\b", re.I)


def _coerce_confidence(*candidates: Any) -> float:
    """First candidate that parses as a float, else 0.0 — never raises."""
    for value in candidates:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _is_list_query(query_text: str) -> bool:
    return bool(_LIST_QUERY_RE.search(query_text or ""))


def _extract_inference_evidence(context_packet: Dict[str, Any], *, limit: int = 5) -> List[str]:
    evidence: List[str] = []
    seen: set[str] = set()
    for score in context_packet.get("scores") or []:
        if not isinstance(score, dict):
            continue
        for key in ("summary_text", "topic", "entity_text", "label"):
            value = score.get(key)
            if value and str(value) not in seen:
                seen.add(str(value))
                evidence.append(str(value))
                break
        if len(evidence) >= limit:
            return evidence
    for hit in context_packet.get("semantic_hits") or []:
        if not isinstance(hit, dict):
            continue
        for key in ("content_preview", "title", "text"):
            value = hit.get(key)
            if value and str(value) not in seen:
                seen.add(str(value))
                evidence.append(str(value))
                break
        if len(evidence) >= limit:
            break
    return evidence


#: Redaction writes these placeholders into stored text, and the NER pass then
#: extracts them as if they were names.
_REDACTION_PLACEHOLDERS = {"NAME", "ADDRESS", "DATE", "EMAIL", "PHONE", "URL", "LOCATION"}


def _looks_like_entity_label(value: str) -> bool:
    """Reject debris that is not a name.

    `graph.nodes` and `scores` carry raw NER output, which includes WordPiece
    continuation tokens ("Topos" tokenizes to "Top" + "##os", and both are stored
    as separate entities), redaction placeholders, and serialized fact blobs. Live
    2026-08-26 the answer to "Who's in my close circle?" was
    ["U", "AI", "UMA", "Sara", "Marcus", "NAME", "Top", "##os", "VoxTerm A"].
    """
    v = value.strip()
    if len(v) < 2 or len(v) > 60:
        return False
    if v.startswith("##"):          # WordPiece continuation, never a standalone name
        return False
    if "{" in v or "}" in v:        # serialized fact payload, not a label
        return False
    if v.upper() in _REDACTION_PLACEHOLDERS:
        return False
    if v.isupper() and len(v) <= 3:  # bare acronym (U, AI, UMA), not a person here
        return False
    return True


def _extract_entity_labels(context_packet: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    seen: set[str] = set()
    graph = context_packet.get("graph") or {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        for key in ("label", "name", "node_id", "entity_text"):
            value = node.get(key)
            if value and str(value) not in seen and _looks_like_entity_label(str(value)):
                seen.add(str(value))
                labels.append(str(value))
    for score in context_packet.get("scores") or []:
        if not isinstance(score, dict):
            continue
        for key in ("entity_text", "label", "topic", "summary_text"):
            value = score.get(key)
            if value and str(value) not in seen and _looks_like_entity_label(str(value)):
                seen.add(str(value))
                labels.append(str(value))
    return labels[:10]


#: availability:read licenses free/busy ONLY. The three sets below are the
#: ENTIRE licensed vocabulary of an availability inference answer — a person
#: name or a meeting title has no slot in it, so enforcement is a closed-set
#: membership check, never a redaction pattern that has to recognize names.
_AVAILABILITY_ANSWERS = frozenset({"yes", "conditional", "no", "unknown"})
_AVAILABILITY_BANDS = frozenset(
    {"overlap_found", "negotiable_overlap", "no_overlap", "unknown"}
)
#: Free text can only leave through a key, so unknown keys are dropped rather
#: than inspected. Everything listed is either closed-set or pipeline-stamped
#: metadata (exclusion/truncated/empty_cause carry slugs and integers only).
_AVAILABILITY_ALLOWED_KEYS = frozenset(
    {
        "access_mode",
        "scope_id",
        "answer_type",
        "band",
        "answer",
        "confidence",
        "items",
        "deferred",
        "redaction",
        "exclusion",
        "truncated",
        "empty_cause",
        "packet_resolution",
        "packet_resolution_reason",
        "principal_cls",
    }
)


def is_availability_scope(scope_id: str) -> bool:
    return "availability" in (scope_id or "")


def enforce_availability_inference_contract(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Free/busy-only egress gate for availability inference payloads.

    The branches in ``DefaultGameLayer.apply`` already emit only closed-set
    values for this scope — but the pipeline lanes that run AFTER the game
    layer ``update()`` the payload in place, and any of them can re-open what
    the game layer refused. Live 2026-09-02 the LLM inference lane did exactly
    that: the who-guard's ``answer: "unknown"`` left the engine as a contact's
    full name at confidence 1. This function is the last owner of the payload
    before it is serialized, so a violation here is scrubbed to "unknown"
    rather than trusted, and the scrub is declared via ``redaction`` (a
    closed-set slug) instead of happening silently.
    """
    out = {k: v for k, v in payload.items() if k in _AVAILABILITY_ALLOWED_KEYS}
    scrubbed = len(out) != len(payload)
    if out.get("answer_type") not in ("band", "list"):
        out["answer_type"] = "band"
        scrubbed = True
    if str(out.get("band") or "unknown") not in _AVAILABILITY_BANDS:
        out.pop("band", None)
        scrubbed = True
    if str(out.get("answer")) not in _AVAILABILITY_ANSWERS:
        out["answer"] = "unknown"
        out["confidence"] = 0.0
        scrubbed = True
    if out.get("items"):
        out["items"] = []
        scrubbed = True
    if scrubbed:
        out["redaction"] = "availability_free_busy_only"
    return out


class DefaultGameLayer:
    reveal_strategy: RevealStrategy = RevealStrategy.DIRECT

    def select_strategy(self, access_mode: str, scope_id: str) -> RevealStrategy:
        if access_mode == "inference":
            return RevealStrategy.INFERENCE_GATED
        if access_mode == "summary":
            return RevealStrategy.SUMMARY_FIRST
        if "availability" in scope_id:
            return RevealStrategy.MINIMUM_DISCLOSURE
        return RevealStrategy.DIRECT

    def apply(
        self,
        *,
        context_packet: Dict[str, Any],
        access_mode: str,
        scope_id: str = "",
        query_text: str = "",
    ) -> PublicResult:
        strategy = self.select_strategy(access_mode, scope_id)
        payload: Dict[str, Any] = {"access_mode": access_mode, "scope_id": scope_id}
        q = str(query_text or "").strip()

        if access_mode == "inference":
            if "availability" in scope_id and (_is_list_query(q) or _WHO_QUERY_RE.search(q)):
                payload.update(
                    {
                        "answer_type": "list",
                        "items": [],
                        "answer": "unknown",
                        "confidence": 0.0,
                    }
                )
            elif "availability" in scope_id:
                # Minimum disclosure: ONE band, one confidence — the requester
                # learns whether the window works (yes / conditional / no),
                # never the schedule bundle. Band computed store-side
                # (retrieval._availability_band); absent band ⇒ honest unknown.
                band_info = context_packet.get("availability_band") or {}
                band = str(band_info.get("band") or "unknown")
                payload.update(
                    {
                        "answer_type": "band",
                        "band": band,
                        "answer": {
                            "overlap_found": "yes",
                            "negotiable_overlap": "conditional",
                            "no_overlap": "no",
                        }.get(band, "unknown"),
                        "confidence": float(band_info.get("confidence") or 0.0),
                    }
                )
            elif _is_list_query(q) or (
                "relationship" in scope_id and (_WHO_QUERY_RE.search(q) or _WHAT_QUERY_RE.search(q))
            ):
                items = _extract_entity_labels(context_packet)
                payload.update(
                    {
                        "answer_type": "list",
                        "items": items,
                        "answer": "list" if items else "unknown",
                        "confidence": 0.7 if items else 0.0,
                    }
                )
            else:
                scores = context_packet.get("scores") or []
                semantic = context_packet.get("semantic_hits") or []
                yes_no = bool(scores or semantic)
                confidence = 0.0
                if semantic and isinstance(semantic[0], dict):
                    confidence = _coerce_confidence(semantic[0].get("similarity"))
                elif scores and isinstance(scores[0], dict):
                    # A score item's "value" is not always numeric: fact items
                    # carry value_struct dicts (live 2026-09-03 a
                    # mind.self_reported_state JSON crashed the whole turn
                    # here). Confidence is best-effort telemetry — never let
                    # its coercion cost the answer.
                    confidence = _coerce_confidence(
                        scores[0].get("value"), scores[0].get("confidence")
                    )
                evidence = _extract_inference_evidence(context_packet)
                payload.update(
                    {
                        "answer_type": "yes_no",
                        "answer": "yes" if yes_no else "no",
                        "confidence": confidence,
                    }
                )
                if scope_id == "activity:read" and evidence:
                    payload["items"] = evidence
            for forbidden in FORBIDDEN_INFERENCE_PUBLIC_KEYS:
                payload.pop(forbidden, None)
        elif access_mode == "summary":
            payload["answer_type"] = "summary"
            payload["summaries"] = context_packet.get("summaries") or []
            # Planner-parsed date window (present only when retrieval honored
            # one) — lets synthesis state the searched range explicitly.
            if context_packet.get("time_window"):
                payload["time_window"] = context_packet["time_window"]
            # Q7: the topic thread — the same summary rows, said as ONE ordered
            # conversation with a participant set and its decision points. It
            # carries record ids, timestamps and closed-set labels that point AT
            # `summaries`; it never duplicates their text, and every id in it is
            # already in the list beside it. Without this line the assembly is
            # unreachable: retrieval would compute an ordering that synthesis
            # never sees and the owner is handed a ranked list again.
            if context_packet.get("topic_thread"):
                payload["topic_thread"] = context_packet["topic_thread"]
            # Q1: the per-goal commitment report. Same projection rule as the thread —
            # it carries record ids, timestamps and closed-set empty-causes that point AT
            # `summaries`, never their text, and every id in it is already in the list
            # beside it. Without this line the mode is unreachable: retrieval would
            # compute per-goal evidence that synthesis never sees, and the model would go
            # back to matching the wording of a goal against the wording of a journal
            # entry and asserting progress it cannot see. That confident-wrong answer is
            # the failure the mode exists to remove, so the carry is the feature.
            if context_packet.get("commitment_report"):
                payload["commitment_report"] = context_packet["commitment_report"]
        else:
            payload["answer_type"] = "raw"
            payload["rows"] = context_packet.get("rows") or []

        # Enforced exclusion (retrieval plane). Carried on EVERY mode because the
        # claim it makes is about the whole turn: what the owner asked to be left
        # out, whether it actually was, and how much it removed. The block is closed
        # -set slugs and integers — the excluded entity's name is never in it. It
        # travels because the alternative is an answer that quietly reads as though
        # an exclusion the engine could not compile had been honoured.
        exclusion = context_packet.get("exclusion")
        if isinstance(exclusion, dict) and exclusion:
            payload["exclusion"] = exclusion

        # Row-cap truncation (retrieval plane). Carried on EVERY mode for the same
        # reason as the exclusion block: the claim is about the whole turn, not about
        # one lane's contents. It is closed-set — an integer cap and table names — and
        # never carries a row.
        #
        # This layer REBUILDS the payload rather than passing the packet through, so a
        # field set in retrieval and not named here is dropped silently. That is what
        # happened on 2026-08-25: retrieval set `truncated`, the ledger recorded
        # `capped/row_cap_reached`, and `public_result` arrived with four keys and no
        # sign that the answer had been cut off. A scheduled report then stated that
        # something had not happened, when the evidence that it had was one row past
        # the cap. An absence read off a truncated result is the failure this field
        # prevents, and it only prevents it if it survives this rebuild.
        truncated = context_packet.get("truncated")
        if isinstance(truncated, dict) and truncated:
            payload["truncated"] = truncated

        return PublicResult(payload=payload, strategy=strategy.value)


GameLayer = DefaultGameLayer
