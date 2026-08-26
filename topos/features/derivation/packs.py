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


def first_party_pack_dirs() -> tuple:
    """The one directory whose packs may say `net_subject: allow` (D9).

    A pack that describes people other than the owner is, by construction, an ontology
    for deriving claims about third parties who never consented to this node. That is
    not a hostile edge case of a community pack; it is F5's own named red-team threat.
    So the answer is first-party forever — not "until we build signing", not "except for
    verified partners". Community packs stay welcome and are owner-subject only.

    ONE root, deliberately: the pack directory that ships inside the installed package,
    which is also the only directory production ever loads (`registry.bundled_pack_dir`,
    hardcoded at `derivation_job.py`). An earlier draft of this also blessed the repo
    catalog via `Path(__file__).parents[3] / "derivation-packs"`, so the rule would fire
    at edit time rather than at mirror time. That was wrong: `parents[3]` names a
    different thing in every topology, and measured 2026-08-26 it resolves to

        dev checkout   -> <repo>/derivation-packs           (the intended catalog)
        deploy-head    -> ~/.topos/derivation-packs         (user-writable app data,
                                                             sibling of database.db)
        uv tool wheel  -> <venv>/lib/python3.10/derivation-packs

    A trust boundary that relocates into the app-data directory when the engine is
    deployed is not a trust boundary. The repo catalog is therefore NOT first-party: if a
    catalog pack ever declares `allow`, loading it from the catalog fails until it is
    mirrored into `bundled_packs/`, and the mirror is exactly where the boundary belongs.
    """
    return (Path(__file__).resolve().parent / "bundled_packs",)


def is_first_party_pack(path: Path) -> bool:
    """Resolve BEFORE comparing, so a symlink planted in a first-party directory points
    at its real location and fails — trust belongs to the file, not to the name it is
    reachable by."""
    try:
        parent = Path(path).resolve().parent
    except OSError:  # unreadable path is not a first-party path
        return False
    return any(parent == d for d in first_party_pack_dirs())



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


#: --- the lens contract (L5-22) -------------------------------------------------
#:
#: A `synthesis[]` entry declares a LENS: a named computation over accumulated structure,
#: as opposed to a `predicates[]` entry, which declares an extraction from one record.
#: Grown in place rather than given a sibling block (D8, 2026-08-26) — the field already
#: meant "derive from accumulated evidence", it already holds 55 declarations, and nothing
#: consumes it yet, so widening it now is free and widening it later is a migration.
#:
#: There are TWO shapes, and conflating them is the first thing a validator gets wrong:
#: producers emit a value onto a predicate; reconcilers open a review and assert nothing,
#: so they carry no predicate, no inputs and no evidence floor.
PRODUCER_KINDS = ("pattern", "disposition", "trajectory", "rhythm", "stylometry",
                  "trend", "graph_labeling")
RECONCILER_KINDS = ("consistency_check", "reconciliation")
SYNTHESIS_KINDS = PRODUCER_KINDS + RECONCILER_KINDS

#: What a lens's output is ABOUT. The load-bearing addition: authorisation for an outward
#: write has to come from the predicate's declared axis, not from a routing string an
#: extractor produced per assertion (finding F10).
SUBJECT_AXES = ("owner", "person", "dyad", "circle", "network")
CALIBRATION_METHODS = ("fixed", "own_baseline", "population_quantile")
#: Narrow to wide. A lens may narrow its pack's default; it may never widen it.
DISCLOSURES = ("owner_only", "scoped", "public")

_DURATION_RE = re.compile(r"^(\d+)\s*([dwmy])$", re.I)
_TYPED_COUNT_RE = re.compile(r"^(\d+)_([a-z_]+)$", re.I)
_DAYS_PER = {"d": 1, "w": 7, "m": 30, "y": 365}


@dataclass
class MinEvidence:
    """The floor below which a lens abstains, normalised.

    Measured across the 55 shipped declarations: **21 distinct spellings** in three
    families — bare counts (`3`), durations (`'21d'`, `'6w'`), and counts carrying a unit
    (`'200_messages'`, `'5_goals'`). A validator that picked one family would have rejected
    40 of the 55, so this parses all three rather than legislating a winner.
    """

    count: Optional[int] = None
    days: Optional[int] = None
    unit: str = ""
    raw: Any = None

    @classmethod
    def parse(cls, value: Any) -> "MinEvidence":
        if value is None:
            return cls(raw=None)
        if isinstance(value, bool):  # bool is an int subclass; never a floor
            raise ValueError(f"min_evidence must not be a boolean: {value!r}")
        if isinstance(value, int):
            return cls(count=int(value), raw=value)
        text = str(value).strip()
        m = _DURATION_RE.match(text)
        if m:
            return cls(days=int(m.group(1)) * _DAYS_PER[m.group(2).lower()], raw=value)
        m = _TYPED_COUNT_RE.match(text)
        if m:
            return cls(count=int(m.group(1)), unit=m.group(2).lower(), raw=value)
        raise ValueError(f"unparseable min_evidence {value!r}")


