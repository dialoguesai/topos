"""One place that decides how torch runs in this process: device, and thread count.

Every local model — embeddings, NER, rerank, the privacy filter, the scope head — used
to answer "which device?" on its own, and nothing answered "how many threads?" at all.
That produced two bugs with one shape.

**The deadlock this module exists to prevent.** On 2026-08-15 the node wedged twelve
times, always identically: a torch C++ thread inside
``TensorImpl::incref_pyobject → gil_scoped_acquire → take_gil``, with twenty-six threads
resident in libtorch and the event loop stuck in ``take_gil`` behind it. Torch's intra-op
pool refcounts a tensor's *Python* object from a non-Python thread, so it needs the GIL;
in an asyncio server that already runs DB work and job workers on threads, the pool and
the loop deadlock. MPS makes it likelier — its synchronous Metal dispatch runs the block
that grabs the GIL — but the thread count is the multiplier.

**What we do about it.** Bound the intra-op pool (a 265 MB-class model does not need
twenty-six threads to embed a sentence), and default the *device* to whatever the host
actually has, MPS included — but let the owner force any of them by name. Nothing here
is hardcoded to one vendor: a CUDA box gets CUDA, an Apple box gets MPS or CPU, a Linux
server gets CPU, and every one of those is an override away.

Knobs, highest precedence first:

* ``TOPOS_ML_DEVICE`` / ``settings.engine_ml_device`` — ``cpu`` | ``cuda`` | ``mps`` | ``auto``
* ``TOPOS_ML_THREADS`` — intra-op threads; ``0`` leaves torch's default alone
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

ENV_DEVICE = "TOPOS_ML_DEVICE"
ENV_THREADS = "TOPOS_ML_THREADS"

#: Torch's default intra-op pool is one thread per core — twenty-six on this machine.
#: Every one of them can need the GIL to refcount a Python-owned tensor, and each is
#: another chance to deadlock against the event loop. Local inference here is
#: single-sentence and latency-bound, not throughput-bound, so a small pool costs
#: nothing measurable and removes most of the contention surface.
DEFAULT_THREADS = 2

_configured = False


def _settings_device() -> str:
    try:
        from topos.config.settings import settings

        return (getattr(settings, "engine_ml_device", None) or "").strip().lower()
    except Exception:  # noqa: BLE001 — settings must never break model loading
        return ""


def resolve_device(preferred: Optional[str] = None) -> str:
    """``cpu`` | ``cuda`` | ``mps``, by explicit choice then by capability.

    ``auto`` (the default) means CUDA, else MPS, else CPU — the accelerator the host
    actually has, rather than the one the author's laptop had.
    """
    choice = (
        (preferred or "").strip().lower()
        or os.environ.get(ENV_DEVICE, "").strip().lower()
        or _settings_device()
        or "auto"
    )
    if choice in {"cpu", "cuda", "mps"}:
        return choice
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001 — a probe must never break loading
        logger.debug("torch device probe failed; using cpu", exc_info=True)
    return "cpu"


def configure() -> None:
    """Bound torch's thread pools. Idempotent; safe to call from any loader.

    Must run before the first model loads to take effect on every pool, which is why
    the app calls it at startup as well.
    """
    global _configured
    if _configured:
        return
    _configured = True
    raw = os.environ.get(ENV_THREADS, "").strip()
    try:
        threads = int(raw) if raw else DEFAULT_THREADS
    except ValueError:
        threads = DEFAULT_THREADS
    if threads <= 0:
        logger.info("torch threading left at library defaults (TOPOS_ML_THREADS=%s)", raw)
        return
    try:
        import torch

        torch.set_num_threads(threads)
        # Inter-op controls the pool that runs ops in parallel with each other; it can
        # only be set before any parallel work has started, so a late call raises
        # rather than silently doing nothing. Not fatal — intra-op is the big one.
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            logger.debug("inter-op threads already fixed; leaving as-is")
        logger.info(
            "torch threading bounded: intra-op=%d inter-op=1 (deadlock mitigation)", threads
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not bound torch threads", exc_info=True)


def device_for(component: str, preferred: Optional[str] = None) -> str:
    """Resolve, log once per component, and make sure threading is bounded."""
    configure()
    device = resolve_device(preferred)
    logger.info("ml device for %s: %s", component, device)
    return device
