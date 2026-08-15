"""The trained-head artifact and its loader — the seam rungs 2 and 3 drop into.

PLAN_SCOPE_CLASSIFIER.md §5 and §6.4. ``scope_classifier.classify`` prefers a trained
head when one is installed and falls back to prototype similarity when none is. Callers
see the same ``ScopeVerdict`` either way, so a model can land — or be pulled — without
touching the query path.

Three things this file refuses to do, each a load-time gate rather than a convention:

**Refuse a head whose training corpus names a non-public source.** §6.4 rule 3 says the
shipped model's manifest must be auditable, and "No training on your data"
(``securityContent.ts:54``) is a published claim. A manifest is checkable, so it is
checked: every corpus entry must be public-licensed or internally synthetic, and a head
carrying anything else will not load. That turns the claim into a mechanism.

**Refuse a head whose label set drifted from the live registry.** §6A.2 — a renamed
scope against a positionally-indexed head is silent catastrophic mis-routing, which is
the worst available failure next to a permission boundary. Legacy ids are refused
outright.

**Refuse a head that does not declare its own provenance.** No manifest, no load. An
artifact that cannot say what it was trained on cannot be audited, and an unauditable
model is exactly what the privacy architecture exists to prevent.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

FORMAT = "topos-scope-head-1"
ENV_HEAD_PATH = "TOPOS_SCOPE_HEAD"

#: Licences a training corpus may carry. Anything else means the head saw data we have
#: promised not to train on, or data we cannot ship a model from, and it will not load.
ALLOWED_LICENCE_PREFIXES = ("CC BY", "CC0", "Apache", "MIT", "BSD", "internal", "public domain")

#: Share-alike is checked FIRST and rejected, because "CC BY-SA-4.0".startswith("CC BY")
#: is True — the prefix list alone would have waved it through. PLAN §6.5a already ruled
#: CC BY-SA out for SGD: whether trained weights are a derivative of their training data
#: is legally unsettled, and some providers assert that they are. A gate that admits the
#: exact licence the plan rejected is worse than no gate, because it looks like one.
DENIED_LICENCE_MARKERS = ("-SA", " SA", "sharealike", "share-alike", "NonCommercial",
                          "non-commercial", "NC-", "-ND", "NoDeriv")

KIND_LINEAR = "linear"      # logistic weights over a frozen sentence embedding
KIND_ENCODER = "encoder"    # a fine-tuned encoder directory (rung 3)


class ScopeHeadError(RuntimeError):
    """A head exists but must not be used. Never swallow this — fall back loudly."""


@dataclass(frozen=True)
class ScopeHead:
    """A loaded head plus everything needed to audit and version it."""

    kind: str
    labels: Tuple[str, ...]
    embedding_model: str
    tau_high: float
    tau_low: float
    corpus_manifest: Dict[str, Any]
    metrics: Dict[str, Any]
    trained_at: str
    path: Path
    _predict: Callable[[Sequence[str]], List[List[float]]]

    def predict(self, texts: Sequence[str]) -> List[Dict[str, float]]:
        """Per-text ``{label: score}``. Scores are per-label, not a softmax — the task is
        multi-label ("am I free Friday according to my calendar" is two scopes)."""
        return [
            {label: float(score) for label, score in zip(self.labels, row)}
            for row in self._predict(list(texts))
        ]


def default_head_path() -> Path:
    override = os.environ.get(ENV_HEAD_PATH)
    if override:
        return Path(override)
    return Path.home() / ".topos" / "models" / "scope_head"


def _check_manifest(manifest: Dict[str, Any]) -> None:
    corpora = manifest.get("corpora")
    if not corpora:
        raise ScopeHeadError(
            "head declares no training corpus — an artifact that cannot say what it was "
            "trained on cannot be audited, and 'No training on your data' is a published "
            "claim (PLAN §6.4 rule 3)"
        )
    for entry in corpora:
        licence = str(entry.get("licence") or "")
        denied = [m for m in DENIED_LICENCE_MARKERS if m.lower() in licence.lower()]
        if denied:
            raise ScopeHeadError(
                f"head was trained on {entry.get('source')!r} under {licence!r}, which is "
                f"share-alike / non-commercial / no-derivatives. PLAN §6.5a rejected this "
                f"licence class for SGD and the same reasoning applies here: refusing to load."
            )
        if not licence.startswith(ALLOWED_LICENCE_PREFIXES):
            raise ScopeHeadError(
                f"head was trained on {entry.get('source')!r} under licence {licence!r}, "
                f"which is not a public or synthetic source. Refusing to load: this is "
                f"the mechanism behind 'No training on your data'."
            )


def _check_labels(labels: Sequence[str]) -> None:
    from .scope_classifier import live_scope_ids
    from .scope_registry_loader import LEGACY_SCOPE_IDS

    if not labels:
        raise ScopeHeadError("head declares no labels")
    legacy = sorted(set(labels) & LEGACY_SCOPE_IDS)
    if legacy:
        raise ScopeHeadError(f"head emits legacy scope ids {legacy} (PLAN §6A.2)")
    live = set(live_scope_ids())
    unknown = sorted(set(labels) - live)
    if unknown:
        raise ScopeHeadError(
            f"head emits {unknown}, absent from the live registry — the taxonomy moved "
            f"under a trained artifact. Retrain against the current {len(live)} scopes "
            f"rather than remapping at load time (PLAN §6A.2)."
        )


def _linear_predictor(
    path: Path, meta: Dict[str, Any], embed: Callable[[Sequence[str], str], List[List[float]]]
) -> Callable[[Sequence[str]], List[List[float]]]:
    import numpy as np

    blob = np.load(path / "weights.npz")
    coef, intercept = blob["coef"], blob["intercept"]

    def _predict(texts: Sequence[str]) -> List[List[float]]:
        vectors = np.asarray(embed(list(texts), "query"), dtype=float)
        if vectors.size == 0:
            return []
        logits = vectors @ coef.T + intercept
        return (1.0 / (1.0 + np.exp(-logits))).tolist()

    return _predict


def _encoder_predictor(
    path: Path, meta: Dict[str, Any]
) -> Callable[[Sequence[str]], List[List[float]]]:
    """Rung 3: a fine-tuned sequence classifier, slot-cached like every other local model.

    Multi-label, so the output is a per-label sigmoid rather than a softmax — "am I free
    Friday according to my calendar" is two scopes, and a softmax would force a choice.
    """
    import torch  # noqa: PLC0415 — heavy, and only rung 3 needs it
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    from ..engine.model_cache import ModelSlot, get_model_cache

    model_dir = str(path / "model")
    max_length = int(meta.get("max_length", 64))

    def _load():
        tokenizer = AutoTokenizer.from_pretrained(model_dir)
        model = AutoModelForSequenceClassification.from_pretrained(model_dir)
        model.eval()
        return model, tokenizer

    handle, _ = get_model_cache().acquire(ModelSlot.SCOPE_HEAD, model_dir, _load)
    model, tokenizer = handle

    def _predict(texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        batch = tokenizer(
            list(texts), padding=True, truncation=True,
            max_length=max_length, return_tensors="pt",
        )
        with torch.no_grad():
            logits = model(**batch).logits
        return torch.sigmoid(logits).tolist()

    return _predict


def save_encoder_head(
    path: Path,
    *,
    labels: Sequence[str],
    model: Any,
    tokenizer: Any,
    base_model: str,
    corpus_manifest: Dict[str, Any],
    metrics: Optional[Dict[str, Any]] = None,
    tau_high: float = 0.5,
    tau_low: float = 0.3,
    max_length: int = 64,
    trained_at: str = "",
) -> Path:
    """Persist a fine-tuned encoder head. Validates BEFORE writing, as the linear one does.

    A fine-tune is expensive; discovering the manifest is dirty after saving 265 MB is
    the wrong order.
    """
    _check_labels(labels)
    _check_manifest(corpus_manifest)
    path.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(path / "model")
    tokenizer.save_pretrained(path / "model")
    (path / "head.json").write_text(
        json.dumps(
            {
                "format": FORMAT,
                "kind": KIND_ENCODER,
                "labels": list(labels),
                "base_model": base_model,
                "embedding_model": "",
                "tau_high": tau_high,
                "tau_low": tau_low,
                "max_length": max_length,
                "corpus_manifest": corpus_manifest,
                "metrics": metrics or {},
                "trained_at": trained_at,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def load_head(
    path: Optional[Path] = None,
    *,
    embed: Optional[Callable[[Sequence[str], str], List[List[float]]]] = None,
) -> Optional[ScopeHead]:
    """Load an installed head, or ``None`` when none is installed.

    ``None`` for "not installed" is the normal, silent case — no head is the shipping
    default. ``ScopeHeadError`` for "installed but unusable" is deliberately loud: a
    refused head is a fact the operator needs, not a fallback to paper over.
    """
    root = Path(path) if path else default_head_path()
    meta_file = root / "head.json"
    if not meta_file.is_file():
        return None

    meta = json.loads(meta_file.read_text("utf-8"))
    if meta.get("format") != FORMAT:
        raise ScopeHeadError(f"unknown head format {meta.get('format')!r}, expected {FORMAT}")

    labels = tuple(str(x) for x in (meta.get("labels") or ()))
    _check_labels(labels)
    _check_manifest(meta.get("corpus_manifest") or {})

    kind = str(meta.get("kind") or "")
    if embed is None:
        from .scope_classifier import _default_embed

        embed = _default_embed

    if kind == KIND_LINEAR:
        predictor = _linear_predictor(root, meta, embed)
    elif kind == KIND_ENCODER:
        predictor = _encoder_predictor(root, meta)
    else:
        raise ScopeHeadError(f"unknown head kind {kind!r}")

    return ScopeHead(
        kind=kind,
        labels=labels,
        embedding_model=str(meta.get("embedding_model") or ""),
        tau_high=float(meta.get("tau_high", 0.5)),
        tau_low=float(meta.get("tau_low", 0.3)),
        corpus_manifest=dict(meta.get("corpus_manifest") or {}),
        metrics=dict(meta.get("metrics") or {}),
        trained_at=str(meta.get("trained_at") or ""),
        path=root,
        _predict=predictor,
    )


def save_linear_head(
    path: Path,
    *,
    labels: Sequence[str],
    coef: Any,
    intercept: Any,
    embedding_model: str,
    corpus_manifest: Dict[str, Any],
    metrics: Optional[Dict[str, Any]] = None,
    tau_high: float = 0.5,
    tau_low: float = 0.3,
    trained_at: str = "",
) -> Path:
    """Persist a linear head. Validates before writing, so a bad artifact never lands."""
    import numpy as np

    _check_labels(labels)
    _check_manifest(corpus_manifest)
    path.mkdir(parents=True, exist_ok=True)
    np.savez(path / "weights.npz", coef=np.asarray(coef), intercept=np.asarray(intercept))
    (path / "head.json").write_text(
        json.dumps(
            {
                "format": FORMAT,
                "kind": KIND_LINEAR,
                "labels": list(labels),
                "embedding_model": embedding_model,
                "tau_high": tau_high,
                "tau_low": tau_low,
                "corpus_manifest": corpus_manifest,
                "metrics": metrics or {},
                "trained_at": trained_at,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def describe(head: Optional[ScopeHead]) -> Dict[str, Any]:
    """What is installed, for a status surface. Safe to log — no weights, no text."""
    if head is None:
        return {"installed": False, "source": "prototype"}
    return {
        "installed": True,
        "source": "head",
        "kind": head.kind,
        "labels": len(head.labels),
        "embedding_model": head.embedding_model,
        "trained_at": head.trained_at,
        "corpora": [e.get("source") for e in head.corpus_manifest.get("corpora") or []],
        "metrics": head.metrics,
    }