@dataclass
class Lens:
    kind: str
    #: Always a list. 9 of the 55 shipped declarations name SEVERAL predicates from one
    #: computation (communication.style's stylometry pass fills six at once), 42 name one,
    #: and 4 name none because they are reconcilers. Normalising here rather than at every
    #: call site is the difference between a runtime that dispatches and one that guesses.
    predicates: List[str] = field(default_factory=list)
    inputs: List[str] = field(default_factory=list)
    min_evidence: MinEvidence = field(default_factory=MinEvidence)
    subject: str = "owner"
    over: str = ""
    calibration: Dict[str, Any] = field(default_factory=dict)
    null_model: str = ""
    coverage: Dict[str, Any] = field(default_factory=dict)
    disclosure: str = ""
    description: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_producer(self) -> bool:
        return self.kind in PRODUCER_KINDS

    @property
    def predicate(self) -> str:
        """The single-predicate case, which is most of them. Empty for reconcilers."""
        return self.predicates[0] if self.predicates else ""


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
    required_fields: Optional[List[str]] = None  # RETIRED (W2.2, 2026-08-26): parsed for compat, never enforced —
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
    #: The same block, parsed and validated. `synthesis` stays as the raw list so nothing
    #: reading it today changes behaviour; `lenses` is what the runtime will dispatch on.
    lenses: List[Lens] = field(default_factory=list)
    #: May this pack's predicates ever describe someone other than the owner?
    #: Default DENY. A pack authored to describe the owner must not start
    #: producing dossiers about third parties merely because it was enabled, and
    #: before this existed every enabled pack could do exactly that — including
    #: the special-class health.* packs.
    net_subject: str = "deny"
    #: Where this pack came from, decided ONCE at load and carried forward. Without it,
    #: first-party-ness is unrecoverable after loading — which matters because the
    #: compute-time half of D9 (L5-18) reads the registry, not the YAML on disk, and a
    #: derived table holding non-owner claims never passes through the fact writer.
    source_path: str = ""
    first_party: bool = False
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


def _load_lenses(entries: Any, pid: str, preds: Dict[str, Predicate],
                 pack_disclosure: str, pack_net_subject: str) -> List[Lens]:
    """Validate a pack's `synthesis[]` block into Lens objects.

    Strict for the same reason the predicate loader is: a half-validated lens computes a
    half-governed claim about a person. Lenient only where the contract has not been
    decided yet — `over`, `null_model` and `coverage` are recorded but not yet required,
    because requiring them would reject all 55 shipped declarations on the day they gain
    a meaning. What IS required is anything that governs a write.
    """
    out: List[Lens] = []
    for i, e in enumerate(entries or []):
        where = f"synthesis[{i}]"
        _require(isinstance(e, dict), pid, f"{where}: not a mapping")
        kind = str(e.get("kind") or "")
        _require(kind in SYNTHESIS_KINDS, pid, f"{where}: unknown kind {kind!r}")

        raw_pred = e.get("predicate")
        names = ([str(x) for x in raw_pred] if isinstance(raw_pred, list)
                 else ([str(raw_pred)] if raw_pred else []))
        if kind in PRODUCER_KINDS:
            _require(bool(names), pid, f"{where}: {kind} must name at least one predicate")
            for nm in names:
                _require(nm in preds, pid,
                         f"{where}: predicate {nm!r} is not declared by this pack")
        # Reconcilers open a review rather than asserting, so they carry no predicate,
        # no inputs and no evidence floor — 4 of the 55 shipped declarations are this
        # shape, and treating the two as one is the first thing a validator gets wrong.

        try:
            min_ev = MinEvidence.parse(e.get("min_evidence"))
        except ValueError as exc:
            raise PackValidationError(f"pack {pid}: {where}: {exc}") from exc

        subject = str(e.get("subject") or "owner").strip().lower()
        _require(subject in SUBJECT_AXES, pid, f"{where}: bad subject {subject!r}")
        # The rule that ties this contract to the consent plane. Authorisation for an
        # outward write is derived HERE, from a declaration, rather than from a routing
        # string an extractor produced per assertion (F10). A lens that says it describes
        # dyads or other people cannot live in a pack that has not claimed the right to.
        _require(subject == "owner" or pack_net_subject == "allow", pid,
                 f"{where}: subject {subject!r} describes non-owners, but this pack does "
                 f"not declare net_subject: allow")

        cal = e.get("calibration") or {}
        _require(isinstance(cal, dict), pid, f"{where}: calibration must be a mapping")
        if cal.get("method") is not None:
            _require(str(cal["method"]) in CALIBRATION_METHODS, pid,
                     f"{where}: bad calibration.method {cal.get('method')!r}")

        disclosure = str(e.get("disclosure") or "").strip().lower()
        if disclosure:
            _require(disclosure in DISCLOSURES, pid,
                     f"{where}: bad disclosure {disclosure!r}")
            # Narrow, never widen — the same ceiling rule the pack itself lives under.
            base = pack_disclosure if pack_disclosure in DISCLOSURES else "owner_only"
            _require(DISCLOSURES.index(disclosure) <= DISCLOSURES.index(base), pid,
                     f"{where}: disclosure {disclosure!r} is wider than the pack's {base!r}")

        inputs = e.get("inputs") or []
        _require(isinstance(inputs, list), pid, f"{where}: inputs must be a list")
        out.append(Lens(
            kind=kind, predicates=names, inputs=[str(x) for x in inputs],
            min_evidence=min_ev, subject=subject, over=str(e.get("over") or ""),
            calibration=cal, null_model=str(e.get("null_model") or ""),
            coverage=e.get("coverage") or {}, disclosure=disclosure,
            description=str(e.get("description") or ""), raw=e))
    return out


