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
    if subtype == "truth_verdict":
        # Per-lane truthfulness stance for the mode-gated verify_claim path
        # (PLAN_TRUTHFULNESS_PLUGIN.md). Evidence arrives pre-filtered by the
        # mode's aperture; this prompt only compares, never retrieves.
        claim = str(payload.get("claim") or "")
        lanes = payload.get("lanes") or {}
        lane_lines = []
        for lane_name, texts in lanes.items():
            if isinstance(texts, list) and texts:
                joined = "\n".join(f"  - {str(t)[:300]}" for t in texts[:8])
                lane_lines.append(f"{lane_name}:\n{joined}")
        lanes_block = "\n".join(lane_lines) or "(no evidence)"
        return (
            "You judge whether a spoken claim agrees with stored facts. For each "
            "evidence lane, decide independently using ONLY that lane's facts.\n"
            'Reply JSON only: {"lanes": {"<lane>": {"stance": '
            '"supports"|"contradicts"|"no_evidence", "confidence": <0.0-1.0>}}}\n'
            '- "supports": the lane\'s facts are consistent with the claim.\n'
            '- "contradicts": the lane\'s facts are inconsistent with the claim '
            "(opposite preference, mutually exclusive value, or the claim denies "
            "something the facts assert).\n"
            '- "no_evidence": the facts are about UNRELATED attributes.\n'
            "- IMPORTANT: single-valued attributes (a person's name, age, "
            "origin, a favorite): if a fact states a DIFFERENT value than the "
            'claim asserts, that lane "contradicts" with high confidence — a '
            "different stored value is evidence of falsehood, never "
            '"no_evidence". Claim "my name is Timothy" vs fact "goes by Jonny" '
            '→ contradicts.\n'
            "- Spelling variants of the same spoken name/word (Jonny/Johnny, "
            "Sara/Sarah) are the SAME value → supports.\n"
            "- Judge only lanes present below; confidence reflects how directly "
            "the facts bear on the claim. Never invent facts.\n\n"
            f"Claim: {claim[:500]}\n\nEvidence lanes:\n{lanes_block[:2500]}"
        )
    if subtype == "query_inference":
        # Typed answers, not forced booleans: the old "yes|no|unknown" template
        # made every who/what/list question structurally unanswerable (the
        # model replied "yes" at confidence 1.0 to "Who do I message most?").
        # B8 attribution: do NOT claim every scored record is the owner's —
        # owner_authored/speaker_label mark others' evidence that must be
        # attributed or ignored for first-person asks.
        ctx = payload.get("context") or ""
        q = payload.get("query") or text
        return (
            "You answer a question about the owner's personal data using ONLY the "
            "provided context.\n"
            'Reply JSON only: {"answer": <answer>, "confidence": <0.0-1.0>}\n'
            '- Yes/no question: answer "yes" or "no".\n'
            "- Who/what/which/when/how-much question: answer with the specific "
            "name(s) or value(s) found in the context, as a short phrase.\n"
            '- List question: answer with a JSON array of short strings.\n'
            "- If the query is a topic rather than a question, answer \"yes\" or "
            '"no" for whether the context contains relevant material, with '
            "confidence reflecting how strong that material is.\n"
            "- The context is privacy-filtered: records appear as relevance/"
            "similarity scores without raw message text. Attribution metadata "
            "may still be present:\n"
            "  * owner_authored=true (or missing on derived facts/stats): may "
            "support a first-person claim about the owner.\n"
            "  * owner_authored=false / speaker_label set: another person's "
            "speech or an assistant reply — NEVER present as the owner's "
            "opinion, interest, identity, or preference. Attribute by "
            "speaker_label or ignore for first-person asks.\n"
            "- Decision rule for topic queries: if any OWNER-authored (or "
            "unmarked derived) record has relevance_score or similarity >= 0.6, "
            'answer "yes" with confidence = the highest such score. Scores that '
            "are only other-authored do NOT justify a first-person yes.\n"
            '- If the context does not contain owner-grounded information, answer '
            '"unknown" with confidence 0.0. Never guess beyond the context.\n\n'
            f"Query: {q}\n\nContext: {ctx[:3500]}"
        )
    return str(payload) if payload else ""
