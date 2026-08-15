"""HuggingFace Hub model resolution for the Enrichment Lab playground.

Given a pasted model reference ("org/model", "hf:org/model", or a full
huggingface.co URL), fetch hub metadata (pipeline tag, library, weight size,
downloads, gated/private flags) and map the hub task onto compatible
enrichment jobs so the UI can suggest where the model fits.

Results are cached in-process for a short TTL. Hub outages degrade to
format-validation only (status "unreachable") so the Lab remains usable
offline with already-downloaded models.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("topos.enrichment_lab.model_resolve")

# "org/model" plus legacy root-level ids ("gpt2", "distilbert-base-uncased-...").
HF_MODEL_ID_RE = re.compile(r"^[\w.\-]+(/[\w.\-]+)?$")

HUB_API_BASE = "https://huggingface.co/api/models"
RESOLVE_TIMEOUT_SECONDS = 8.0
_CACHE_TTL_SECONDS = 600.0

# Hub pipeline tags acceptable for a job's hf_task beyond an exact match.
# sentence-transformers embedding models are usually tagged sentence-similarity.
_TASK_COMPAT: Dict[str, Tuple[str, ...]] = {
    "text-classification": ("text-classification",),
    "token-classification": ("token-classification",),
    "feature-extraction": ("feature-extraction", "sentence-similarity"),
    "zero-shot-classification": ("zero-shot-classification", "text-classification"),
}

_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".h5", ".msgpack", ".onnx", ".gguf")

_KNOWN_LIBRARIES = ("transformers", "sentence-transformers")

_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def normalize_model_id(raw: str) -> Optional[str]:
    """Normalize pasted input to a hub model id, or None if it can't be one.

    Accepts 'org/model', legacy root-level ids ('gpt2', 'distilbert-base-...'),
    'hf:org/model', and huggingface.co URLs (with or without scheme,
    optionally with /tree/main etc. suffixes).
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("hf:"):
        text = text[3:].strip()
    lowered = text.lower()
    for prefix in ("https://huggingface.co/", "http://huggingface.co/", "huggingface.co/"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.strip().strip("/")
    parts = [p for p in text.split("/") if p]
    if not parts:
        return None
    candidate = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
    if HF_MODEL_ID_RE.match(candidate):
        return candidate
    return None


def _lab_jobs_with_hf_task() -> List[Any]:
    from ..enrichment.catalog import get_enrichment_catalog

    return [
        entry
        for entry in get_enrichment_catalog().values()
        if entry.supports_lab and entry.hf_task
    ]


def compatible_jobs_for_pipeline_tag(pipeline_tag: Optional[str]) -> List[Dict[str, Any]]:
    """Enrichment jobs a hub model with this pipeline tag can back.

    match "exact" means the tag equals the job's hf_task; "compatible" means
    the runtime can still load it for that job (alias tasks).
    """
    tag = str(pipeline_tag or "").strip()
    if not tag:
        return []
    out: List[Dict[str, Any]] = []
    for entry in _lab_jobs_with_hf_task():
        accepted = _TASK_COMPAT.get(entry.hf_task or "", (entry.hf_task,))
        if tag == entry.hf_task:
            out.append({"job_id": entry.job_id, "title": entry.title, "match": "exact"})
        elif tag in accepted:
            out.append({"job_id": entry.job_id, "title": entry.title, "match": "compatible"})
    out.sort(key=lambda item: (item["match"] != "exact", item["job_id"]))
    return out


def task_compatibility(job_id: str, pipeline_tag: Optional[str]) -> Optional[bool]:
    """True/False when we can judge tag-vs-job fit, None when unknown.

    None (job without hf_task, or unknown tag) must not block a run: the Lab
    should stay permissive when the hub is unreachable or metadata is missing.
    """
    from ..enrichment.catalog import get_catalog_entry

    tag = str(pipeline_tag or "").strip()
    if not tag:
        return None
    entry = get_catalog_entry(job_id)
    if not entry or not entry.hf_task:
        return None
    accepted = _TASK_COMPAT.get(entry.hf_task, (entry.hf_task,))
    return tag in accepted


def format_size(num_bytes: Optional[int]) -> Optional[str]:
    if not num_bytes or num_bytes <= 0:
        return None
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return None


def _weight_size_bytes(siblings: List[Dict[str, Any]]) -> Optional[int]:
    """Estimate the on-disk weight size of what Topos will actually download.

    Repos often ship the same weights in several formats (safetensors, bin,
    h5, onnx, ...). The runtime loads safetensors when present, else bin, so
    prefer those groups instead of summing every duplicate format.
    """
    safetensors = 0
    torch_bin = 0
    other = 0
    for sibling in siblings or []:
        name = str(sibling.get("rfilename") or "")
        size = sibling.get("size")
        if not isinstance(size, (int, float)):
            continue
        if name.endswith(".safetensors"):
            safetensors += int(size)
        elif name.endswith(".bin"):
            torch_bin += int(size)
        elif name.endswith(_WEIGHT_SUFFIXES):
            other += int(size)
    return safetensors or torch_bin or other or None


def _result(status: str, model_id: Optional[str], **extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "status": status,
        "model_id": model_id,
        "pipeline_tag": None,
        "library_name": None,
        "downloads": None,
        "likes": None,
        "gated": None,
        "private": None,
        "size_bytes": None,
        "size_human": None,
        "compatible_jobs": [],
        "hub_reachable": status not in ("unreachable",),
        "warnings": [],
    }
    base.update(extra)
    return base


def resolve_model(raw_model_id: str, *, use_cache: bool = True) -> Dict[str, Any]:
    """Resolve a pasted model reference against the HuggingFace Hub."""
    model_id = normalize_model_id(raw_model_id)
    if not model_id:
        return _result(
            "invalid",
            None,
            warnings=[
                "Expected a HuggingFace model id like 'org/model-name' "
                "(you can paste the model page URL too)."
            ],
        )

    now = time.monotonic()
    if use_cache:
        cached = _cache.get(model_id)
        if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
            return dict(cached[1])

    result = _fetch_from_hub(model_id)
    if use_cache and result.get("status") in ("ok", "not_found", "unauthorized"):
        _cache[model_id] = (now, dict(result))
    return result


def _fetch_from_hub(model_id: str) -> Dict[str, Any]:
    import httpx

    url = f"{HUB_API_BASE}/{model_id}?blobs=true"
    try:
        response = httpx.get(
            url,
            timeout=RESOLVE_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 — any transport failure degrades gracefully
        logger.info("HF Hub unreachable while resolving %s: %s", model_id, exc)
        return _result(
            "unreachable",
            model_id,
            warnings=[
                "Could not reach the HuggingFace Hub; the id format looks valid, "
                "but task compatibility and size could not be checked."
            ],
        )

    if response.status_code == 404:
        return _result(
            "not_found",
            model_id,
            warnings=[f"Model '{model_id}' was not found on the HuggingFace Hub."],
        )
    if response.status_code in (401, 403):
        # The hub answers 401 for gated, private, AND nonexistent repos.
        return _result(
            "unauthorized",
            model_id,
            gated=True,
            warnings=[
                f"'{model_id}' is not accessible: it may not exist, or it is "
                "gated/private (requires accepting its license and an HF token). "
                "Check the id on huggingface.co."
            ],
        )
    if response.status_code != 200:
        return _result(
            "unreachable",
            model_id,
            warnings=[f"HuggingFace Hub returned HTTP {response.status_code}."],
        )

    try:
        data = response.json()
    except ValueError:
        return _result(
            "unreachable",
            model_id,
            warnings=["HuggingFace Hub returned an unparseable response."],
        )

    pipeline_tag = data.get("pipeline_tag")
    library_name = data.get("library_name")
    gated_raw = data.get("gated")
    gated = bool(gated_raw) if gated_raw is not None else False
    size_bytes = _weight_size_bytes(data.get("siblings") or [])
    compatible = compatible_jobs_for_pipeline_tag(pipeline_tag)

    warnings: List[str] = []
    if gated:
        warnings.append(
            "This model is gated: you must accept its license on huggingface.co "
            "and configure an HF token before Topos can download it."
        )
    if library_name and library_name not in _KNOWN_LIBRARIES:
        warnings.append(
            f"Model library is '{library_name}'; Topos runs transformers and "
            "sentence-transformers models — this one may not load."
        )
    if pipeline_tag and not compatible:
        warnings.append(
            f"No enrichment uses the '{pipeline_tag}' task; this model can't back "
            "any current enrichment."
        )
    if not pipeline_tag:
        warnings.append(
            "The hub does not declare a task for this model, so compatibility "
            "could not be determined."
        )

    return _result(
        "ok",
        model_id,
        pipeline_tag=pipeline_tag,
        library_name=library_name,
        downloads=data.get("downloads"),
        likes=data.get("likes"),
        gated=gated,
        private=bool(data.get("private") or False),
        size_bytes=size_bytes,
        size_human=format_size(size_bytes),
        compatible_jobs=compatible,
        warnings=warnings,
    )


def clear_cache() -> None:
    _cache.clear()
