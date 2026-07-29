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


def _extract_entity_labels(context_packet: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    seen: set[str] = set()
    graph = context_packet.get("graph") or {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        for key in ("label", "name", "node_id", "entity_text"):
            value = node.get(key)
            if value and str(value) not in seen:
                seen.add(str(value))
                labels.append(str(value))
    for score in context_packet.get("scores") or []:
        if not isinstance(score, dict):
            continue
        for key in ("entity_text", "label", "topic", "summary_text"):
            value = score.get(key)
            if value and str(value) not in seen:
                seen.add(str(value))
                labels.append(str(value))
    return labels[:10]


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
                    confidence = float(semantic[0].get("similarity") or 0.0)
                elif scores and isinstance(scores[0], dict):
                    confidence = float(scores[0].get("value") or scores[0].get("confidence") or 0.0)
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
        else:
            payload["answer_type"] = "raw"
            payload["rows"] = context_packet.get("rows") or []

        return PublicResult(payload=payload, strategy=strategy.value)


GameLayer = DefaultGameLayer
