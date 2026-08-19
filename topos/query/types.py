"""Query pipeline types (Phase 3 runtime)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

AccessMode = Literal["raw", "summary", "inference"]

MODE_RANK = {"summary": 0, "inference": 1, "raw": 2}


class RetrievalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RetrievalRequest:
    manifest: Any
    access_mode: AccessMode
    query_text: Optional[str] = None
    #: What the RARE GATE should treat as discriminative needles, when the caller can
    #: say it better than the raw text can. Defaults to `query_text`.
    #:
    #: `_residual_content_tokens` turns every non-surface, non-recency token into a word
    #: the retrieved rows must contain, and a rare needle matching nothing empties the
    #: lane — correctly, for a specific ask. But an instruction is not an ask: "summarize
    #: achievements … with any adjustments made" gates the very lanes it is asking about.
    #: Measured live 2026-08-17, one node / window / scope: 0 summaries for the full
    #: prompt, 25 for the same question distilled.
    #:
    #: Needles ONLY. The planner, vector ranking and scope classifier keep `query_text` —
    #: handing them a keyword digest was measured on 2026-08-16 to lose time windows and
    #: make the classifier abstain on keyword soup.
    needle_text: Optional[str] = None
    #: The SAME needles, split PER PART of a multi-part request. One flattened needle
    #: set cannot gate a multi-part request: the rare gate vetoes the whole answer when
    #: ANY token is unevidenced, so part A's specific ask ("the Threnody-7 rewrite")
    #: empties part B's lane ("how did I sleep"). On a six-section report that is the
    #: gate silently off — it fires on every section or none, and the sections that
    #: needed protection from filler are exactly the ones it cannot reach.
    #:
    #: Each entry is the subject of one part, in request order. The gate runs once per
    #: entry and the request is vetoed only when EVERY part is vetoed. Absent (the
    #: default) means one part — `needle_text or query_text` — which is byte-identical
    #: to the pre-multi-needle behaviour.
    #:
    #: Same separation as `needle_text`: needles ONLY. The planner, the embeddings and
    #: the scope classifier keep the owner's sentence.
    needle_parts: Optional[List[str]] = None
    filter_manifest: Optional[Dict[str, Any]] = None
    field_transforms: Optional[List[Any]] = None
    skip_retrieval: bool = False
    installed_source_ids: Optional[List[str]] = None
    disclosure_tier: str = "owner_raw"
    requester_id: str = "owner"
    owner_id: str = "owner"
    # Selector-aware disclosure (plan A2): the query names a third-party entity this grantee
    # may not select. Retrieval must produce an empty, mode-appropriate result — the entity's
    # data never participates (PermLLM access-advantage=0), and the shape is identical to a
    # query about a nonexistent entity (CQE indistinguishability).
    suppress_selectors: bool = False
    # A2.3 / A8 refuse-vs-aggregate: grantee ask is aggregate-only under an active entity
    # selector (and/or a recognized cohort token). Retrieval returns a non-entity-specific
    # aggregate packet — never named-person rows. Mutually exclusive with suppress_selectors.
    cohort_aggregate: bool = False
    # Reference instant for temporal planning (as-of derivation, relative time
    # ranges). datetime or ISO string; None → wall clock. Eval harnesses inject
    # a fixed now (pipeline.execute(now=...) or TOPOS_QUERY_NOW) so month
    # arithmetic is reproducible.
    now: Optional[Any] = None
    #: Optional `narrowing.NarrowingLedger`, mutated in place by whichever stages
    #: shrink this request. Absent (the default) means every path behaves exactly as
    #: it did before — the ledger records what already happens, it never decides
    #: anything. Typed `Any` to keep `types` free of a query-module import.
    ledger: Optional[Any] = None


@dataclass
class RetrievalBundle:
    context_packet: Dict[str, Any] = field(default_factory=dict)
    stores_touched: List[str] = field(default_factory=list)
    record_counts: Dict[str, int] = field(default_factory=dict)
    retrieval_metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class FilteredContext:
    context_packet: Dict[str, Any] = field(default_factory=dict)
    filters_applied: List[str] = field(default_factory=list)


@dataclass
class PublicResult:
    payload: Dict[str, Any] = field(default_factory=dict)
    strategy: str = "direct"

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.payload)


@dataclass
class QueryTurn:
    query_text: str
    scope_id: str
    access_mode: AccessMode
    intent_hash: str = ""
    requester_id: str = ""


@dataclass
class ClassificationResult:
    outcome: Any
    cache_key: Optional[str] = None
    deny_reason: Optional[str] = None


FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {"evidence", "source_rows", "retrieval_context", "prompt", "vector", "embedding", "vectors"}
)

FORBIDDEN_INFERENCE_PUBLIC_KEYS = frozenset({"evidence", "source_rows", "retrieval_context"})
