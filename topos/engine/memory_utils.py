"""Process-level ML memory release helpers."""

from __future__ import annotations

import gc
import logging
import sys
from typing import Optional

try:
    import resource
except ImportError:  # Windows has no resource module; RSS reporting degrades to None.
    resource = None  # type: ignore[assignment]

logger = logging.getLogger("topos.engine.memory")


def get_process_rss_mb() -> Optional[float]:
    """Return current process RSS in megabytes, or None if unavailable."""
    if resource is None:
        return None
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = usage.ru_maxrss
        if sys.platform == "darwin":
            return float(rss) / (1024 * 1024)
        return float(rss) / 1024
    except Exception:
        return None


def release_ml_memory() -> None:
    """Best-effort release of Python and torch allocator caches."""
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("release_ml_memory torch flush skipped: %s", exc)


def torch_available() -> bool:
    try:
        import torch  # noqa: F401

        return True
    except ImportError:
        return False


def mps_available() -> bool:
    try:
        import torch

        return bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
    except ImportError:
        return False
