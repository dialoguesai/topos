"""Whether a derivation job's model is usable on this machine right now.

A job that cannot reach the model it needs has not failed because the code is
wrong. It failed because the machine is not ready yet, and it will succeed
unchanged once the model arrives. Telling those two apart is what lets recorded
debt WAIT for the model instead of being retried into the same wall and parked.

Per-job provider and model come from the model catalog (``MVP_JOB_SPECS``), not
a second hand-maintained list: the catalog is what actually binds a job to
ollama / huggingface / rules, so a job registered there is classified here
without anyone remembering to update a frozenset that lives somewhere else.

**Two questions, deliberately separate.** ``ready`` is the honest answer to "can
this run right now" and is what a person should be shown. ``blocking`` is the
much narrower "should queued work be HELD for this", and only a hard stop sets
it. They differ for HuggingFace: weights that are not cached make a job not
ready, but on a networked node the first run downloads them and succeeds, so
holding that job's debt would strand work that would have completed. Ollama is
a hard stop — nothing downloads a server that was never installed.

Jobs absent from the catalog (``facts``, ``topic_clusters``, ``statistics``,
``timeline``, ``attention_triage``, ``complexity_snapshot``) have no declared
provider and are reported ready. Some of them do use an LLM. Reporting "unknown"
as "not ready" would stall debts this module cannot classify, so it errs toward
attempting the work — which is what the code did before this module existed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: How long a probe is trusted. The sweep, the retry executor and the per-
#: dimension health computation all ask; without this, one health page would
#: open a socket and scan the HF cache once per job per dimension.
_PROBE_TTL_SECONDS = 30.0

#: Providers whose absence is a HARD stop — see the module docstring on why
#: huggingface is not one of them.
_BLOCKING_PROVIDERS = frozenset({"ollama"})

#: Weight formats the torch runtime never loads. Mirrors enrichment_lab.worker's
#: download-side list so "cached" here means the same thing it means there.
_SNAPSHOT_IGNORE_PATTERNS = ("*.h5", "*.msgpack", "*.tflite", "*.ot", "*.onnx")

_probe_at: float = 0.0
_probe_cache: Dict[str, str] = {}
_hf_cache: Dict[str, Tuple[float, bool]] = {}


@dataclass(frozen=True)
class JobReadiness:
    """What a job needs, and whether it has it."""

    job: str
    provider: Optional[str]
    model: str
    ready: bool
    blocking: bool
    reason: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "job": self.job,
            "provider": self.provider,
            "model": self.model,
            "ready": self.ready,
            "reason": self.reason,
        }


def _provider_status(*, force: bool = False) -> Dict[str, str]:
    """Service reachability, cached for ``_PROBE_TTL_SECONDS``."""
    global _probe_at, _probe_cache
    now = time.monotonic()
    if not force and _probe_cache and (now - _probe_at) < _PROBE_TTL_SECONDS:
        return dict(_probe_cache)
    try:
        from ..features.signal.data_health import check_provider_status

        _probe_cache = dict(check_provider_status())
    except Exception as exc:  # noqa: BLE001 — a broken probe is not readiness
        logger.debug("[DERIVE:READY] provider probe failed: %s", exc)
        _probe_cache = {}
    _probe_at = now
    return dict(_probe_cache)


def hf_model_cached(model: str, *, force: bool = False) -> bool:
    """True when the model snapshot is already in the local HF cache.

    This is the difference between "will run" and "will run after a few hundred
    megabytes arrive", which is the whole question on a machine that has just
    been set up offline.
    """
    key = str(model or "").strip()
    if not key:
        return False
    now = time.monotonic()
    hit = _hf_cache.get(key)
    if not force and hit and (now - hit[0]) < _PROBE_TTL_SECONDS:
        return hit[1]
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=key,
            local_files_only=True,
            ignore_patterns=list(_SNAPSHOT_IGNORE_PATTERNS),
        )
        cached = True
    except Exception:  # noqa: BLE001 — any miss means "not cached"
        cached = False
    _hf_cache[key] = (now, cached)
    return cached


def reset_probe_cache() -> None:
    """Drop every cached probe (tests, and after an install that adds a model)."""
    global _probe_at, _probe_cache
    _probe_at = 0.0
    _probe_cache = {}
    _hf_cache.clear()


def _catalog_entry(job_name: str) -> Optional[Tuple[str, str]]:
    """(provider, model) declared for ``job_name``, or None if uncatalogued."""
    try:
        from .models.mvp_defaults import MVP_JOB_SPECS
    except Exception:  # noqa: BLE001
        return None
    wanted = str(job_name or "").strip()
    for job_id, _task, provider, model_path, _preferred in MVP_JOB_SPECS:
        if job_id == wanted:
            return str(provider), str(model_path or "")
    return None


def provider_for_job(job_name: str) -> Optional[str]:
    """Declared provider for ``job_name``, or None if the catalog has no entry."""
    entry = _catalog_entry(job_name)
    return entry[0] if entry else None


def jobs_for_provider(provider: str) -> frozenset:
    """Every catalogued job bound to ``provider``.

    Exists so callers that need "the LLM jobs" derive them from the catalog
    instead of keeping a parallel literal that silently goes stale when a job
    is added or repointed.
    """
    try:
        from .models.mvp_defaults import MVP_JOB_SPECS
    except Exception:  # noqa: BLE001
        return frozenset()
    wanted = str(provider or "").strip()
    return frozenset(job_id for job_id, _t, prov, _m, _p in MVP_JOB_SPECS if prov == wanted)


def readiness_of(
    job_name: str,
    *,
    force: bool = False,
    provider_status: Optional[Dict[str, str]] = None,
) -> JobReadiness:
    """The full picture for one job: what it needs and whether it has it.

    ``provider_status`` lets a caller that ALREADY probed pass its answer in,
    rather than each job re-asking. A caller looping over many jobs (the health
    page walks ten dimensions) would otherwise either re-probe per job or read
    a TTL-cached result that no longer matches the probe it just took.
    """
    entry = _catalog_entry(job_name)
    if entry is None:
        return JobReadiness(
            job=job_name,
            provider=None,
            model="",
            ready=True,
            blocking=False,
            reason="no declared provider",
        )
    provider, model = entry

    if provider == "ollama":
        status = provider_status if provider_status is not None else _provider_status(force=force)
        up = status.get("ollama") == "up"
        # An absent probe result counts as not-ready for a hard-stop provider:
        # the alternative is re-running a derivation that will defer again and
        # park the debt, which is the failure this module exists to prevent.
        return JobReadiness(
            job=job_name,
            provider=provider,
            model=model,
            ready=up,
            blocking=not up,
            reason="ollama reachable" if up else "ollama not reachable",
        )

    if provider == "huggingface":
        cached = hf_model_cached(model, force=force)
        return JobReadiness(
            job=job_name,
            provider=provider,
            model=model,
            ready=cached,
            # Never blocking: an uncached model downloads on first use, so
            # holding the job would strand work a networked node completes.
            blocking=False,
            reason=f"{model} cached" if cached else f"{model} not downloaded yet",
        )

    return JobReadiness(
        job=job_name,
        provider=provider,
        model=model,
        ready=True,
        blocking=False,
        reason=f"{provider} needs no model",
    )


def should_hold_job(job_name: str, *, force: bool = False) -> Tuple[bool, str]:
    """Should queued work for ``job_name`` WAIT rather than run? (hold, reason)."""
    state = readiness_of(job_name, force=force)
    return state.blocking, state.reason


def blocking_providers_ready(*, force: bool = False) -> Dict[str, bool]:
    """Readiness of every hard-stop provider — for edge detection."""
    status = _provider_status(force=force)
    return {p: status.get(p) == "up" for p in sorted(_BLOCKING_PROVIDERS)}
