"""On-device disclosure minimizer (plan §D).

Content-level "only what's needed" enforcement for grantee responses. Runs AFTER the
deterministic disclosure filter and BEFORE the game layer; owner queries skip it entirely.

Verifiable-subset contract (the whole point):
  * The candidate disclosure is decomposed into enumerated atomic facts, each with an id.
  * A pluggable *selector* (deterministic, or a bounded local LLM) returns the set of fact
    IDs to KEEP — never free text.
  * The output packet is reconstructed from ONLY the selected original facts. The model
    therefore cannot invent, expand, or exfiltrate: output is provably ⊆ input. The worst an
    adversarial/injected fact can do is make the selector keep MORE (up to the full input,
    which already passed the deterministic filter) — never something not already present.
  * A deterministic backstop (PII/NSFW re-scrub) runs LAST, so the LLM never has the final say.

Fail-closed: if the primary selector errors/times out, a more-aggressive DETERMINISTIC
fallback selector runs instead — the fuller candidate is never passed through.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Set

# Artifact lists whose items are treated as atomic facts for minimization.
_FACT_KEYS = ("rows", "summaries", "scores", "semantic_hits", "facts")

# Text-bearing fields used to render a fact's text for the selector / relevance scoring.
_TEXT_FIELDS = (
    "content", "summary_text", "topic", "label", "text", "text_preview",
    "content_preview", "entity_text", "tag", "title", "description",
)

_TOKEN_RE = re.compile(r"[\w']+")
_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "to", "for", "and", "or", "in", "on", "at", "is", "are",
     "with", "about", "me", "my", "your", "i", "what", "who", "when", "how", "did", "do"}
)


class SelectorUnavailable(Exception):
    """Raised by a selector when the underlying model is unavailable or times out."""


@dataclass(frozen=True)
class Fact:
    fact_id: str
    text: str
    source_key: str
    source_index: int


def _fact_text(item: Any) -> str:
    if isinstance(item, dict):
        parts = [str(item.get(f)) for f in _TEXT_FIELDS if isinstance(item.get(f), str) and item.get(f)]
        if parts:
            return " ".join(parts)
        return " ".join(str(v) for v in item.values() if isinstance(v, str))
    return str(item)


def enumerate_facts(packet: Optional[Dict[str, Any]]) -> List[Fact]:
    """Decompose a context packet's artifact lists into atomic facts with stable ids."""
    facts: List[Fact] = []
    if not isinstance(packet, dict):
        return facts
    for key in _FACT_KEYS:
        seq = packet.get(key)
        if not isinstance(seq, list):
            continue
        for idx, item in enumerate(seq):
            facts.append(Fact(fact_id=f"{key}:{idx}", text=_fact_text(item), source_key=key, source_index=idx))
    return facts


class FactSelector(Protocol):
    version: str

    def select(self, *, intent: str, facts: Sequence[Dict[str, Any]]) -> Set[str]:
        """Return the subset of fact_ids to KEEP. Must only return ids drawn from `facts`."""
        ...


class KeepAllSelector:
    """Baseline: keeps everything (equivalent to minimizer off). Useful as a control."""

    version = "keep_all/1"

    def select(self, *, intent: str, facts: Sequence[Dict[str, Any]]) -> Set[str]:
        return {str(f["fact_id"]) for f in facts}


class KeywordRelevanceSelector:
    """Deterministic minimizer + fail-closed fallback: keep facts sharing a content token with
    the intent. No model needed; never keeps more than the relevant subset."""

    version = "keyword_relevance/1"

    def _tokens(self, text: str) -> Set[str]:
        return {t for t in _TOKEN_RE.findall(str(text or "").lower()) if t not in _STOPWORDS and len(t) > 1}

    def select(self, *, intent: str, facts: Sequence[Dict[str, Any]]) -> Set[str]:
        want = self._tokens(intent)
        if not want:
            # No usable intent tokens → keep nothing (most conservative).
            return set()
        kept: Set[str] = set()
        for f in facts:
            if self._tokens(f.get("text", "")) & want:
                kept.add(str(f["fact_id"]))
        return kept


