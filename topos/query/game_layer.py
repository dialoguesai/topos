"""Game layer reveal strategies (PRD §8.6)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict

from .types import FORBIDDEN_INFERENCE_PUBLIC_KEYS, PublicResult


class RevealStrategy(str, Enum):
    DIRECT = "direct"
    PROGRESSIVE = "progressive"
    SUMMARY_FIRST = "summary_first"
    INFERENCE_GATED = "inference_gated"
    OWNER_CONFIRM = "owner_confirm"
    MINIMUM_DISCLOSURE = "minimum_disclosure"


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

    def apply(self, *, context_packet: Dict[str, Any], access_mode: str, scope_id: str = "") -> PublicResult:
        strategy = self.select_strategy(access_mode, scope_id)
        payload: Dict[str, Any] = {"access_mode": access_mode, "scope_id": scope_id}

        if access_mode == "inference":
            scores = context_packet.get("scores") or []
            yes_no = bool(scores)
            confidence = 0.0
            if scores and isinstance(scores[0], dict):
                confidence = float(scores[0].get("value") or scores[0].get("confidence") or 0.0)
            payload.update({"answer": "yes" if yes_no else "no", "confidence": confidence})
            for forbidden in FORBIDDEN_INFERENCE_PUBLIC_KEYS:
                payload.pop(forbidden, None)
        elif access_mode == "summary":
            payload["summaries"] = context_packet.get("summaries") or []
        else:
            payload["rows"] = context_packet.get("rows") or []

        return PublicResult(payload=payload, strategy=strategy.value)


GameLayer = DefaultGameLayer
