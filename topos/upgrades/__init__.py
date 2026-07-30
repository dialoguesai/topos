"""Per-release upgrade manifests: what each version invalidates.

Schema migrations (storage/db/migrations) handle SHAPE; these manifests handle
DERIVED DATA — the layers whose extractor logic changed in a release and must
be recomputed from raw/canonical data that itself never moves. The 1.2.0
motivating case: the NER wordpiece stitcher changed what entity extraction
produces, so mentions extracted by 1.1.0 are stale even though every message
row is untouched.

The manifest is the reviewed, single source of truth for "what does upgrading
to X require":

  * a future STARTUP RE-DERIVATION JOB diffs the node's stamped version
    against the shipped version and runs `steps_between(installed, shipped)` —
    multi-hop upgrades (1.0 → 1.2) union every intervening release's steps by
    construction;
  * release notes and the fleet dashboard read the same file, so "what will
    this upgrade do to my node" is never tribal knowledge.

Steps must be IDEMPOTENT and RESUMABLE — upgrade jobs get interrupted by
restarts (observed live on 2026-07-10: a node restart mid re-extraction left
95% of mentions missing with nothing reporting the gap).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_MANIFESTS_PATH = Path(__file__).with_name("manifests.json")

STEP_KINDS = frozenset({
    # POST /v1/enrichment/process {source_id?, job_names, force_reprocess} per
    # affected source — the heavy lane (re-runs extraction models).
    "enrichment_reprocess",
    # A single idempotent engine endpoint call (e.g. the graph rebuild).
    "engine_endpoint",
    # Re-run raw→canonical (or canonical-only) via reprocess_source.
    "canonical_reprocess",
    # Rebuild derived layers (entity graph, topic clusters, timeline).
    "derived_rebuild",
    # Re-embed + rebuild ANN (vec0) as one unit.
    "reembed",
    # Nothing to run — entry documents that a change needs no data action.
    "none",
})


def _version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in str(version).strip().split("."))


UNRELEASED_VERSION = "unreleased"


def _validate_release(release: Dict[str, Any]) -> None:
    version = release.get("version")
    if not version:
        raise ValueError("release entry needs 'version'")
    if version != UNRELEASED_VERSION:
        _version_key(version)  # must parse as semver
    for step in release.get("steps", []):
        if step["kind"] not in STEP_KINDS:
            raise ValueError(
                f"unknown step kind {step['kind']!r} in release {version}"
            )
        if not step.get("id") or not step.get("why"):
            raise ValueError(f"step in release {version} needs 'id' and 'why'")


def load_manifests(*, include_unreleased: bool = False) -> List[Dict[str, Any]]:
    """Shipped release manifests, oldest → newest. Raises on malformed entries.

    The staging ``\"unreleased\"`` entry (PLAN §4d) is excluded by default so
    ``steps_between`` never executes in-flight PR work. Pass
    ``include_unreleased=True`` for cut_release / CI guards.
    """
    data = json.loads(_MANIFESTS_PATH.read_text(encoding="utf-8"))
    manifests = data["releases"]
    shipped: List[Dict[str, Any]] = []
    unreleased: Optional[Dict[str, Any]] = None
    for release in manifests:
        _validate_release(release)
        if release["version"] == UNRELEASED_VERSION:
            unreleased = release
            continue
        shipped.append(release)
    ordered = sorted(shipped, key=lambda r: _version_key(r["version"]))
    if include_unreleased and unreleased is not None:
        ordered.append(unreleased)
    return ordered


def load_unreleased() -> Optional[Dict[str, Any]]:
    """Return the staging unreleased entry, or None if absent."""
    data = json.loads(_MANIFESTS_PATH.read_text(encoding="utf-8"))
    for release in data.get("releases", []):
        if release.get("version") == UNRELEASED_VERSION:
            _validate_release(release)
            return release
    return None


def steps_between(installed: str, shipped: str) -> List[Dict[str, Any]]:
    """Union of re-derivation steps for installed < version <= shipped.

    Later releases' steps run after earlier ones; duplicate step ids keep the
    LATEST occurrence (a later release re-requiring a rebuild supersedes the
    earlier request — running it once at the end is sufficient).
    Staging ``unreleased`` is never included.
    """
    lo, hi = _version_key(installed), _version_key(shipped)
    ordered: List[Dict[str, Any]] = []
    for release in load_manifests(include_unreleased=False):
        v = _version_key(release["version"])
        if lo < v <= hi:
            ordered.extend(release.get("steps", []))
    # Keep the LAST occurrence of each step id, in last-occurrence order.
    result: List[Dict[str, Any]] = []
    seen: set = set()
    for step in reversed(ordered):
        if step["id"] in seen:
            continue
        seen.add(step["id"])
        result.append(step)
    result.reverse()
    return [s for s in result if s["kind"] != "none"]
