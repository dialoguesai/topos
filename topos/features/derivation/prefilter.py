"""Lexical per-pack prefilter (S1c sizing consequence; R0-router-lite).

A record reaches a pack's LLM pass only if it lexically resembles the pack's
routing block. Deliberately generous (recall over precision — a miss is silent
sparsity); hit-rates are telemetry for the real R0 router later.
"""
from __future__ import annotations

import re
from typing import Set

from .packs import Pack

_STOP = {"the","a","an","and","or","of","to","in","for","with","my","our","their","what","who",
         "how","i","we","is","are","was","be","on","at","it","this","that",
         # generic enum/descriptor words that would fire on anything
         "other","none","active","new","people","owner","string","kind","status","recurring"}


def _tokens(text: str) -> Set[str]:
    return {t for t in re.findall(r"[a-z']+", (text or "").lower()) if len(t) > 2 and t not in _STOP}


class PackPrefilter:
    def __init__(self, pack: Pack) -> None:
        r = pack.routing or {}
        self.exemplars = [str(e).lower() for e in (r.get("exemplars") or [])]
        toks: Set[str] = set()
        for d in r.get("descriptors") or []:
            toks |= _tokens(str(d))
        for e in self.exemplars:
            toks |= _tokens(e)
        # predicate enums + value_schema enums are domain vocabulary too
        for pred in pack.predicates.values():
            for v in pred.values or []:
                toks |= _tokens(str(v).replace("_", " "))
            for spec in (pred.value_schema or {}).values():
                if isinstance(spec, dict):
                    for v in spec.get("enum") or []:
                        toks |= _tokens(str(v).replace("_", " "))
        # the pack's own eval gold is routing vocabulary BY DEFINITION — a pack
        # whose prefilter drops its own gold has coverage that silently never
        # happens at ingest (found 2026-08-26: 13+ gold texts across 11 packs)
        for g in ((pack.raw or {}).get("eval") or {}).get("gold") or []:
            toks |= _tokens(str(g.get("text") or ""))
        self.lexicon = toks - _STOP
        self.hits = 0
        self.misses = 0

    def passes(self, text: str) -> bool:
        low = (text or "").lower()
        # phrase exemplars are the strongest signal; one DISTINCTIVE unigram is enough
        # (recall over precision: a miss is silent sparsity, an extra hit is one LLM call)
        if any(e in low for e in self.exemplars) or (_tokens(low) & self.lexicon):
            self.hits += 1
            return True
        self.misses += 1
        return False
