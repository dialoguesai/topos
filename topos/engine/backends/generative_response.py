"""Parse generative enrichment responses (shared across LLM backends)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def parse_json_object(response_text: str) -> Dict[str, Any]:
    response_text = (response_text or "").strip()
    start = response_text.find("{")
    if start < 0:
        return {}
    end = response_text.rfind("}") + 1
    if end <= start:
        return {}
    try:
        obj = json.loads(response_text[start:end])
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def parse_generative_response(
    response_text: str,
    subtype: str,
    model: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    response_text = (response_text or "").strip()
    if subtype in ("emotion_classification", "emo_27"):
        try:
            start = response_text.find("{")
            if start >= 0:
                end = response_text.rfind("}") + 1
                if end > start:
                    obj = json.loads(response_text[start:end])
                    return {
                        "emotion_label": obj.get("emotion_label"),
                        "confidence": obj.get("confidence"),
                        "all_emotions": [
                            {"label": obj.get("emotion_label"), "confidence": obj.get("confidence", 0)}
                        ],
                    }
        except (json.JSONDecodeError, KeyError):
            pass
        return {
            "emotion_label": response_text[:100] if response_text else None,
            "confidence": None,
            "all_emotions": [],
        }
    if subtype == "topic_extraction":
        parsed = parse_json_object(response_text)
        topics = parsed.get("topics") if isinstance(parsed, dict) else []
        if not isinstance(topics, list):
            topics = []
        return {"topics": topics[:5], "model": model}
    if subtype == "brief_update":
        parsed = parse_json_object(response_text)
        dimension = (payload or {}).get("dimension") or "memory"
        from topos.features.signal.brief_ontology import llm_merge_section_ids

        merge_keys = llm_merge_section_ids(str(dimension))
        if isinstance(parsed, dict):
            out: Dict[str, Any] = {
                "dimension": parsed.get("dimension") or dimension,
                "model": model,
            }
            for key in merge_keys:
                out[key] = parsed.get(key)
            if not any(str(out.get(k) or "").strip() for k in merge_keys):
                legacy = parsed.get("at_a_glance") or parsed.get("summary_text")
                if legacy and merge_keys:
                    out[merge_keys[0]] = legacy
            return out
        fallback_key = merge_keys[0] if merge_keys else "at_a_glance"
        return {fallback_key: response_text[:2000], "model": model}
    if subtype == "raw_to_summary":
        parsed = parse_json_object(response_text)
        if isinstance(parsed, dict) and parsed.get("summary_text"):
            return {
                "summary_text": parsed.get("summary_text"),
                "dimension": parsed.get("dimension"),
                "model": model,
            }
        return {"summary_text": response_text[:2000], "dimension": "memory", "model": model}
    if subtype == "goal_extraction":
        parsed = parse_json_object(response_text)
        goals = parsed.get("goals") if isinstance(parsed, dict) else []
        if not isinstance(goals, list):
            goals = []
        return {"goals": goals, "model": model}
    if subtype == "query_inference":
        parsed = parse_json_object(response_text)
        if isinstance(parsed, dict):
            answer = parsed.get("answer")
            # Typed answers: strings and lists are both valid; normalize other
            # shapes to short strings so downstream packaging stays stable.
            if isinstance(answer, list):
                answer = [str(a)[:200] for a in answer[:20]]
            elif answer is not None and not isinstance(answer, str):
                answer = str(answer)[:500]
            return {
                "answer": answer,
                "confidence": parsed.get("confidence"),
                "model": model,
            }
        return {"answer": "unknown", "confidence": 0.0, "model": model}
    return {"output": response_text, "model": model}
