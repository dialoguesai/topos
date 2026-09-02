"""Query quality eval cases: quality rubrics, latency budgets, permission boundaries."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.types import FORBIDDEN_INFERENCE_PUBLIC_KEYS

EvalFn = Callable[[Dict[str, Any]], Tuple[bool, str]]

# Version stamp for the request catalog. Bump when cases are added, removed,
# or their rubrics change — reports keyed to a catalog version stay comparable
# only within that version.
#   qq-catalog-1: Q1-Q6, P1, PB1-PB3
#   qq-catalog-2: + D1-D4 dense-intelligence series (entity dossiers, stat
#                 insights, retrieval diversity) after the dense upgrade.
#   qq-catalog-3: + graded composition lanes — C-series (live-DB oracles,
#                 composition_eval_cases.py) and S-series (seeded needles,
#                 composition_seed_corpus.py). Scored 0-1, reported not gated.
# qq-catalog-4 (Phase 1): +C13–C30 live composition cases, +N1–N12 per-scope negative
# controls, +G1–G5 generative cases. Comparability break vs qq-catalog-3 by design; the
# iteration gauge still pairs the case_ids the versions share.
# qq-catalog-5 (instrument fixes): _blob no longer ASCII-escapes (em-dash/± needles were
# unmatchable); C1/C28 oracles align to the d30 stat window retrieval serves; C29 oracle
# reads user_goals (the store retrieval reads) instead of duplicated signal_objects rows;
# auto-extracted needles get sanity guards (no leading artifacts, dedup, min word length).
# Scores comparable to qq-catalog-4 only via the iteration gauge's shared case_ids.
# qq-catalog-6 (D2 oracle classes + D1.1 hard negatives): CompositionCase gains query_class
# (known_item|browse|recency|aggregate); the SqlOracle grades canonical surface rows of a
# browse case as direct answers (closes the oracle-human gap on browse queries). +NH1-NH3
# common-word negatives (own negative_hard lane, red-first — the zero-df gate's blind
# flank). Composite comparable to qq-catalog-5 via the iteration gauge's shared case_ids.
# qq-catalog-7: +IMB1-IMB10 imbalance/attribution lane (own scratch corpus qq-imb-1,
# poison_groups+authored_only fields, red-first non-gating); +T4-T8 temporal cases
# (qq-seeded-4). Composite comparable to qq-catalog-6 via the iteration gauge's
# shared case_ids.
# qq-catalog-8: +PRV-A1..PRV-M1 provenance-taxonomy lane (qq-prv-1, taxonomy-mapped,
# red-first non-gating). Existing lanes untouched — composite comparable to
# qq-catalog-7 via the iteration gauge's shared case_ids.
# qq-catalog-9 (post demo-purge recalibration, 2026-07-10): the owner deleted the demo
# corpora (demo_* sources) that Q1/Q2/D2 targeted — docker/keycloak/financial material no
# longer exists on the node, so those cases measured absence, not retrieval. Retargeted to
# the REAL corpus: Q1 → UMA scopes/signal extraction (ai_chat), Q5 → edtech pilots (real, distinct from Q1;
# the old seeded illustration needles have df=0 post-purge and the zero-df abstention gate
# rightly refuses them), Q2 → voice transcription
# (voxterm messages), D2 → place-visit aggregate (places:read — the live scope with real stat coverage).
# Comparable to qq-catalog-8 only via the iteration gauge's shared case_ids.
# qq-catalog-10 (C-series post-purge recalibration + rare-gate morphology): C1 excludes
# Speaker-N diarization artifacts (voxterm labels are not contacts; vacuous when no real
# contact volume exists), C8 → PostHog analytics (real niche cluster; demo art needle df 0),
# C12 → Marcus (Luc was a demo persona). Engine-side, the rare-token gate gained light
# morphology (journaling→journal, active→activity) and stat-insight tags entered the
# evidence blob — C11/C23-class self-vetoes on aggregate asks are fixed, not recalibrated.
# Comparable to qq-catalog-9 via the iteration gauge's shared case_ids.
# qq-catalog-11 (MCP introspection I-series, PLAN_MCP_INTROSPECTION.md): +I-S/I-K/I-C/
# I-A/I-G/I-B/I-R cases (introspection_eval_cases.py, qq-introspect-1) — MCP-lane ONLY
# (they exercise CP gateway card composition + the query_scope meta-intercept, not engine
# retrieval; skipped under --no-mcp). Own non-gating lanes (present/absent/guard/boundary),
# red-first; promote I-C1 + the absent lane to gates once the intercept is deployed.
# Existing lanes untouched — composite comparable to qq-catalog-10 via shared case_ids.
# qq-catalog-12 (D1.2–D1.5 hole-punchers): +D12/D13/D14/D15 cases in d1_hole_puncher_cases.py
# (own non-gating d1_hole_punchers lane; D1.1 remains NH* in negative_hard). CompositionCase
# gains optional family_id/variant_index for D1.4 paraphrase variance. Existing lane
# composites comparable to qq-catalog-11 via shared case_ids.
# qq-catalog-13 (Prov authored-goals path lock): D3 re-scoped ai_conversations→work_context
# and requires ≥1 user_goal in fused sources (product "working on lately" path).
# qq-catalog-14 (A7 D1.3 grantee×tier): +D13-GT-* grantee denial≡absence cells at
# summary/inference/raw; leak_delta_by_tier on d1_hole_punchers. Owner D13-1..3 unchanged.
# qq-catalog-15 (D1.7 / Wave B2): GEN lane qq-gen-1→qq-gen-2 — 15 answerable + 15
# unanswerable probes (was 3+2 decorative); G1 re-scoped ai_conversations→messages
# (Q2 lesson). Comparability break on GEN composite by design; other lanes untouched.
# qq-catalog-16 (Wave B8): +IMBG1–IMBG10 GEN-judged IMB phrasing lane (qq-imb-gen-1)
# on the IMB scratch corpus; attribution IdealBadPair + poison-in-answer checks.
# qq-catalog-17 (Wave B11): +D3M messenger authored-goals path (messages:read + user_goal).
# Existing lane composites comparable to qq-catalog-15 via shared case_ids.
# qq-catalog-18 (S6 graph lane): +G1 Shortfall probe ("Who works on this with me?"
# must consult the GRAPH — stores_touched carries "graph", answers name entities via
# graph:* items, not topic-cluster fragments) and +G2 direct graph:read structure ask
# (optional_seed: depends on live edge density). Existing lanes untouched — composites
# comparable to qq-catalog-17 via shared case_ids.
# qq-catalog-19 (S7 SUITE-P): +AGGREGATE_CASES P-01..P-09 — the aggregate verb
# graded on EXACT numbers over the constructed 7,500-row corpus
# (tests/fixtures/query_eval_seed/apply_aggregate_seed.py); each case carries a
# necessity_query for the old-lane baseline (the kill-switch leg). QueryQualityCase
# gains engine_only (default False): G1/G2 no longer run on the MCP lane, where a
# third-party-classed harness gets graph silence BY DESIGN and a red there would
# grade the privacy invariant as a defect. Existing lanes untouched — composites
# comparable to qq-catalog-18 via shared case_ids.
QUERY_CATALOG_VERSION = "qq-catalog-19"

_DEFAULT_LATENCY_MS = {
    "summary": int(os.environ.get("TOPOS_QQ_LATENCY_SUMMARY_MS", "10000")),
    "inference": int(os.environ.get("TOPOS_QQ_LATENCY_INFERENCE_MS", "25000")),
    "raw": int(os.environ.get("TOPOS_QQ_LATENCY_RAW_MS", "5000")),
    "deny": int(os.environ.get("TOPOS_QQ_LATENCY_DENY_MS", "500")),
}


def _blob(obj: Any) -> str:
    # ensure_ascii=False: needles containing non-ASCII chars (em-dash, ±) must be
    # able to substring-match; default escaping made them unmatchable (— vs —).
    return json.dumps(obj, default=str, ensure_ascii=False).lower()


def _public_result(response: Dict[str, Any]) -> Dict[str, Any]:
    pr = response.get("public_result")
    return pr if isinstance(pr, dict) else {}


def _not_denied(response: Dict[str, Any]) -> Tuple[bool, str]:
    if response.get("turn_outcome") == "denied" or response.get("deny_reason"):
        return False, f"denied: {response.get('deny_reason') or response.get('turn_outcome')}"
    return True, "ok"


def eval_q1_scopes(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    pr = _public_result(response)
    items = pr.get("summaries") or pr.get("summary_items") or pr.get("scores") or []
    blob = _blob(items)
    if not items:
        return False, "no summary items"
    if any(k in blob for k in ("scope", "signal", "extraction", "uma")):
        return True, "top summaries mention scopes/signal/extraction"
    return False, f"first item not scopes-related: {_blob(items[0])[:120]}"


def eval_q2_keycloak(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    pr = _public_result(response)
    conf = pr.get("confidence")
    if conf is None and isinstance(pr.get("answer"), dict):
        conf = pr["answer"].get("confidence")
    if conf is None:
        return False, "missing confidence"
    if float(conf) <= 0.5:
        return False, f"confidence too low: {conf}"
    return True, f"inference answer confidence={conf}"


def eval_q3_work_goals(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    pr = _public_result(response)
    items = pr.get("summaries") or pr.get("summary_items") or pr.get("scores") or []
    blob = _blob(items)
    if not items:
        return False, "no summaries"
    if any(k in blob for k in ("goal", "project", "work", "user_goal")):
        return True, "summaries include work/goal signal"
    return False, "summaries lack work/goal terms"


def eval_q4_collaborators(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    pr = _public_result(response)
    answer_type = pr.get("answer_type")
    if answer_type in ("list", "yes_no", "band"):
        return True, f"answer_type={answer_type}"
    if isinstance(pr.get("items"), list) and pr["items"]:
        return True, f"list with {len(pr['items'])} items"
    return False, f"expected list/yes_no/band inference shape, got {answer_type}"


def eval_q5_illustration(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    pr = _public_result(response)
    items = pr.get("summaries") or pr.get("summary_items") or pr.get("scores") or []
    blob = _blob(items)
    if not items:
        return False, "no summaries"
    if any(k in blob for k in ("edtech", "pilot", "austin")):
        return True, "edtech/pilot cluster present"
    return False, "no edtech/pilot terms in top summaries"


def eval_q6_git_raw(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    pr = _public_result(response)
    rows = pr.get("rows") or pr.get("raw_rows") or pr.get("messages") or []
    if not rows:
        return False, "no raw rows"
    if "git" in _blob(rows):
        return True, f"{len(rows)} raw rows with git content"
    return False, "rows present but no git-related content"


def _summary_items(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    pr = _public_result(response)
    items = pr.get("summaries") or pr.get("summary_items") or pr.get("scores") or []
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _sources(items: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        src = str(item.get("retrieval_source") or "?")
        counts[src] = counts.get(src, 0) + 1
    return counts


def _stores(response: Dict[str, Any]) -> List[str]:
    """stores_touched, always a list (audit.py defaults it)."""
    audit = response.get("audit") if isinstance(response.get("audit"), dict) else {}
    stores = audit.get("stores_touched")
    return [str(s) for s in stores] if isinstance(stores, list) else []


def eval_g1_graph_consulted(response: Dict[str, Any]) -> Tuple[bool, str]:
    """S6 Shortfall probe: the graph must be CONSULTED, and the answer must
    carry relation items naming entities — not topic-cluster fragments. The
    2026-08-31 audit's live finding: this exact ask returned two plausible
    names mixed with three fragments while 19 declared project entities and a
    528-degree hub sat unread one table away."""
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    stores = _stores(response)
    if "graph" not in stores:
        return False, f"graph store not consulted (stores_touched: {stores})"
    items = _summary_items(response)
    graph_items = [
        i
        for i in items
        if str(i.get("retrieval_source") or "").startswith("graph:")
    ]
    if not graph_items:
        return False, f"no graph:* items in the packet (sources: {_sources(items)})"
    named = [i for i in graph_items if i.get("entity_id") and i.get("topic")]
    if not named:
        return False, "graph items carry no entity identities"
    edge_types = sorted(
        {str(i.get("edge_type") or "") for i in graph_items if i.get("edge_type")}
    )
    return True, f"{len(graph_items)} graph items via {edge_types}"


def eval_g2_graph_direct(response: Dict[str, Any]) -> Tuple[bool, str]:
    """Direct graph:read ask: structure comes back as graph relations."""
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    if "graph" not in _stores(response):
        return False, f"graph store not consulted ({_stores(response)})"
    items = _summary_items(response)
    graph_items = [
        i for i in items if str(i.get("retrieval_source") or "").startswith("graph:")
    ]
    if not graph_items:
        return False, f"no graph:* items (sources: {_sources(items)})"
    return True, f"{len(graph_items)} relations returned"


def eval_d1_entity_dossier(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    items = _summary_items(response)
    if not items:
        return False, "no summary items"
    dossiers = [i for i in items if i.get("retrieval_source") == "entity_dossier"]
    if not any("topos" in _blob(i) for i in dossiers):
        return False, f"no Topos entity_dossier item (sources: {_sources(items)})"
    entity_kinds = sum(v for k, v in _sources(items).items() if k.startswith("entity"))
    if entity_kinds < 2:
        return False, f"entity spine thin: only {entity_kinds} entity items"
    return True, f"Topos dossier + {entity_kinds} entity items"


def eval_d2_stat_insight(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    items = _summary_items(response)
    stats = [i for i in items if i.get("retrieval_source") == "stat_insight"]
    if not stats:
        return False, f"no stat_insight items (sources: {_sources(items)})"
    if not any(("visit" in _blob(i) or "place" in _blob(i)) for i in stats):
        return False, "stat insights present but none about place visits"
    return True, f"{len(stats)} stat insights incl. place-visit aggregate"


def eval_d3_retrieval_diversity(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    items = _summary_items(response)
    sources = _sources(items)
    if len(items) < 10:
        return False, f"sparse result: {len(items)} items"
    # 3-source floor (qq-catalog-9): the 4-source floor was calibrated to the
    # demo-era corpus breadth; on the real corpus a broad ask honestly fuses
    # recents + facts + goals (semantic/cluster lanes join only when the query
    # carries discriminative content tokens, which this deliberately does not).
    if len(sources) < 3:
        return False, f"low retrieval diversity: {sorted(sources)}"
    # qq-catalog-13: "working on lately" must surface authored goals on the
    # work_context path — diversity alone greenwashed ai_conversations runs.
    if "user_goal" not in sources:
        return False, f"no user_goal in fused sources: {sorted(sources)}"
    return True, f"{len(items)} items from {len(sources)} sources: {sorted(sources)}"


def eval_d3m_messenger_goals(response: Dict[str, Any]) -> Tuple[bool, str]:
    """B11 D3-like: messages:read must fuse authored user_goal from messenger."""
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    items = _summary_items(response)
    sources = _sources(items)
    goals = [i for i in items if i.get("retrieval_source") == "user_goal"]
    if not goals:
        return False, f"no user_goal in messages:read fuse: {sorted(sources)}"
    messenger_ids = {"imessage", "signal", "demo_messenger_file"}
    messenger_goals = [
        g for g in goals if str(g.get("source_id") or "") in messenger_ids
    ]
    if not messenger_goals:
        return (
            False,
            "user_goal present but none from messenger sources "
            f"(source_ids={[g.get('source_id') for g in goals]})",
        )
    return True, f"{len(messenger_goals)} messenger user_goal items"


def eval_d4_person_dossier(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    items = _summary_items(response)
    if not items:
        return False, "no summary items"
    dossier = any(
        i.get("retrieval_source") == "entity_dossier" and "marcus" in _blob(i) for i in items
    )
    mentions = sum(1 for i in items if i.get("retrieval_source") == "entity_mention" and "marcus" in _blob(i))
    if not dossier:
        return False, f"no Marcus dossier item (sources: {_sources(items)})"
    if mentions < 1:
        return False, "dossier present but no supporting mentions"
    return True, f"Marcus dossier + {mentions} mentions"


def eval_no_forbidden_inference_keys(response: Dict[str, Any]) -> Tuple[bool, str]:
    pr = _public_result(response)
    if not pr:
        return True, "no public_result"
    leaked = [k for k in FORBIDDEN_INFERENCE_PUBLIC_KEYS if k in pr]
    if leaked:
        return False, f"forbidden keys in public_result: {leaked}"
    blob = _blob(pr)
    for k in FORBIDDEN_INFERENCE_PUBLIC_KEYS:
        if f'"{k}"' in blob:
            return False, f"forbidden key {k!r} nested in public_result"
    return True, "no evidence leakage"


@dataclass(frozen=True)
class QueryQualityCase:
    id: str
    query: str
    scope_id: str
    access_mode: str
    evaluate: EvalFn
    max_latency_ms: int = 0
    description: str = ""
    optional_seed: bool = False
    # True: the case grades an owner_raw-only surface (e.g. the graph lane) that a
    # third-party-classed MCP harness is CORRECTLY silent on — running it over MCP
    # would grade the privacy invariant as a failure.
    engine_only: bool = False

    def __post_init__(self) -> None:
        if self.max_latency_ms <= 0:
            object.__setattr__(
                self,
                "max_latency_ms",
                _DEFAULT_LATENCY_MS.get(self.access_mode, 8000),
            )


@dataclass(frozen=True)
class PermissionBoundaryCase:
    id: str
    scope_id: str
    access_mode: str
    query: str
    expect_denied: bool = True
    deny_substrings: Tuple[str, ...] = ()
    max_latency_ms: int = field(default_factory=lambda: _DEFAULT_LATENCY_MS["deny"])
    use_legacy_scope: bool = False
    description: str = ""


@dataclass
class EvalRunResult:
    case_id: str
    path: str
    quality_pass: bool
    quality_reason: str
    latency_ms: float
    latency_pass: bool
    turn_outcome: str
    denied: bool
    optional_seed: bool = False

    @property
    def pass_all(self) -> bool:
        if self.optional_seed and not self.quality_pass:
            return self.latency_pass
        return self.quality_pass and self.latency_pass


QUALITY_CASES: List[QueryQualityCase] = [
    # S6 (qq-catalog-18): SUITE-GRAPH — protects: relations that exist in the
    # graph are consulted at query time. G1 is the Shortfall probe verbatim.
    QueryQualityCase("G1", "Who works on Topos with me?", "relationship_context:read", "summary",
                     eval_g1_graph_consulted, engine_only=True,
                     description="Graph lane consulted; relation items name entities, not fragments"),
    QueryQualityCase("G2", "What is connected to Topos?", "graph:read", "summary",
                     eval_g2_graph_direct, optional_seed=True, engine_only=True,
                     description="Direct graph:read structure ask returns edge relations"),
    QueryQualityCase("Q1", "UMA scopes and signal extraction", "ai_conversations:read", "summary", eval_q1_scopes,
                     description="Query-aware summary ranks scope/signal-extraction topics first"),
    # qq-catalog-5: Q2 re-scoped ai_conversations→messages. The owner's keycloak
    # material lives in messages (1 canonical row, 11 indexed chunks); within
    # ai_conversations the honest answer is "unknown" — the old pass was the
    # forced-yes/no prompt answering "yes" over an empty evidence packet.
    QueryQualityCase("Q2", "Voice transcription in the terminal", "messages:read", "inference", eval_q2_keycloak,
                     description="Inference yes/no with confidence > 0.5 (voxterm transcript evidence in messages)"),
    QueryQualityCase("Q3", "Work goals and projects", "work_context:read", "summary", eval_q3_work_goals,
                     description="Work scope returns goal/project summaries"),
    QueryQualityCase("Q4", "Collaborators on coding work", "relationship_context:read", "inference", eval_q4_collaborators,
                     description="Relationship inference returns list or yes/no"),
    QueryQualityCase("Q5", "Edtech pilot programs in Austin", "ai_conversations:read", "summary", eval_q5_illustration,
                     description="Distinct from Q1 — edtech/pilot clusters (real corpus, df>0)"),
    QueryQualityCase("Q6", "git GitHub messages", "ai_conversations:read", "raw", eval_q6_git_raw,
                     description="Raw mode returns git-related canonical rows", optional_seed=True),
    # D-series: dense-intelligence probes (entity spine, stats layer, fusion
    # diversity). These measure whether the intelligence-density upgrades
    # actually surface in query responses.
    QueryQualityCase("D1", "Tell me about Topos", "work_context:read", "summary", eval_d1_entity_dossier,
                     description="Entity spine surfaces the Topos dossier + mentions"),
    QueryQualityCase("D2", "Which places do I visit most often?", "places:read", "summary", eval_d2_stat_insight,
                     description="Aggregate intent routes to stat insights (place visits, live scope)"),
    QueryQualityCase("D3", "What have I been working on lately?", "work_context:read", "summary", eval_d3_retrieval_diversity,
                     description="Work-context working-on ask fuses >=3 sources incl. authored user_goal"),
    QueryQualityCase(
        "D3M",
        "What goals have I mentioned in my messages?",
        "messages:read",
        "summary",
        eval_d3m_messenger_goals,
        description="B11: messages:read fuses authored user_goal from messenger corpus",
    ),
    QueryQualityCase("D4", "Who is Marcus?", "relationship_context:read", "summary", eval_d4_person_dossier,
                     description="Person query returns dossier + supporting mentions (Marcus: real mention evidence)"),
]

PRIVACY_CASES: List[QueryQualityCase] = [
    QueryQualityCase(
        "P1",
        "Keycloak authentication setup",
        "ai_conversations:read",
        "inference",
        eval_no_forbidden_inference_keys,
        max_latency_ms=_DEFAULT_LATENCY_MS["inference"],
        description="Inference public_result must not expose evidence trail",
    ),
]

PERMISSION_CASES: List[PermissionBoundaryCase] = [
    PermissionBoundaryCase(
        "PB1",
        "work_context:read",
        "inference",
        "what are my work goals",
        deny_substrings=("mode_ceiling", "ceiling"),
        description="Work context summary ceiling blocks inference at retrieval",
    ),
    PermissionBoundaryCase(
        "PB2",
        "aiMessages:read",
        "summary",
        "legacy scope probe",
        use_legacy_scope=True,
        deny_substrings=("legacy", "deprecated", "unknown"),
        description="Legacy scope IDs rejected at manifest resolution",
    ),
    PermissionBoundaryCase(
        "PB3",
        "relationship_context:read",
        "inference",
        "collaborators",
        expect_denied=False,
        max_latency_ms=_DEFAULT_LATENCY_MS["inference"],
        description="Relationship inference allowed when scope is granted (owner path)",
    ),
]

LIVE_DB_PATH = Path(os.environ.get("TOPOS_DATABASE_PATH", Path.home() / ".topos" / "database.db"))


def manifest_for_scope(scope_id: str):
    return resolve_scope_manifest(scope_id)


def latency_budget_ms(access_mode: str, *, denied: bool = False) -> int:
    if denied:
        return _DEFAULT_LATENCY_MS["deny"]
    return _DEFAULT_LATENCY_MS.get(access_mode, 8000)


# --------------------------------------------------------------------------
# S7 SUITE-P — the aggregate verb, graded on EXACT numbers.
#
# protects: counting-class questions get exact answers the retrieval stack
# structurally cannot produce, and the claim is measured, not assumed: each
# case carries a `necessity_query` — the same question phrased naturally —
# that the runner sends through TODAY'S inference lane on the SAME corpus.
# The old lane's failure rate on these is the verb's justification; if it
# somehow passes at scale, the verb was unnecessary and the plan says so
# (the S7 kill-switch).
#
# Every expected number is a constructed fact of apply_aggregate_seed.py.


@dataclass(frozen=True)
class AggregateCase:
    id: str
    payload: Dict[str, Any]          # aggregate-verb arguments
    expect: Dict[str, float]         # group/bucket key -> exact value ("" = scalar)
    necessity_query: str             # the natural phrasing for the old-lane leg
    necessity_scope: str
    description: str = ""
    max_latency_ms: int = 2000       # deterministic SQL — an order faster than retrieval


def evaluate_aggregate_result(case: "AggregateCase", public_result: Dict[str, Any]) -> Tuple[bool, str]:
    rows = public_result.get("rows")
    if not isinstance(rows, list):
        return False, "no rows in public_result"
    got: Dict[str, float] = {}
    for row in rows:
        key = str(row.get("label") or row.get("bucket") or row.get("group") or "")
        got[key] = row.get("value")
    for key, want in case.expect.items():
        if key not in got:
            return False, f"missing key {key!r} (got {sorted(got)[:6]})"
        if abs(float(got[key]) - float(want)) > 1e-6:
            return False, f"{key!r}: expected {want}, got {got[key]}"
    return True, f"{len(case.expect)} exact value(s) matched"


def necessity_answer_contains(case: "AggregateCase", response: Dict[str, Any]) -> Tuple[bool, str]:
    """Old-lane rubric: does the answer state ANY of the exact expected numbers?

    Deliberately generous to the old lane (comma-tolerant, any expected value
    counts, confidence ignored) — the necessity claim must survive the most
    charitable reading of the incumbent.
    """
    blob = json.dumps(response, ensure_ascii=False, default=str)
    for want in case.expect.values():
        variants = {f"{want:g}", f"{int(want):,}" if float(want).is_integer() else f"{want:g}"}
        if any(v in blob for v in variants):
            return True, f"answer contains {want:g}"
    return False, "no expected number appears anywhere in the response"


AGGREGATE_CASES: List[AggregateCase] = [
    AggregateCase(
        "P-01", {"scope_id": "messages:read", "measure": "count"}, {"": 5200},
        "How many messages did I send in total?", "messages:read",
        description="Scalar count over 5,200 seeded rows"),
    AggregateCase(
        "P-02", {"scope_id": "messages:read", "measure": "count", "group_by": "person"},
        {"Casey Verano": 3000, "Ana Torres": 1700, "+15125550199": 500},
        "How many messages did I exchange with Casey Verano?", "messages:read",
        description="The alias trap at scale: two contact rows fold to one human"),
    AggregateCase(
        "P-03", {"scope_id": "messages:read", "measure": "count", "bucket": "hour_of_day"},
        {"23": 400},
        "How many late-night messages did I send?", "messages:read",
        description="Late-nights: the hour-23 block"),
    AggregateCase(
        "P-04", {"scope_id": "resources:read", "measure": "sum", "field": "amount",
                 "group_by": "category",
                 "since": "2026-03-01T00:00:00", "until": "2026-03-31T23:59:59"},
        {"groceries": 1000.0, "utilities": 800.0, "dining": 600.0},
        "How much did I spend on groceries in March?", "resources:read",
        description="Jordan F-02: March spending by category"),
    AggregateCase(
        "P-05", {"scope_id": "resources:read", "measure": "sum", "field": "amount",
                 "group_by": "category"},
        {"savings": 2000.0},
        "How much did I transfer to savings?", "resources:read",
        description="Jordan F-04: savings transfers sum to 2000"),
    AggregateCase(
        "P-06", {"scope_id": "resources:read", "measure": "avg", "field": "amount",
                 "group_by": "category"},
        {"income": 12000.0},
        "What is my approximate monthly income?", "resources:read",
        description="Jordan F-05: income rows average 12000"),
    AggregateCase(
        "P-07", {"scope_id": "health:read", "measure": "count", "group_by": "mood_tag"},
        {"calm": 25, "anxious": 15, "hopeful": 12, "energized": 8},
        "How many of my journal entries were calm?", "health:read",
        description="Jordan B-01's mood distribution, as exact counts"),
    AggregateCase(
        "P-08", {"scope_id": "schedule:read", "measure": "count",
                 "since": "2026-08-24T00:00:00", "until": "2026-08-28T23:59:59"},
        {"": 30},
        "How many calendar events did I have the week of August 24?", "schedule:read",
        description="OS-03's count skeleton on an absolute window"),
    AggregateCase(
        "P-09", {"scope_id": "activity:read", "measure": "count", "bucket": "day",
                 "since": "2026-07-01T00:00:00", "until": "2026-07-01T23:59:59"},
        {"2026-07-01": 40},
        "How many browsing events did I log on July 1st?", "activity:read",
        description="Load: a constructed 40-events day"),
]
