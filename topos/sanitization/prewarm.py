"""Background prewarm for ingest-time sanitization models.

Also the source of truth for "is this node still downloading its models?".

A first node boot fetches ~2.9GB of Hugging Face weights (openai/privacy-filter
2.6G, NSFW_text_classifier 256M). Until 2026-08-06 that was the longest stretch
of the whole install journey and the only one with no feedback anywhere: the
setup panel said "Not connected" and the tray showed a dot, so a first-time user
had no way to tell a working download from a hung install.

It is reportable because the node is already up while it happens. Prewarm runs in
a worker thread (`asyncio.to_thread`) and `wait_for_control_plane_on_startup`
defaults to False, so uvicorn finishes startup and the control-plane socket is
dialing before the first model load begins — verified by running the node and by
polling `/v1/upgrade/status` (16ms) and `/v1/shell/status` (3ms) while the
prewarm thread was deliberately blocked for 25s.

Bytes on disk, never a percentage: the total is only knowable after the fact, and
a fabricated ETA that stalls is worse than an honest number that climbs.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.sanitization.prewarm")

PRIVACY_FILTER_REPO = "openai/privacy-filter"
NSFW_REPO = "michellejieli/NSFW_text_classifier"

# Same module-level snapshot pattern as upgrades/runner.py: a lock plus a plain
# dict, copied out under the lock so a reader never sees a half-written state.
_lock = threading.Lock()
_state: Dict[str, Any] = {
    "phase": "idle",  # idle | loading | ready | failed
    "current_model": None,
    "models_done": 0,
    "models_total": 2,
    "cached_bytes": 0,
    "first_run": None,
    "started_at": None,
    "finished_at": None,
    "models": [],
}


def _hf_cache_root() -> Optional[str]:
    try:
        from huggingface_hub import constants as hf_constants

        return str(hf_constants.HF_HUB_CACHE)
    except Exception:  # noqa: BLE001
        return None


def _repo_dir(cache_root: str, repo_id: str) -> str:
    # Built by hand rather than imported: `repo_folder_name` is not exported from
    # the huggingface_hub top level on 1.18.0 (verified — it raises ImportError),
    # and reaching into a private module to save one line is not worth the
    # breakage on the next upgrade. The layout itself is stable and documented.
    return os.path.join(cache_root, "models--" + repo_id.replace("/", "--"))


def _dir_bytes(path: str) -> int:
    """Bytes currently on disk under `path`, including partial downloads.

    Counts `blobs/*.incomplete` too, which is what makes the number climb during
    a download rather than jumping at the end. Measured at 0.6–6.0ms over the
    2.83GB repo, so it is safe to call on every status poll.
    """
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    # lstat, not stat: the HF layout stores each file once under
                    # blobs/ and links to it from snapshots/, so following links
                    # counts every byte twice — it reported 6.19GB for repos
                    # totalling 2.9GB. A progress number that overstates by 2x is
                    # worse than none, because it finishes at "200%".
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _cached_bytes(repos: List[str]) -> int:
    root = _hf_cache_root()
    if not root:
        return 0
    return sum(_dir_bytes(_repo_dir(root, repo)) for repo in repos)


def _repo_present(repo_id: str) -> bool:
    """Whether this repo is fully downloaded, not merely started.

    Directory existence alone is not enough: an interrupted first download
    leaves the repo directory in place with half-written `*.incomplete` blobs,
    so the next boot would decide first_run is False and the UI would say
    "preparing… should only take a moment" while it fetched the remaining
    gigabytes. An unfinished blob is the cache's own marker for that.
    """
    root = _hf_cache_root()
    if not root:
        return False
    repo_dir = _repo_dir(root, repo_id)
    if not os.path.isdir(repo_dir):
        return False
    blobs = os.path.join(repo_dir, "blobs")
    try:
        names = os.listdir(blobs)
    except OSError:
        # No blobs dir at all means nothing has landed yet.
        return False
    # Complete means at least one finished blob and nothing still in flight. An
    # empty blobs dir is the very start of a first download, not the end of one.
    return any(not n.endswith(".incomplete") for n in names) and not any(
        n.endswith(".incomplete") for n in names
    )


def _update(**fields: Any) -> None:
    with _lock:
        _state.update(fields)


def _mark_model(repo_id: str, status: str, error: Optional[str] = None) -> None:
    with _lock:
        models = [m for m in _state["models"] if m.get("repo_id") != repo_id]
        entry: Dict[str, Any] = {"repo_id": repo_id, "status": status}
        if error:
            entry["error"] = error
        models.append(entry)
        _state["models"] = models


def prewarm_status() -> Dict[str, Any]:
    """A copy of the current prewarm state, safe to serialise.

    `cached_bytes` is recomputed on read while loading, so a caller polling this
    sees the download grow. Once ready it is left alone — the walk is cheap but
    pointless when nothing is changing.
    """
    with _lock:
        snapshot = dict(_state)
        snapshot["models"] = [dict(m) for m in _state["models"]]
    if snapshot["phase"] == "loading":
        snapshot["cached_bytes"] = _cached_bytes([PRIVACY_FILTER_REPO, NSFW_REPO])
    return snapshot


def prewarm_sanitization_models() -> None:
    """Load privacy-filter and NSFW pipelines so first ingest does not cold-start during UI use."""
    from topos.config.settings import settings

    if not getattr(settings, "sanitization_prewarm_on_startup", True):
        logger.debug("sanitization prewarm skipped (SANITIZATION_PREWARM_ON_STARTUP=off)")
        _update(phase="ready", finished_at=time.time(), models_done=0, models_total=0)
        return

    # "First run" decides whether the UI says "downloading" or "loading": the
    # same code path takes minutes on a cold cache and seconds on a warm one, and
    # telling a returning user their models are downloading would be a lie.
    first_run = not (_repo_present(PRIVACY_FILTER_REPO) and _repo_present(NSFW_REPO))
    _update(
        phase="loading",
        first_run=first_run,
        started_at=time.time(),
        models_done=0,
        models_total=2,
        current_model=PRIVACY_FILTER_REPO,
    )

    try:
        from topos.sanitization.privacy_filter import prewarm_privacy_filter

        prewarm_privacy_filter()
        _mark_model(PRIVACY_FILTER_REPO, "ready")
        _update(models_done=1, current_model=NSFW_REPO)
        logger.info("sanitization prewarm: privacy-filter ready")
    except Exception as exc:  # noqa: BLE001
        _mark_model(PRIVACY_FILTER_REPO, "failed", str(exc))
        _update(models_done=1, current_model=NSFW_REPO)
        logger.warning("sanitization prewarm: privacy-filter failed (non-fatal): %s", exc)

    try:
        from topos.sanitization.nsfw_classifier import prewarm_nsfw_classifier

        prewarm_nsfw_classifier()
        _mark_model(NSFW_REPO, "ready")
        logger.info("sanitization prewarm: nsfw classifier ready")
    except Exception as exc:  # noqa: BLE001
        _mark_model(NSFW_REPO, "failed", str(exc))
        logger.warning("sanitization prewarm: nsfw classifier failed (non-fatal): %s", exc)

    # Prewarm failures are non-fatal by design — the node serves either way — so
    # the phase reports what happened without implying the node is broken.
    with _lock:
        any_failed = any(m.get("status") == "failed" for m in _state["models"])
    _update(
        phase="failed" if any_failed else "ready",
        models_done=2,
        current_model=None,
        finished_at=time.time(),
        cached_bytes=_cached_bytes([PRIVACY_FILTER_REPO, NSFW_REPO]),
    )
