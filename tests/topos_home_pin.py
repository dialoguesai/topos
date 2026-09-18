"""Point every ``~/.topos`` default the engine has at a per-session temp home.

The conftest calls :func:`pin_env` at import time, BEFORE it imports any topos
module, because ``topos.config.settings`` builds its singleton on first import
and several modules freeze a path into a module constant at import. A
session-scoped fixture would run after both. The conftest's session fixture
``_pinned_topos_home`` then owns the directory, and a per-test fixture puts the
pins (and owner mode) back after any test that moved them.

Why it exists (2026-09-18, audits MERGE_REHEARSAL.md §2.8 items 4-5): no
conftest set ``TOPOS_ENV_FILE``, so every app lifespan with a control-plane URL
ran the dual-mint's ``ensure_owner_key`` against the owner's real
``~/.topos/.env``: it read the file and appended a minted ``TOPOS_OWNER_KEY``
when none was there. The same lifespan bound the owner socket at
``~/.topos/engine.sock``.

Enumerated 2026-09-18 by grepping ``topos/`` for ``expanduser``, ``Path.home()``
and ``.topos``. Two groups:

* Defaults with an env override. Pinned in ``os.environ``, so a subprocess
  inherits them too: see :data:`PINNED_ENV`.
* Defaults with no override, patched on the module: see :func:`patch_module_defaults`.

Deliberately NOT pinned, and left to the file guard in ``tests/live_db_watch.py``
(which refuses any open under the real ``~/.topos``):

* ``topos.cli.tray`` reads ``Path.home()/".topos"/".env"`` inline inside a method.
* ``topos.query.scope_head.default_head_path`` stats
  ``~/.topos/models/scope_head/head.json`` inline. Its only override,
  ``TOPOS_SCOPE_HEAD``, also means "never fetch", which would change what the
  scope-head tests measure.
* ``topos.core.logging.default_node_log_path``. Only ``topos start --app`` uses
  it, to default ``TOPOS_LOG_FILE``, and ``test_app_mode_logging`` asserts its
  real value. Pinning ``TOPOS_LOG_FILE`` itself would send every
  ``setup_logging`` call to a file instead of stdout.
* ``TOPOS_DATABASE_PATH`` / ``TOPOS_BACKUP_DIR`` / ``TOPOS_SCOPE_SHADOW_LOG``:
  already pinned PER TEST by ``_no_live_db_guard`` and
  ``_no_live_scope_shadow_guard``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Dict, Optional

#: The engine's own override variables for ``~/.topos`` defaults, and where each
#: lands inside the pinned home (relative to its ``.topos``).
PINNED_ENV: Dict[str, str] = {
    # topos/owner_key.py ENV_PATH = "~/.topos/.env": the dual-mint target.
    "TOPOS_ENV_FILE": ".env",
    # topos/uds.py SOCKET_PATH = "~/.topos/engine.sock": the owner socket.
    "TOPOS_UDS_PATH": "engine.sock",
    # topos/storage/raw/file_store.py active_ingestion_base().
    "TOPOS_INGESTION_BASE_PATH": "ingestion",
}

#: Owner mode, which the dual-mint arms process-wide. Removed at pin time so a
#: developer's shell export cannot put the whole suite in owner mode.
OWNER_KEY_ENV = "TOPOS_OWNER_KEY"

#: macOS caps an AF_UNIX path at 104 bytes; the socket lives in this directory.
_SUN_PATH_MAX = 100

_ROOT: Optional[Path] = None


def _make_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="topos-home-")).resolve()
    if len(str(root / ".topos" / "engine.sock")) > _SUN_PATH_MAX:
        root.rmdir()
        root = Path(tempfile.mkdtemp(prefix="th-", dir="/tmp")).resolve()
    (root / ".topos").mkdir()
    return root


def pinned_root() -> Path:
    """The per-session stand-in for the home directory (holds ``.topos/``)."""
    if _ROOT is None:
        raise RuntimeError("tests.topos_home_pin.pin_env() has not run")
    return _ROOT


def pinned_topos_dir() -> Path:
    return pinned_root() / ".topos"


def pinned_paths() -> Dict[str, str]:
    base = pinned_topos_dir()
    return {name: str(base / rel) for name, rel in PINNED_ENV.items()}


def pin_env() -> Path:
    """Create the session home and pin the env overrides. Idempotent.

    Overwrites rather than ``setdefault``: a value inherited from the shell is
    exactly what must not reach the suite (someone who exported
    ``TOPOS_ENV_FILE=~/.topos/.env`` to run a node by hand).
    """
    global _ROOT
    if _ROOT is None:
        _ROOT = _make_root()
    os.environ.update(pinned_paths())
    os.environ.pop(OWNER_KEY_ENV, None)
    return _ROOT


def repin_env() -> None:
    """Put back any pin a test changed without monkeypatch (or deleted)."""
    for name, value in pinned_paths().items():
        if os.environ.get(name) != value:
            os.environ[name] = value


def _redirect_real_home(original):
    """Wrap a ``Path.home()/".topos"``-returning function.

    Redirect only when ``Path.home()`` is still the real home. Tests that point
    ``Path.home`` at their own tmp home (test_active_database_binding,
    test_active_ingestion_binding, ...) keep exactly the behaviour they assert.
    """
    from tests.live_db_watch import REAL_HOME

    def _pinned():
        if Path.home() == REAL_HOME:
            return pinned_topos_dir()
        return original()

    _pinned.__wrapped__ = original
    _pinned.__name__ = getattr(original, "__name__", "_pinned")
    return _pinned


def _swap_default(module, name: str, old, new) -> None:
    """Replace ``old`` with ``new`` in the module constant AND in every function
    default that captured it at def time (``def f(env_path=USER_ENV_PATH)``)."""
    setattr(module, name, new)
    for value in vars(module).values():
        defaults = getattr(value, "__defaults__", None)
        if defaults and any(d is old for d in defaults):
            value.__defaults__ = tuple(new if d is old else d for d in defaults)


#: (module, attribute) -> the pinned value, filled by patch_module_defaults.
_PATCHED: Dict[tuple, object] = {}


def patch_module_defaults() -> None:
    """Patch the ``~/.topos`` defaults that have no env override. Idempotent.

    Re-run before every test: a test that reloads one of these modules (several
    pop ``topos.*`` from ``sys.modules`` to isolate app startup) gets a fresh
    module with the real default back.
    """
    import importlib

    from tests.live_db_watch import REAL_HOME

    topos_dir = pinned_topos_dir()
    real_env = REAL_HOME / ".topos" / ".env"

    # topos/relay_stamp.py: the CP stamp key the node pins on first boot (TOFU).
    # The lifespan starts `autopin_stamp_key` on a thread whenever a
    # control-plane URL is set, and it WRITES this file.
    relay_stamp = importlib.import_module("topos.relay_stamp")
    pinned_key = str(topos_dir / "cp_stamp_key.pub")
    if relay_stamp._PINNED_KEY_PATH != pinned_key:
        relay_stamp._PINNED_KEY_PATH = pinned_key
    _PATCHED[("topos.relay_stamp", "_PINNED_KEY_PATH")] = pinned_key

    # topos/cli/commands.py and topos/cli/reprocess_cmd.py: USER_ENV_PATH, the
    # env file `topos start` / `--save-key` read and write.
    for mod_name in ("topos.cli.commands", "topos.cli.reprocess_cmd"):
        mod = importlib.import_module(mod_name)
        current = getattr(mod, "USER_ENV_PATH", None)
        if current is not None and Path(current) == real_env:
            _swap_default(mod, "USER_ENV_PATH", current, topos_dir / ".env")
        _PATCHED[(mod_name, "USER_ENV_PATH")] = topos_dir / ".env"

    # topos/storage/db/paths.py active_base(), and topos/profiles.py, which
    # imports it by value: the active slot, its active-profile.json marker and
    # the profiles/ archive.
    for mod_name in ("topos.storage.db.paths", "topos.profiles"):
        mod = importlib.import_module(mod_name)
        current = getattr(mod, "active_base")
        if getattr(current, "__wrapped__", None) is None:
            mod.active_base = _redirect_real_home(current)
        _PATCHED[(mod_name, "active_base")] = mod.active_base



def patched() -> Dict[tuple, object]:
    return dict(_PATCHED)
