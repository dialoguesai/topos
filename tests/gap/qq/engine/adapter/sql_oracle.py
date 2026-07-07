"""Oracle computing ground truth from the live DB (live lane) and seeded needles. REAL — Phase 1.

Wraps the existing composition_eval_cases oracle logic behind the topos_eval.Oracle
protocol: each CompositionCase's SQL oracle runs once against the DB (live) or the seeded
scratch DB, and the resulting needle groups + topic terms become graded relevance:

    2 — the item contains a needle-group alternative (the actual answer)
    1 — the item matches a topic term (related, on-topic)
    0 — off-topic

provenance:
  * live SQL oracle  → "oracle"
  * seeded needle    → "seeded"
  * necessary_atoms/forbidden_atoms return empty sets in Phase 1 — the real N(q) must be
    derived by minimum-sufficient-set ablation (topos_eval.metrics.disclosure, Phase 2,
    wiki 10 T2). We return nothing rather than a hand list masquerading as derived truth.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Tuple

from topos_eval.protocols.oracle import Provenance
from topos_eval.protocols.target import Request


def _blob(obj: Any) -> str:
    return json.dumps(obj, default=str).lower()


@dataclass(frozen=True)
class CaseTruth:
    """The computed ground truth for one case, keyed by its (resolved) query text."""

    needle_groups: Tuple[Tuple[str, ...], ...]  # each group: any alternative counts
    topic_terms: Tuple[str, ...] = ()
    negative: bool = False  # correct behavior is abstention
    vacuous: bool = False  # the DB lacks data to grade this case


class SqlOracle:
    """Oracle backed by ground truth computed per-case from a read-only sqlite connection.

    Build with ``from_cases`` so every case's SQL oracle runs exactly once; requests are
    matched to their truth by query text (the same key the runner uses to pair them).
    """

    def __init__(self, provenance: Provenance = "oracle") -> None:
        self._provenance = provenance
        self._truth: Dict[str, CaseTruth] = {}

    @classmethod
    def from_cases(
        cls, cases: Iterable[Any], conn: sqlite3.Connection, *, provenance: Provenance = "oracle"
    ) -> "SqlOracle":
        """cases: composition_eval_cases.CompositionCase list. Runs each case's SQL oracle
        against ``conn`` and registers the truth under the case's resolved query text."""
        oracle = cls(provenance=provenance)
        for case in cases:
            case_oracle = case.oracle(conn)
            oracle.register(
                case.query_text(conn),
                needle_groups=[list(g) for g in case_oracle.needle_groups],
                topic_terms=list(getattr(case, "topic_terms", ()) or ()),
                negative=bool(getattr(case, "negative", False)),
                vacuous=not case_oracle.ok,
            )
        return oracle

    def register(
        self,
        query_text: str,
        *,
        needle_groups: List[List[str]],
        topic_terms: List[str] | None = None,
        negative: bool = False,
        vacuous: bool = False,
    ) -> None:
        self._truth[query_text] = CaseTruth(
            needle_groups=tuple(tuple(g) for g in needle_groups),
            topic_terms=tuple(topic_terms or ()),
            negative=negative,
            vacuous=vacuous,
        )

    def truth_for(self, request: Request) -> CaseTruth | None:
        return self._truth.get(request.text)

    # -- topos_eval.Oracle protocol ----------------------------------------------------

    def graded_relevance(self, request: Request, item: Dict[str, Any]) -> int:
        truth = self._truth.get(request.text)
        if truth is None or truth.negative:
            return 0  # nothing is relevant to a fabricated topic
        blob = _blob(item)
        for group in truth.needle_groups:
            if any(alt.lower() in blob for alt in group):
                return 2
        if any(term.lower() in blob for term in truth.topic_terms):
            return 1
        return 0

    def answer_key(self, request: Request) -> Any:
        """The needle groups (any alternative per group is acceptable). None for negative
        controls — the suite grades that the node abstained."""
        truth = self._truth.get(request.text)
        if truth is None or truth.negative or truth.vacuous:
            return None
        return [list(g) for g in truth.needle_groups]

    def necessary_atoms(self, request: Request) -> set[str]:
        return set()  # Phase 2: outcome-derived N(q); never hand-asserted here

    def forbidden_atoms(self, request: Request) -> set[str]:
        return set()  # Phase 2: policy-derived F(q)

    @property
    def provenance(self) -> Provenance:
        return self._provenance
