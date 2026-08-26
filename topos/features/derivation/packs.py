"""Ontology pack loader + validator (shadow-pilot subset of plan F1.1).

Packs are DECLARATIVE YAML artifacts (PLAN_DERIVATION_LAYER.md §1). This loader is
deliberately strict: a pack that fails validation does not load — there is no
"best effort" path, because a half-loaded ontology writes half-governed facts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

SENSITIVITY = ("none", "personal", "special")
ROLE_POLICIES = ("authored_only", "authored_addressed", "any_with_label")
ALTITUDES = ("stated", "inferred", "predicted")
TEMPORALS = ("interval", "episodic", "point")
CARDINALITIES = ("single", "multi")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$")


_SCALES_CACHE: Dict[str, List[str]] = {}


def load_scales(directory: Path) -> Dict[str, List[str]]:
    """Shared value scales (_scales.yaml). Packs reference them as {scale: freq_5}."""
    global _SCALES_CACHE
    if not _SCALES_CACHE:
        f = Path(directory) / "_scales.yaml"
        if f.exists():
            raw = yaml.safe_load(f.read_text()) or {}
            _SCALES_CACHE = {k: list(v) for k, v in (raw.get("scales") or {}).items()
                             if isinstance(v, list)}
    return _SCALES_CACHE


@dataclass
class Predicate:
    name: str
    value_type: str
    cardinality: str
    temporal: str
    altitude: str
    sensitivity: Optional[str] = None  # per-predicate override; effective = max(pack, this)
    values: Optional[List[str]] = None
    value_schema: Optional[Dict[str, Any]] = None
    qualifiers: Optional[Dict[str, Any]] = None
    required_fields: Optional[List[str]] = None  # a structured value MISSING one of these is
                                                 # not an instance of this predicate at all
                                                 # (a "habit" with no nameable cadence is a task)
    key_fields: Optional[List[str]] = None  # identity fields of a structured value (the rest is STATE);
    event_identity: str = "windowed:45"     # episodic dedup: once | windowed:<days> | dated
                                            # (retellings of one life event must corroborate, not multiply —
                                            #  measured 2026-08-26: one firing -> 6 events, one death -> 5)
                                            # e.g. rel.relationship keys on [person] so role/status changes
                                            # supersede instead of accumulating parallel truths
    note: str = ""


@dataclass
class Pack:
    pack: str
    version: str
    title: str
    sensitivity_class: str
    role_policy: str
    disclosure_default: str
    routing: Dict[str, Any]
    predicates: Dict[str, Predicate]
    guidance: Dict[str, Any]
    revision: Dict[str, Any] = field(default_factory=dict)
    synthesis: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def effective_sensitivity(self, predicate: str) -> str:
        p = self.predicates[predicate]
        order = {s: i for i, s in enumerate(SENSITIVITY)}
        pred_s = p.sensitivity or self.sensitivity_class
        return max(self.sensitivity_class, pred_s, key=lambda s: order[s])

    def allowed_roles(self) -> tuple:
        return {
            "authored_only": ("authored",),
            "authored_addressed": ("authored", "addressed"),
            # any_with_label: ambient/observed allowed but every fact carries the label
            "any_with_label": ("authored", "addressed", "participated", "observed", "ambient"),
        }[self.role_policy]


class PackValidationError(ValueError):
    pass


def _require(cond: bool, pack_id: str, msg: str) -> None:
    if not cond:
        raise PackValidationError(f"pack {pack_id}: {msg}")


def load_pack(path: Path, known_namespaces: Optional[set] = None) -> Pack:
    d = yaml.safe_load(path.read_text())
    pid = str(d.get("pack") or path.stem)
    _require(bool(d.get("pack")), pid, "missing `pack` id")
    _require(d.get("sensitivity_class") in SENSITIVITY, pid, f"bad sensitivity_class {d.get('sensitivity_class')!r}")
    _require(d.get("role_policy") in ROLE_POLICIES, pid, f"bad role_policy {d.get('role_policy')!r}")
    _require(bool(d.get("eval", {}).get("gold")), pid, "eval.gold is MANDATORY (no instrument, no install)")
    _require(bool(d.get("eval", {}).get("negative_controls")), pid, "eval.negative_controls is MANDATORY")
    _require(bool(d.get("consumers")), pid, "at least one consumer is required")
    ns = pid.split(".")[0]
    preds: Dict[str, Predicate] = {}
    for p in d.get("predicates") or []:
        name = str(p.get("name") or "")
        _require(bool(_NAME_RE.match(name)), pid, f"predicate name not namespaced: {name!r}")
        _require(p.get("cardinality") in CARDINALITIES, pid, f"{name}: bad cardinality")
        _require(p.get("temporal") in TEMPORALS, pid, f"{name}: bad temporal")
        ei = str(p.get("event_identity") or "windowed:45")
        _require(ei in ("once", "dated") or (ei.startswith("windowed:") and ei[9:].isdigit()),
                 pid, f"{name}: bad event_identity {ei}")
        _require(p.get("altitude") in ALTITUDES, pid, f"{name}: bad altitude")
        sens = p.get("sensitivity")
        _require(sens is None or sens in SENSITIVITY, pid, f"{name}: bad sensitivity override")
        preds[name] = Predicate(
            name=name, value_type=str(p.get("value_type")), cardinality=p["cardinality"],
            temporal=p["temporal"], altitude=p["altitude"], sensitivity=sens,
            values=p.get("values"), value_schema=p.get("value_schema"),
            qualifiers=p.get("qualifiers"), key_fields=p.get("key_fields"),
            event_identity=str(p.get("event_identity") or "windowed:45"),
            required_fields=p.get("required_fields"),
            note=str(p.get("note") or ""))
    _require(bool(preds), pid, "no predicates")
    if known_namespaces is not None:
        _require(ns not in known_namespaces, pid, f"namespace collision on {ns!r}")
        known_namespaces.add(ns)
    return Pack(
        pack=pid, version=str(d.get("version")), title=str(d.get("title")),
        sensitivity_class=d["sensitivity_class"], role_policy=d["role_policy"],
        disclosure_default=str(d.get("disclosure_default") or "owner_only"),
        routing=d.get("routing") or {}, predicates=preds,
        guidance=d.get("guidance") or {}, revision=d.get("revision") or {},
        synthesis=d.get("synthesis") or [], raw=d)


def load_packs(directory: Path, only: Optional[List[str]] = None) -> Dict[str, Pack]:
    """Load every pack YAML in a directory (skips _scales/vocab/INDEX)."""
    out: Dict[str, Pack] = {}
    seen_ns: set = set()
    load_scales(Path(directory))
    for f in sorted(Path(directory).glob("*.yaml")):
        if f.name.startswith("_"):
            continue
        if only and f.stem not in only:
            continue
        # namespaces legitimately repeat across packs (health.*, behavior.*) — collision
        # checking is per PREDICATE name, namespace set kept for telemetry only.
        p = load_pack(f, known_namespaces=None)
        for name in p.predicates:
            for other in out.values():
                if name in other.predicates:
                    raise PackValidationError(f"predicate {name} declared by both {other.pack} and {p.pack}")
        out[p.pack] = p
    return out
