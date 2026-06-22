"""Shared prompt construction for generative enrichment subtypes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROMPT_CONFIG: Optional[Dict[str, Any]] = None


def _load_prompt_config() -> Dict[str, Any]:
    global _PROMPT_CONFIG
    if _PROMPT_CONFIG is not None:
        return _PROMPT_CONFIG
    config_path = Path(__file__).resolve().parents[2] / "enrichment" / "signal_derivation_config.json"
    try:
        _PROMPT_CONFIG = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        _PROMPT_CONFIG = {}
    return _PROMPT_CONFIG


def build_generative_prompt(subtype: str, payload: Dict[str, Any]) -> str:
    text = payload.get("text") or payload.get("content") or payload.get("url") or ""
    if subtype == "emotion_classification" or subtype == "emo_27":
        return (
            f'Classify the emotion of this text in one word or short phrase. '
            f'Reply with JSON only: {{"emotion_label": "...", "confidence": 0.9}}\n\nText: {text}'
        )
    if subtype == "topic_extraction":
        return (
            "Extract up to 5 topics from the text. Reply with JSON only: "
            '{"topics": [{"label": "...", "confidence": 0.9}]}\n\nText: '
            f"{text}"
        )
    if subtype == "brief_update":
        dimension = payload.get("dimension") or "memory"
        records = payload.get("records") or []
        context = "\n".join(
            str(r.get("brief_input") or r.get("content", r))[:500]
            for r in records[:20]
            if r
        )
        template = payload.get("prompt") or f"Summarize the following {dimension} dimension records."
        from topos.features.signal.brief_schemas import llm_json_template

        json_shape = llm_json_template(str(dimension))
        return (
            f"{template}\n\nRecords:\n{context}\n\n"
            f"Reply JSON only: {json_shape}"
        )
    if subtype == "raw_to_summary":
        dimension = payload.get("dimension") or "memory"
        records = payload.get("records") or []
        context = "\n".join(
            str(r.get("brief_input") or r.get("content", r))[:500]
            for r in records[:20]
            if r
        )
        templates = _load_prompt_config().get("dimension_summary_templates") or {}
        template = templates.get(dimension) or f"Summarize the following {dimension} dimension records."
        return (
            f"{template}\n\nRecords:\n{context}\n\n"
            f'Reply JSON: {{"summary_text": "...", "dimension": "{dimension}"}}'
        )
    if subtype == "goal_extraction":
        return (
            "Extract user goals from the AI chat content. Reply JSON only: "
            '{"goals": [{"text": "...", "confidence": 0.8, "horizon": "short"}]}\n\nText: '
            f"{text}"
        )
    if subtype == "query_inference":
        ctx = payload.get("context") or ""
        q = payload.get("query") or text
        return (
            f"Answer yes or no with confidence 0-1. Reply JSON only: "
            f'{{"answer": "yes|no|unknown", "confidence": 0.5}}\n\nQuery: {q}\n\nContext: {ctx[:3500]}'
        )
    return str(payload) if payload else ""
