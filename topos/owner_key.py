"""Owner-key self-mint — principal fabric P2/P5 dual-mint (node side).

The install-flow invariant, made real: on boot a node ensures a
``TOPOS_OWNER_KEY`` exists in ``~/.topos/.env``, minting one locally if absent.
This is what auto-activates the fabric — packet floors, principal stamping,
tier resolution all switch from legacy (no owner key) to enforcing the moment
the key exists — for every node, fresh or upgraded, with no manual step and no
secret ever crossing the wire (contrast the relay lane, where the CP stamps
identity; the owner key is for the node's OWN direct-HTTP surface).

Idempotent and additive: an existing key is never rewritten (rotation is a
deliberate owner action — delete the line), the rest of the env file is left
byte-for-byte intact, and the file mode is clamped to 0600 to match how the
pairing flow already writes TOPOS_KEY. First-party direct-HTTP callers learn
the value the same way they learn TOPOS_KEY today; over the relay they never
need it, because P3 stamps carry owner identity there.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger("topos.owner_key")

ENV_PATH = "~/.topos/.env"
_KEY_NAME = "TOPOS_OWNER_KEY"
_LINE_RE = re.compile(r"^TOPOS_OWNER_KEY=.+$", re.M)


def _env_path() -> Path:
    return Path(os.path.expanduser(os.environ.get("TOPOS_ENV_FILE") or ENV_PATH))


def ensure_owner_key(*, env_path: Optional[Path] = None) -> Optional[str]:
    """Return the owner key, minting + persisting one if the env lacks it.

    Returns None only when persistence is impossible (unwritable dir), so the
    node stays in legacy mode rather than enforcing with a key it could not
    save — a key that vanished on the next boot would flip enforcement on and
    off and strand the app on the old value.
    """
    # Already present in the process env (pairing just set it, or a prior boot)?
    existing = str(os.environ.get(_KEY_NAME) or "").strip()
    if existing:
        return existing

    path = env_path or _env_path()
    try:
        text = path.read_text() if path.is_file() else ""
    except OSError:
        text = ""
    m = _LINE_RE.search(text)
    if m:
        value = text[m.start():m.end()].split("=", 1)[1].strip()
        if value:
            os.environ.setdefault(_KEY_NAME, value)
            return value

    minted = f"ok_{secrets.token_hex(24)}"
    line = f"{_KEY_NAME}={minted}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text + line)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600, like TOPOS_KEY
        except OSError:
            logger.debug("could not chmod %s", path, exc_info=True)
        os.environ[_KEY_NAME] = minted
        logger.info("minted owner key into %s (fabric enforcement now active)", path)
        return minted
    except OSError:
        logger.warning("could not persist owner key to %s; staying legacy", path, exc_info=True)
        return None
