"""The Python the node installs onto, and the one platform pairing known not to load.

On 24 Oct 2026 a Mac updated to macOS 27.0.1 and every embeddings and entities job
on its node began to fail: dyld refuses scipy 1.15.3's gfortran-built extension
(`scipy/sparse/linalg/_propack/_spropack.cpython-310-darwin.so`: "section
'__DATA/__thread_bss' has a zero-fill section type, but offset field is not
zero"). Measured on that Mac: scipy 1.15.3 fails the same way on Python 3.11, 3.12
and 3.13, and scipy 1.16.3 loads on 3.11 and 3.12, so the fault is the scipy build,
not the interpreter. It still lands on Python 3.10, because 1.15.3 is the last scipy
that supports 3.10 and nothing newer ever will.

Each failing job logged its own dlopen traceback and nothing said why, or what to
do. `log_platform_problems` runs once at startup and says it in one line.
"""

from __future__ import annotations

import logging
import platform
import sys
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Optional

from packaging.version import InvalidVersion, Version

#: The interpreter every install and reinstall names with `--python`. Without
#: it uv picks its own preferred interpreter: on 24 Oct 2026 a plain
#: `uv tool install --force` rebuilt the owner's node on 3.14, which nothing had
#: tested. It equals the floor in pyproject's requires-python, and
#: tests/topos/test_runtime_platform.py fails when the two disagree.
INSTALL_PYTHON = "3.12"

#: The first scipy whose macOS wheels load on macOS 27 (1.15.3 fails, 1.16.3 loads).
_SCIPY_THAT_LOADS = Version("1.16")
_FIRST_MACOS_THAT_REFUSES_OLD_SCIPY = 27


def _macos_major() -> Optional[int]:
    if sys.platform != "darwin":
        return None
    release = platform.mac_ver()[0]
    try:
        return int(release.split(".")[0])
    except ValueError:
        return None


def _installed_scipy() -> Optional[Version]:
    # Read the metadata, not the module: importing scipy is the thing that fails.
    try:
        return Version(package_version("scipy"))
    except (PackageNotFoundError, InvalidVersion):
        return None


def scipy_load_problem() -> Optional[str]:
    """One sentence naming the fix when this Mac cannot load the installed scipy, else None."""
    macos = _macos_major()
    if macos is None or macos < _FIRST_MACOS_THAT_REFUSES_OLD_SCIPY:
        return None
    scipy = _installed_scipy()
    if scipy is None or scipy >= _SCIPY_THAT_LOADS:
        return None
    from .runtime_update import resolve_uv_binary

    uv = resolve_uv_binary() or "uv"
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (
        f"macOS {platform.mac_ver()[0]} cannot load scipy {scipy} (dyld rejects its Fortran extensions), "
        f"so every embeddings and entity-extraction job on this node will fail. This node runs Python {running}; "
        f"scipy {_SCIPY_THAT_LOADS} or newer is needed and does not exist for Python 3.10. "
        f"Fix: `{uv} tool upgrade --python {INSTALL_PYTHON} topos-node`, then restart Topos."
    )


def log_platform_problems(logger: logging.Logger) -> bool:
    """Log each known platform problem once, as an ERROR. True when there was one."""
    problem = scipy_load_problem()
    if problem is None:
        return False
    logger.error(problem)
    return True
