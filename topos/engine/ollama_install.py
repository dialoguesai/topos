"""Start and observe a one-click Ollama install on this node (PLAN Q5 / D3).

The websocket is request/response, but the official installer takes minutes and
~180 MB. So install is a start message plus a poll over a single in-memory job
— the same shape as ``ollama_pull``, except there is only one install per node
(not one per tag).

Darwin only runs the official script. Non-Darwin answers a typed refusal so the
FE keeps the manual card as the only path (Linux wants sudo/systemd a headless
node must not attempt). On a nonzero script exit the D3 resilience rule applies:
if /Applications/Ollama.app is already present, open it hidden and treat
reachability as the truth rather than surfacing a PATH-symlink sudo failure.
"""

from __future__ import annotations

import logging
import os
import platform as platform_mod
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("topos.engine.ollama_install")

STATE_IDLE = "idle"
STATE_INSTALLING = "installing"
STATE_STARTED = "started"
STATE_ERROR = "error"

REASON_UNSUPPORTED = "unsupported_platform"
OLLAMA_APP = Path("/Applications/Ollama.app")

_LOCK = threading.Lock()
_JOB: Optional[Dict[str, Any]] = None

OnStatus = Callable[[str], None]
RunInstall = Callable[..., tuple[int, str]]


def _idle_record() -> Dict[str, Any]:
    return {
        "state": STATE_IDLE,
        "status": "",
        "error": "",
        "refused": False,
        "reason": "",
    }


def reset_job() -> None:
    """Drop the in-memory job. Test seam — nothing in the product calls this."""
    global _JOB
    with _LOCK:
        _JOB = None


def install_status() -> Dict[str, Any]:
    with _LOCK:
        return dict(_JOB) if _JOB is not None else _idle_record()


def default_platform() -> str:
    return platform_mod.system()


def default_app_present() -> bool:
    return OLLAMA_APP.is_dir()


def default_open_app() -> None:
    # Hidden so a headless node does not pop a window in the owner's face.
    subprocess.run(
        ["open", "-a", "Ollama", "--args", "hidden"],
        check=False,
        capture_output=True,
        text=True,
    )


def default_is_reachable() -> bool:
    from .backends.ollama import OllamaAdapter

    return bool(OllamaAdapter().is_reachable())


def default_run_install(*, on_status: Optional[OnStatus] = None) -> tuple[int, str]:
    """Run Ollama's official install.sh. Never set OLLAMA_NO_START — we need :11434."""
    env = {k: v for k, v in os.environ.items() if k != "OLLAMA_NO_START"}
    proc = subprocess.Popen(
        ["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    last = ""
    assert proc.stdout is not None
    for line in proc.stdout:
        last = line.rstrip("\n")
        if on_status is not None and last:
            on_status(last)
    code = proc.wait()
    return int(code), last


def _set_job(**fields: Any) -> Dict[str, Any]:
    global _JOB
    with _LOCK:
        record = dict(_JOB) if _JOB is not None else _idle_record()
        record.update(fields)
        _JOB = record
        return dict(record)


def _run_install_job(
    *,
    run_install: RunInstall,
    app_present: Callable[[], bool],
    open_app: Callable[[], None],
    is_reachable: Callable[[], bool],
) -> None:
    last_line = ""

    def on_status(line: str) -> None:
        nonlocal last_line
        last_line = line
        _set_job(status=line)

    try:
        code, script_last = run_install(on_status=on_status)
        # Prefer streamed status lines; the return string is only a fallback when
        # the installer produced no stdout (some shells buffer until exit).
        if not last_line and script_last:
            last_line = script_last
    except Exception as exc:  # noqa: BLE001 — installer failures must surface, not hang
        logger.warning("ollama install raised: %s", exc)
        _set_job(state=STATE_ERROR, error=str(exc), status=last_line or str(exc))
        return

    if code == 0:
        _set_job(state=STATE_STARTED, error="", status=last_line)
        return

    # D3 resilience: the script's only sudo is a PATH-symlink fallback. The
    # engine needs :11434, not the CLI — so a nonzero exit with the app present
    # is still a win if we can start it ourselves.
    if app_present():
        try:
            open_app()
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to open Ollama.app after install exit %s: %s", code, exc)
            _set_job(
                state=STATE_ERROR,
                error=last_line or f"install exited {code}",
                status=last_line,
            )
            return
        # Spec D3 + Q5 guard: app present after a failed script ends in
        # started (not error). Probe is informational — a transient :11434
        # miss must not strand the CTA on the error path; the FE poll flips C.
        try:
            reachable = bool(is_reachable())
        except Exception:  # noqa: BLE001
            reachable = False
        logger.info("ollama install resilience open complete reachable=%s", reachable)
        _set_job(state=STATE_STARTED, error="", status=last_line)
        return

    _set_job(
        state=STATE_ERROR,
        error=last_line or f"install exited {code}",
        status=last_line,
    )


def start_install(
    *,
    platform: Optional[str] = None,
    run_install: Optional[RunInstall] = None,
    app_present: Optional[Callable[[], bool]] = None,
    open_app: Optional[Callable[[], None]] = None,
    is_reachable: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Begin installing Ollama in the background; returns the first job record.

    A second start while one is ``installing`` is a no-op acknowledging the
    running job — a double-clicked CTA must not launch two installers.
    """
    global _JOB
    plat = platform if platform is not None else default_platform()
    if str(plat) != "Darwin":
        refused = {
            "state": STATE_ERROR,
            "status": "",
            "error": (
                "Ollama one-click install is only available on macOS. "
                "Use the manual steps below."
            ),
            "refused": True,
            "reason": REASON_UNSUPPORTED,
        }
        with _LOCK:
            _JOB = dict(refused)
            return dict(refused)

    with _LOCK:
        if _JOB is not None and _JOB.get("state") == STATE_INSTALLING:
            return dict(_JOB)
        record = _idle_record()
        record["state"] = STATE_INSTALLING
        _JOB = record
        snapshot = dict(record)

    worker = threading.Thread(
        target=_run_install_job,
        kwargs={
            "run_install": run_install if run_install is not None else default_run_install,
            "app_present": app_present if app_present is not None else default_app_present,
            "open_app": open_app if open_app is not None else default_open_app,
            "is_reachable": is_reachable if is_reachable is not None else default_is_reachable,
        },
        name="ollama-install",
        daemon=True,
    )
    worker.start()
    return snapshot