_MINIMIZER_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="disclosure_minimizer")
_ID_RE = re.compile(r"(rows|summaries|scores|semantic_hits|facts):\d+")


def build_minimizer_prompt(intent: str, facts: Sequence[Dict[str, Any]]) -> str:
    lines = [
        "You are a privacy minimizer. Given a requester's intent and a numbered list of "
        "candidate facts, return ONLY the fact ids that are strictly necessary to satisfy the "
        "intent. Output a JSON array of id strings and nothing else. Do not add facts.",
        f"INTENT: {intent}",
        "FACTS:",
    ]
    for f in facts:
        lines.append(f'  {f["fact_id"]}: {f.get("text", "")}')
    lines.append('ANSWER (JSON array of necessary fact ids, e.g. ["rows:0","rows:2"]):')
    return "\n".join(lines)


def parse_selected_ids(text: str, *, valid: Set[str]) -> Optional[Set[str]]:
    """Parse a model completion into a set of kept fact ids. Returns None when the output is
    unparseable (→ the caller fails closed to the deterministic fallback). An explicit empty
    selection ("[]") is a VALID result (keep nothing), not a failure."""
    if text is None:
        return None
    raw = str(text).strip()
    # 1. JSON array anywhere in the output.
    start, end = raw.find("["), raw.rfind("]")
    if start != -1 and end > start:
        try:
            arr = json.loads(raw[start : end + 1])
            if isinstance(arr, list):
                return {str(x) for x in arr} & valid  # empty list → keep nothing (valid)
        except Exception:
            pass
    # 2. Fall back to id-pattern extraction (findall would return only the capture group,
    #    so use finditer to rebuild the full "key:index" id).
    ids = {m.group(0) for m in _ID_RE.finditer(raw)} & valid
    if ids:
        return ids
    # No JSON array and no ids found → unparseable garbage.
    return None


class EngineSelector:
    """Bounded local-model fact selector. Reuses the query-inference plumbing (Ollama, thread
    pool, hard timeout). Fails closed: any timeout / error / unparseable output raises
    SelectorUnavailable so the minimizer applies its deterministic fallback."""

    version = "ollama_minimizer/1"

    def __init__(
        self,
        *,
        complete: Optional[Callable[[str], str]] = None,
        timeout_sec: float = 20.0,
        engine: Any = None,
    ) -> None:
        self._complete = complete
        self._timeout = timeout_sec
        self._engine = engine

    def _complete_via_engine(self, prompt: str) -> str:
        from ..config.settings import settings
        from ..engine.client import get_engine_client_or_local
        from ..engine.tasks import ModelRequest, ProcessingTask

        client = get_engine_client_or_local(self._engine)
        model = getattr(settings, "disclosure_minimizer_model", None) or settings.ollama_query_model
        task = ProcessingTask(
            id="disclosure_minimize",
            type="query_inference",
            subtype="query_inference",
            source_id="disclosure_minimizer",
            record_ids=[],
            input={"query": prompt, "context": ""},
            model_request=ModelRequest(provider="ollama", model=model),
        )
        result = client.run(task)
        if getattr(result, "status", None) != "completed":
            raise SelectorUnavailable(f"minimizer model status={getattr(result, 'status', None)}")
        out = result.output or {}
        return str(out.get("answer") or out.get("output") or "")

    def select(self, *, intent: str, facts: Sequence[Dict[str, Any]]) -> Set[str]:
        prompt = build_minimizer_prompt(intent, facts)
        runner = self._complete or self._complete_via_engine
        try:
            future = _MINIMIZER_POOL.submit(runner, prompt)
            text = future.result(timeout=self._timeout)
        except FuturesTimeoutError as exc:
            raise SelectorUnavailable("minimizer timed out") from exc
        except SelectorUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — any model error fails closed
            raise SelectorUnavailable(str(exc)) from exc
        valid = {str(f["fact_id"]) for f in facts}
        ids = parse_selected_ids(text, valid=valid)
        if ids is None:
            raise SelectorUnavailable("unparseable minimizer output")
        return ids


