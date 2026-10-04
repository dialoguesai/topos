"""``permissions_v2_checking_model`` (A2A-3 §7.5; decision D6; A2A-5 §5.5; N5): the checking model's status, and its
download after the owner's yes.

Every machine assessment a share needs (every p2c-v3 message, journal entry and interest label) comes from one pinned
local model: ``shadow_labeler_local.MODEL`` at ``MODEL_REVISION``, asked at the node's configured model host
(``configured_base_url``). Without it a share releases only what the owner checked by hand (E1 surprise 3). This
module tells the owner where that model stands and fetches it when they say yes.

``status`` (both operations answer it):

- ``unsupported``: this machine is not Apple Silicon. The pinned build is MLX, which runs nowhere else
  (``config.local_model_builds``); there, only owner-reviewed items can be shared (D6).
- ``ready``: the model host lists the pinned tag at the pinned digest.
- ``downloading``: a download of the tag is running in this process (``engine.ollama_pull``'s record, shared with the
  owner's other model downloads, so one started either way is seen both ways).
- ``failed``: this process's last download of it ended in error, or ended and the host does not list the tag at the
  pinned digest (the digest check: a build nobody reviewed is never "ready").
- ``missing``: otherwise, the host unreachable included.

``size_bytes``: the host's listed size when the pinned build is installed; the size of the running download as the
host reports it; otherwise the pinned size (``SIZE_BYTES``: what a download fetches), which the host never reports
before a download starts. ``downloaded_bytes``: the running download's bytes, the size when ready, else 0.
``free_bytes``: free bytes where the host keeps its models, when the host is this machine (``engine.disk_space``);
null for a remote host or an unreadable volume.

``download`` (``{"confirm": true}``): ``unsupported`` refuses on another machine; nothing starts while the model is
ready or downloading (the same status answers); ``disk_low`` refuses when the host is this machine and its free
space is below the model's size plus the node's disk floor (the owner's setting, ``disk_space.min_free_bytes``).
Otherwise the pull starts on the host and the status answers at once. The pull never removes another model to make
room (``reclaim=False``), and it stops mid-stream when the size the host reports would not fit (``ollama_pull``).

The status read never starts, opens or pulls anything: it asks the host's tag list with a short timeout. A download
asks the host to pull, as the owner's other model downloads do.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path
from typing import Optional

#: The pinned build's download size, in bytes: its manifest's layers (8,903,014,479) plus its config (279). Read on
#: 4 Oct 2026 by the program manager (WS0) from the manifest a local model store keeps for ``MODEL``, whose sha256 is
#: ``MODEL_REVISION`` (so it is that build's manifest); no model was run. Change it only with ``MODEL_REVISION``.
SIZE_BYTES: Optional[int] = 8_903_014_758
TAGS_TIMEOUT_SECONDS = 2.0


class Refused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _pinned() -> tuple:
    from . import shadow_labeler_local
    return shadow_labeler_local.MODEL, shadow_labeler_local.MODEL_REVISION


def supported() -> bool:
    """Apple Silicon only: the pinned build is an MLX build (decision D6)."""
    from topos.config.local_model_builds import PLATFORM_MACOS_ARM64, current_platform
    return current_platform() == PLATFORM_MACOS_ARM64


def host() -> str:
    from .shadow_labeler_local import configured_base_url
    return configured_base_url()


def listed(base_url: str) -> Optional[dict]:
    """The host's entry for the pinned tag ({digest, size}), {} when it lists no such tag, None when unreachable."""
    model, _revision = _pinned()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # no proxy from the environment
    try:
        with opener.open(urllib.request.Request(base_url.rstrip("/") + "/api/tags", method="GET"),
                         timeout=TAGS_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 -- not listed by a host that did not answer
        return None
    models = body.get("models") if isinstance(body, dict) else None
    found = [row for row in models or [] if isinstance(row, dict) and row.get("name") == model]
    if len(found) != 1:
        return {}
    size = found[0].get("size")
    return {"digest": found[0].get("digest"), "size": size if type(size) is int and size >= 0 else None}


def _local(base_url: str) -> bool:
    from topos.engine.disk_space import space_check_applies
    return space_check_applies(base_url)


def free_bytes(base_url: str) -> Optional[int]:
    from topos.engine.disk_space import free_bytes as volume_free, ollama_models_dir
    return volume_free(ollama_models_dir()) if _local(base_url) else None


def _floor(served: Optional[Path]) -> int:
    """The node's disk floor: the owner's setting in the served database (``disk_space.min_free_bytes``, which keeps
    the shipped default when it cannot read one)."""
    from topos.engine.disk_space import DEFAULT_MIN_FREE_BYTES, min_free_bytes
    if served is None:
        return int(min_free_bytes())
    try:
        conn = sqlite3.connect(Path(served).as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return DEFAULT_MIN_FREE_BYTES
    try:
        return int(min_free_bytes(conn))
    finally:
        conn.close()


def _record() -> dict:
    from topos.engine.ollama_pull import pull_status
    model, _revision = _pinned()
    return pull_status(model)


def status(*, base_url: Optional[str] = None) -> dict:
    from topos.engine.ollama_pull import STATE_DONE, STATE_ERROR, STATE_PULLING
    base_url = base_url or host()
    _model, revision = _pinned()
    free = free_bytes(base_url)
    if not supported():
        return {"status": "unsupported", "size_bytes": SIZE_BYTES or 0, "downloaded_bytes": 0, "free_bytes": free}
    record = _record()
    pulling = record.get("state") == STATE_PULLING
    entry = None if pulling else listed(base_url)
    if entry and entry.get("digest") == revision:
        size = entry.get("size") or SIZE_BYTES or 0
        return {"status": "ready", "size_bytes": size, "downloaded_bytes": size, "free_bytes": free}
    reported = int(record.get("total") or 0)
    if pulling:
        return {"status": "downloading", "size_bytes": reported or SIZE_BYTES or 0,
                "downloaded_bytes": int(record.get("completed") or 0), "free_bytes": free}
    # Not installed at the pinned digest: what a download will fetch. A size the host lists is another build's.
    size = SIZE_BYTES or (entry or {}).get("size") or reported or 0
    if record.get("state") in (STATE_DONE, STATE_ERROR):
        # Ended, and not listed at the pinned digest: an error, or a build nobody reviewed (the digest check).
        return {"status": "failed", "size_bytes": size, "downloaded_bytes": 0, "free_bytes": free}
    return {"status": "missing", "size_bytes": size, "downloaded_bytes": 0, "free_bytes": free}


def download(*, served: Optional[Path], base_url: Optional[str] = None) -> dict:
    """Start the pinned model's download after the owner's yes; the status, or ``Refused``."""
    from topos.engine.backends.ollama import OllamaAdapter
    from topos.engine.ollama_pull import start_pull
    base_url = base_url or host()
    if not supported():
        raise Refused("unsupported")
    now = status(base_url=base_url)
    if now["status"] in ("ready", "downloading"):
        return now
    needed = int(now["size_bytes"] or SIZE_BYTES or 0)
    free = now["free_bytes"]
    if free is not None and free < needed + _floor(served):
        raise Refused("disk_low")
    model, _revision = _pinned()
    start_pull(model, adapter=OllamaAdapter(base_url=base_url), known_size_bytes=needed or None, reclaim=False)
    return status(base_url=base_url)
