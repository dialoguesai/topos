"""How to get Ollama onto THIS machine, in one place for every surface.

Three surfaces ask the same question and used to answer it three different
ways — badly, off macOS:

  * the one-click install refusal (`ollama_install.start_install`) said only
    "available on macOS, use the manual steps below";
  * the web setup card hard-coded ``brew install ollama`` and showed it to
    everyone, so a Windows owner was handed a Homebrew command;
  * the terminal install path never mentioned Ollama at all, which left a
    `topos-node` owner with a running node, no local model, and nowhere to go
    but the macOS-flavoured card.

So the instructions live here, keyed on the platform the NODE runs on (not the
browser's — the two are usually the same machine but nothing guarantees it, and
this module answers for the machine that has to run the model). Every surface
renders the same strings, and adding a platform means editing one table.

`can_auto_install` is deliberately narrow. Only macOS is automated: Ollama's
official script wants sudo/systemd on Linux, which a headless node must not
attempt, and Windows ships a GUI installer with no unattended contract we have
tested. Refusing loudly with a command the owner can paste beats a silent
half-install on someone else's box.

One naming trap worth stating, because the sentinel is shared with
``local_model_builds`` and means something different there: in THIS module
``platform=None`` means "the machine this node is running on" (these functions
exist to answer for the local box), whereas in ``local_model_builds`` a None
platform means "we could not identify the machine". An unidentifiable OS still
reaches the same place here — ``current_platform()`` returns None and the
lookup falls to ``_UNKNOWN`` — so pass an explicit platform string when you
mean "some other machine".

Stdlib-only: imported from the CLI, which must stay cheap to start.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from topos.config.local_model_builds import (
    PLATFORM_LINUX,
    PLATFORM_MACOS_ARM64,
    PLATFORM_MACOS_X86_64,
    PLATFORM_WINDOWS,
    current_platform,
)

OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"


#: The CASK, not the formula. `brew install ollama` drops the binary and starts
#: nothing, so :11434 stays closed and every surface that gates on reachability
#: ("Then run `topos-node setup-models` again", the card's 4s poll) loops
#: forever telling the owner to run a command they already ran. The cask
#: installs Ollama.app, which starts the server and is what
#: `ollama_install.default_app_present` already looks for.
_MACOS = {
    "label": "macOS",
    "auto": True,
    "commands": ["brew install --cask ollama"],
    "note": "Or install it for you — no terminal needed.",
}
_WINDOWS = {
    "label": "Windows",
    "auto": False,
    # winget ships with Windows 10 1709+ and needs no admin prompt for this
    # package; the download link is the fallback for older builds.
    "commands": ["winget install --id Ollama.Ollama -e"],
    "note": "Or download the installer and run it.",
}
_LINUX = {
    "label": "Linux",
    # The official script installs a systemd unit and wants sudo. That is the
    # owner's call to make in their own shell, not a headless node's.
    "auto": False,
    "commands": ["curl -fsSL https://ollama.com/install.sh | sh"],
    "note": "Runs as your user and asks for sudo to install the service.",
}
_UNKNOWN = {
    "label": "your machine",
    "auto": False,
    "commands": [],
    "note": "Download the build for your platform.",
}

_BY_PLATFORM: Dict[str, Dict[str, Any]] = {
    PLATFORM_MACOS_ARM64: _MACOS,
    PLATFORM_MACOS_X86_64: _MACOS,
    PLATFORM_WINDOWS: _WINDOWS,
    PLATFORM_LINUX: _LINUX,
}

#: Derived, never hand-listed: a second copy of "which platforms can we drive"
#: would let the card offer a one-click button that `start_install` refuses.
AUTO_INSTALL_PLATFORMS = frozenset(
    platform for platform, entry in _BY_PLATFORM.items() if entry["auto"]
)


#: Pass this where you mean "a machine I could not identify". `None` cannot say
#: that here — it means "this node's machine" — and `normalize_platform` returns
#: exactly `None` for an unrecognised OS, so piping one into the other answered
#: for the LOCAL box: a refusal for a foreign platform came back recommending
#: macOS one-click.
PLATFORM_UNKNOWN = "unknown"


def install_guidance(platform: Optional[str] = None) -> Dict[str, Any]:
    """What to tell the owner about installing Ollama on `platform`.

    Shape: ``{platform, label, auto_install, commands, note, download_url}``.
    `platform` defaults to the machine this node is running on. An
    unrecognised platform still returns a usable card — the download page
    covers every OS Ollama ships — rather than an empty one.
    """
    key = platform if platform is not None else current_platform()
    entry = _BY_PLATFORM.get(str(key or ""), _UNKNOWN)
    return {
        "platform": key,
        "label": entry["label"],
        "auto_install": bool(entry["auto"]),
        "commands": list(entry["commands"]),
        "note": entry["note"],
        "download_url": OLLAMA_DOWNLOAD_URL,
    }


def can_auto_install(platform: Optional[str] = None) -> bool:
    """Whether this node may run the install itself on `platform`."""
    key = platform if platform is not None else current_platform()
    return str(key or "") in AUTO_INSTALL_PLATFORMS


def manual_install_message(platform: Optional[str] = None) -> str:
    """One sentence naming what to run, for a refusal or a terminal prompt.

    Always names a concrete next action. The old refusal — "only available on
    macOS. Use the manual steps below." — told a Windows owner what they could
    not do and then pointed at a card showing a Homebrew command.
    """
    guidance = install_guidance(platform)
    if guidance["commands"]:
        return (
            f"Install Ollama on {guidance['label']} with: {guidance['commands'][0]} "
            f"(or download it from {guidance['download_url']})."
        )
    return f"Install Ollama for {guidance['label']} from {guidance['download_url']}."


def install_command_lines(platform: Optional[str] = None) -> List[str]:
    """Just the commands, for a terminal transcript."""
    return list(install_guidance(platform)["commands"])
