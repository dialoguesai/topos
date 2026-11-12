"""Open a Terminal window that runs one ``topos-node`` command for the person to answer.

Some steps need a yes from the person on a terminal: connecting a coding tool (``agents add``), and
turning on a source that holds other people's words (``sources enable imessage``). A coding agent has
no terminal of its own, so instead of telling the person to type a command it may open the window
where that command is already running and waiting for them. Opening the window grants nothing: the
command inside asks its question and does nothing on any answer but yes.

``agents_cmd._open_approval_terminal`` is the first use of this pattern and keeps its own copy (its
script name and wording are pinned by tests); this is the same script with the words passed in.
macOS only: elsewhere there is no window to open and the caller hands the person the command.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

#: Only paths cross into the new terminal, never credentials. HOME and the socket path too, so a
#: sandboxed node (scripts/local/fresh-node-sandbox.sh) is asked about ITS database, not ``~/.topos``.
_PATH_ENV = ("TOPOS_ENV_FILE", "TOPOS_DATABASE_PATH", "TOPOS_UDS_PATH", "HOME")


def _sh_text(text: str) -> str:
    """``text`` as a single-quoted ``printf`` argument: no expansion, no format directives."""
    return shlex.quote(str(text).replace("%", "%%").replace("\\", "\\\\"))


def build_script(*, title: str, argv: Sequence[str], done: str, not_done: str) -> str:
    """The ``.command`` file's text: say what this window is, run the command, say how it ended, wait."""
    exports = "".join(
        f"export {name}={shlex.quote(os.environ[name])}\n" for name in _PATH_ENV if os.environ.get(name)
    )
    command = shlex.join([sys.executable, "-m", "topos.cli", *argv])
    return (
        '#!/bin/sh\ntrap \'rm -f -- "$0"\' EXIT\n' + exports +
        f"printf '\\n'; printf {_sh_text(title)}; printf '\\n\\n'\n" + command +
        '\nresult=$?\nif [ "$result" -eq 0 ]; then\n'
        f"printf '\\n'; printf {_sh_text(done)}; printf '\\n'\n"
        "else\n"
        f"printf '\\n'; printf {_sh_text(not_done)}; printf '\\n'\n"
        "fi\n"
        "printf '\\nPress Return to close this window.\\n'\nread -r answer\nexit \"$result\"\n"
    )


def open_terminal_window(*, slug: str, title: str, argv: Sequence[str], done: str, not_done: str) -> bool:
    """Launch the window; True when Terminal accepted it. False off macOS or when it could not open."""
    if sys.platform != "darwin":
        return False
    folder = Path(tempfile.mkdtemp(prefix=f"topos-{slug}-"))
    script = folder / "Topos.command"
    script.write_text(build_script(title=title, argv=argv, done=done, not_done=not_done), encoding="utf-8")
    script.chmod(0o700)
    try:
        subprocess.run(["/usr/bin/open", "-a", "Terminal", str(script)], check=True,
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        script.unlink(missing_ok=True)
        folder.rmdir()
        return False
    return True
