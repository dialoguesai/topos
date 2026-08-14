"""Whether a derivation job's provider is usable on this machine right now.

A job that cannot reach the model it needs has not failed because the code is
wrong. It failed because the machine is not ready yet, and it will succeed
unchanged once the model arrives. Telling those two apart is what lets recorded
debt WAIT for the model instead of being retried into the same wall and parked.

Per-job provider comes from the model catalog (``MVP_JOB_SPECS``), not a second
hand-maintained list: the catalog is what actually binds a job to ollama /
huggingface / rules, so a job registered there is classified here without anyone
remembering to update a frozenset that lives somewhere else.

Two deliberate limits, both inherited rather than introduced:

* Jobs absent from the catalog (``facts``, ``topic_clusters``, ``statistics``,
  ``timeline``, ``attention_triage``, ``complexity_snapshot``) have no declared
  provider and are reported READY. Some of them do use an LLM. Reporting
  "unknown" as "blocked" would stall debts this module does not understand, so
  it errs toward attempting the work — which is exactly today's behaviour.
* HuggingFace is reported reachable unconditionally, because
  ``check_provider_status`` says so without checking whether any weights are
  actually cached. An offline first run with no cached weights is therefore
  still misreported as ready. Fixing that belongs with the probe, not here.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: How long a provider probe is trusted. The sweep and the retry executor both
#: ask; without this, a queue drain would open a socket per debt.
_PROBE_TTL_SECONDS = 30.0

#: Providers whose absence blocks a job. "rules" needs nothing; "huggingface"
#: runs in-process (see the caveat above).
_BLOCKING_PROVIDERS = frozenset({"ollama"})

_probe_at: float = 0.0
_probe_cache: Dict[str, str] = {}


def _provider_status(*, force: bool = False) -> Dict[str, str]:
    """Provider reachability, cached for ``_PROBE_TTL_SECONDS``."""
    global _probe_at, _probe_cache
    now = time.monotonic()
    if not force and _probe_cache and (now - _probe_at) < _PROBE_TTL_SECONDS:
        return dict(_probe_cache)
    try:
        from ..features.signal.data_health import check_provider_status

        _probe_cache = dict(check_provider_status())
    except Exception as exc:  # noqa: BLE001 — a probe failure is not readiness
        logger.debug("[DERIVE:READY] provider probe failed: %s", exc)
        _probe_cache = {}
    _probe_at = now
    return dict(_probe_cache)


def reset_probe_cache() -> None:
    """Drop the cached probe (tests, and after an install that adds a model)."""
    global _probe_at, _probe_cache
    _probe_at = 0.0
    _probe_cache = {}


def provider_for_job(job_name: str) -> Optional[str]:
    """Declared provider for ``job_name``, or None if the catalog has no entry."""
    try:
        from .models.mvp_defaults import MVP_JOB_SPECS
    except Exception:  # noqa: BLE001
        return None
    wanted = str(job_name or "").strip()
    for job_id, _task, provider, _path, _preferred in MVP_JOB_SPECS:
        if job_id == wanted:
            return str(provider)
    return None


def job_is_ready(job_name: str, *, force: bool = False) -> Tuple[bool, str]:
    """Can ``job_name`` run right now? Returns (ready, human-readable reason)."""
    provider = provider_for_job(job_name)
    if provider is None:
        return True, "no declared provider"
    if provider not in _BLOCKING_PROVIDERS:
        return True, f"{provider} needs no running service"
    status = _provider_status(force=force)
    state = status.get(provider)
    if state == "up":
        return True, f"{provider} reachable"
    # An absent probe result is treated as not-ready for a blocking provider:
    # the alternative is to re-run a derivation that will defer again and park
    # the debt, which is the failure this module exists to prevent.
    return False, f"{provider} not reachable"


def blocking_providers_ready(*, force: bool = False) -> Dict[str, bool]:
    """Readiness of every provider that can block a job — for edge detection."""
    status = _provider_status(force=force)
    return {p: status.get(p) == "up" for p in sorted(_BLOCKING_PROVIDERS)}
