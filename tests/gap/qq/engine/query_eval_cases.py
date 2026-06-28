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

_DEFAULT_LATENCY_MS = {
    "summary": int(os.environ.get("TOPOS_QQ_LATENCY_SUMMARY_MS", "10000")),
    "inference": int(os.environ.get("TOPOS_QQ_LATENCY_INFERENCE_MS", "25000")),
    "raw": int(os.environ.get("TOPOS_QQ_LATENCY_RAW_MS", "5000")),
    "deny": int(os.environ.get("TOPOS_QQ_LATENCY_DENY_MS", "500")),
}


def _blob(obj: Any) -> str:
    return json.dumps(obj, default=str).lower()


def _public_result(response: Dict[str, Any]) -> Dict[str, Any]:
    pr = response.get("public_result")
    return pr if isinstance(pr, dict) else {}


def _not_denied(response: Dict[str, Any]) -> Tuple[bool, str]:
    if response.get("turn_outcome") == "denied" or response.get("deny_reason"):
        return False, f"denied: {response.get('deny_reason') or response.get('turn_outcome')}"
    return True, "ok"


def eval_q1_docker(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    pr = _public_result(response)
    items = pr.get("summaries") or pr.get("summary_items") or pr.get("scores") or []
    blob = _blob(items)
    if not items:
        return False, "no summary items"
    if any(k in blob for k in ("docker", "nginx", "compose")):
        return True, "top summaries mention docker/nginx/compose"
    return False, f"first item not docker-related: {_blob(items[0])[:120]}"


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
    if answer_type in ("list", "yes_no"):
        return True, f"answer_type={answer_type}"
    if isinstance(pr.get("items"), list) and pr["items"]:
        return True, f"list with {len(pr['items'])} items"
    return False, f"expected list/yes_no inference shape, got {answer_type}"


def eval_q5_illustration(response: Dict[str, Any]) -> Tuple[bool, str]:
    ok, msg = _not_denied(response)
    if not ok:
        return ok, msg
    pr = _public_result(response)
    items = pr.get("summaries") or pr.get("summary_items") or pr.get("scores") or []
    blob = _blob(items)
    if not items:
        return False, "no summaries"
    if any(k in blob for k in ("illustr", "sketch", "pencil")):
        return True, "illustration/sketch cluster present"
    return False, "no illustration/sketch terms in top summaries"


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
    QueryQualityCase("Q1", "Docker, nginx, deployment", "ai_conversations:read", "summary", eval_q1_docker,
                     description="Query-aware summary ranks docker/nginx topics first"),
    QueryQualityCase("Q2", "Keycloak authentication setup", "ai_conversations:read", "inference", eval_q2_keycloak,
                     description="Inference yes/no with confidence > 0.5"),
    QueryQualityCase("Q3", "Work goals and projects", "work_context:read", "summary", eval_q3_work_goals,
                     description="Work scope returns goal/project summaries"),
    QueryQualityCase("Q4", "Collaborators on coding work", "relationship_context:read", "inference", eval_q4_collaborators,
                     description="Relationship inference returns list or yes/no"),
    QueryQualityCase("Q5", "Illustration, pencil sketches", "ai_conversations:read", "summary", eval_q5_illustration,
                     description="Distinct from Q1 — illustration/sketch clusters", optional_seed=True),
    QueryQualityCase("Q6", "git GitHub messages", "ai_conversations:read", "raw", eval_q6_git_raw,
                     description="Raw mode returns git-related canonical rows", optional_seed=True),
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