def load_pack(path: Path, known_namespaces: Optional[set] = None,
              *, trusted: bool = False) -> Pack:
    """Load and validate one pack.

    `trusted` is an explicit assertion by the CALLER that this directory ships with the
    engine. It exists for one reason: the repo catalog is where outward packs are authored,
    and the shipped copy is a mirror of it, so review and mirror tooling must be able to read
    a `net_subject: allow` pack from the source tree. Production never passes it — the
    derivation job loads `bundled_pack_dir()`, whose first-party-ness is DETECTED, not
    asserted. Keeping the assertion a named argument makes every trusting call site greppable
    rather than implicit in a path calculation.
    """
    d = yaml.safe_load(path.read_text())
    pid = str(d.get("pack") or path.stem)
    _require(bool(d.get("pack")), pid, "missing `pack` id")
    _require(d.get("sensitivity_class") in SENSITIVITY, pid, f"bad sensitivity_class {d.get('sensitivity_class')!r}")
    _require(d.get("role_policy") in ROLE_POLICIES, pid, f"bad role_policy {d.get('role_policy')!r}")
    _net_subject = str(d.get("net_subject") or "deny").strip().lower()
    _require(_net_subject in ("allow", "deny"), pid,
             f"net_subject must be 'allow' or 'deny', got {d.get('net_subject')!r}")
    _first_party = bool(trusted) or is_first_party_pack(path)
    _require(_net_subject == "deny" or _first_party, pid,
             "net_subject: allow is first-party only (D9) — only packs shipping with the engine "
             "may describe people other than the owner; community packs are owner-subject")
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
    _disclosure = str(d.get("disclosure_default") or "owner_only")
    _lenses = _load_lenses(d.get("synthesis"), pid, preds, _disclosure, _net_subject)
    if known_namespaces is not None:
        _require(ns not in known_namespaces, pid, f"namespace collision on {ns!r}")
        known_namespaces.add(ns)
    return Pack(
        pack=pid, version=str(d.get("version")), title=str(d.get("title")),
        sensitivity_class=d["sensitivity_class"], role_policy=d["role_policy"],
        disclosure_default=str(d.get("disclosure_default") or "owner_only"),
        routing=d.get("routing") or {}, predicates=preds,
        guidance=d.get("guidance") or {}, revision=d.get("revision") or {},
        synthesis=d.get("synthesis") or [], lenses=_lenses, net_subject=_net_subject,
        source_path=str(path), first_party=_first_party, raw=d)


def load_packs(directory: Path, only: Optional[List[str]] = None,
               *, trusted: bool = False) -> Dict[str, Pack]:
    """Load every pack YAML in a directory (skips _scales/vocab/INDEX).

    See `load_pack` for `trusted`. It is off by default so that the safe answer is the one
    you get by not thinking about it.
    """
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
        p = load_pack(f, known_namespaces=None, trusted=trusted)
        for name in p.predicates:
            for other in out.values():
                if name in other.predicates:
                    raise PackValidationError(f"predicate {name} declared by both {other.pack} and {p.pack}")
        out[p.pack] = p
    return out