@dataclass
class MinimizeResult:
    packet: Dict[str, Any]
    ran: bool
    removed_fact_ids: List[str] = field(default_factory=list)
    kept_fact_ids: List[str] = field(default_factory=list)
    model_version: str = ""
    used_fallback: bool = False
    backstop_hits: List[str] = field(default_factory=list)

    def ddr_summary(self) -> Dict[str, Any]:
        return {
            "ran": self.ran,
            "removed_facts": len(self.removed_fact_ids),
            "kept_facts": len(self.kept_fact_ids),
            "model_version": self.model_version,
            "used_fallback": self.used_fallback,
        }


def _reconstruct(packet: Dict[str, Any], facts: List[Fact], kept_ids: Set[str]) -> Dict[str, Any]:
    """Rebuild the packet keeping ONLY the facts whose id is in kept_ids. Non-fact keys pass
    through untouched. Output items are the original items — never regenerated."""
    keep_by_key: Dict[str, Set[int]] = {}
    for f in facts:
        if f.fact_id in kept_ids:
            keep_by_key.setdefault(f.source_key, set()).add(f.source_index)
    out = dict(packet)
    for key in _FACT_KEYS:
        seq = packet.get(key)
        if not isinstance(seq, list):
            continue
        keep_idx = keep_by_key.get(key, set())
        out[key] = [item for idx, item in enumerate(seq) if idx in keep_idx]
    return out


def _backstop(packet: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    """Deterministic last word: re-run the grantee PII/NSFW scrub on every fact list, so the
    LLM's selection can never reintroduce a raw value the deterministic layer would strip."""
    from .disclosure import _scrub_grantee_text_items

    out = dict(packet)
    hits: List[str] = []
    for key in _FACT_KEYS:
        seq = out.get(key)
        if isinstance(seq, list) and seq:
            scrubbed = _scrub_grantee_text_items(seq)
            if scrubbed != seq:
                hits.append(key)
            out[key] = scrubbed
    return out, hits


class DisclosureMinimizer:
    def __init__(self, selector: Optional[FactSelector] = None, fallback: Optional[FactSelector] = None) -> None:
        self._selector: FactSelector = selector or KeywordRelevanceSelector()
        self._fallback: FactSelector = fallback or KeywordRelevanceSelector()

    def minimize(self, packet: Dict[str, Any], *, intent: str, disclosure_tier: str) -> MinimizeResult:
        # Grantee-only. Owner sees everything (including reasoning) — never minimized.
        if disclosure_tier != "default_disclosure":
            return MinimizeResult(packet=packet, ran=False, model_version="")

        facts = enumerate_facts(packet)
        if not facts:
            return MinimizeResult(packet=packet, ran=True, model_version=self._selector.version)

        valid_ids = {f.fact_id for f in facts}
        fact_dicts = [{"fact_id": f.fact_id, "text": f.text} for f in facts]

        used_fallback = False
        try:
            raw_kept = self._selector.select(intent=intent, facts=fact_dicts)
            model_version = self._selector.version
        except Exception:
            # Fail closed: a more-aggressive deterministic reduction, never keep-all.
            raw_kept = self._fallback.select(intent=intent, facts=fact_dicts)
            model_version = f"fallback:{self._fallback.version}"
            used_fallback = True

        # Only ids from OUR enumeration survive — the selector cannot inject new ones.
        kept = valid_ids & {str(i) for i in (raw_kept or set())}
        removed = valid_ids - kept

        reduced = _reconstruct(packet, facts, kept)
        reduced, hits = _backstop(reduced)

        return MinimizeResult(
            packet=reduced,
            ran=True,
            removed_fact_ids=sorted(removed),
            kept_fact_ids=sorted(kept),
            model_version=model_version,
            used_fallback=used_fallback,
            backstop_hits=hits,
        )
