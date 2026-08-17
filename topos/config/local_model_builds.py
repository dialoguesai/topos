"""Which build of a local model tag THIS machine can actually run.

An Ollama tag may name a hardware-specific build. ``qwen3.5:9b-mlx`` is one:
MLX is Apple's array framework, so that tag exists for Apple-Silicon Macs and
for nothing else. It was nevertheless the engine default for LLM fact/goal
extraction and for the privacy judge, so on Windows, Linux and Intel Macs the
node asked Ollama for a model that machine can never have — and the pack
resolver's local-role demotion (``config/model_packs.resolve_model``) landed on
the same dead tag it was demoting to.

The rule this module encodes: a curated local tag is stored in its PORTABLE
build, and a platform is given an accelerated build only where we have
confirmed one is published for that exact tag. Everything else — an
unrecognised OS, a tag with no row in ``ACCELERATED_BUILDS`` — resolves to the
portable build. The two mistakes are not symmetric: the portable build on Apple
Silicon is slower, the MLX build anywhere else does not exist.

``ACCELERATED_BUILDS`` is a table of verified tags rather than a suffix rule on
purpose. Deriving ``<tag>-mlx`` mechanically would invent ``llama3.2:latest-mlx``
— the same "name a tag nobody can pull" defect as the hardcode, moved one level
up. Adding a row is a claim that the build exists in the Ollama library.

Node mirror of ``control_plane/local_model_builds.py``. Written twice on
purpose, for the reason ``config/model_packs.resolve_model`` gives: the node
and the control plane deploy independently and the node cannot import the
control plane. ``tests/topos/test_local_model_builds.py`` asserts the two
copies agree. Change one, change the other.

Stdlib-only and side-effect free: this is imported from ``config/settings.py``,
which every other import in the tree sits behind.
"""

from __future__ import annotations

import platform as _platform
from typing import Any, Dict, Optional, Tuple

#: Apple Silicon — the only platform that runs MLX builds.
PLATFORM_MACOS_ARM64 = "macos-arm64"
#: Intel Macs. macOS, and still no MLX: the framework needs Apple's own GPU.
#: Keeping it distinct from `macos-arm64` is the whole reason this axis is
#: (os, arch) and not os alone.
PLATFORM_MACOS_X86_64 = "macos-x86_64"
PLATFORM_WINDOWS = "windows"
PLATFORM_LINUX = "linux"

PLATFORMS: Tuple[str, ...] = (
    PLATFORM_MACOS_ARM64,
    PLATFORM_MACOS_X86_64,
    PLATFORM_WINDOWS,
    PLATFORM_LINUX,
)

#: Tag suffixes that restrict the HARDWARE a build runs on, and the platforms
#: each one runs on. Quantization suffixes (`-q4_K_M`, `-fp16`) are deliberately
#: absent: they are portable, and stripping one would move an owner off the
#: build they chose.
BUILD_MLX = "mlx"
HARDWARE_BUILD_PLATFORMS: Dict[str, Tuple[str, ...]] = {
    BUILD_MLX: (PLATFORM_MACOS_ARM64,),
}

#: Portable tag -> the accelerated build published for it, per platform.
#: A tag absent here resolves to itself on every platform, which is always
#: correct and never a 404. Every entry is a claim that the tag exists in the
#: Ollama library — verify before adding one.
ACCELERATED_BUILDS: Dict[str, Dict[str, str]] = {
    "qwen3.5:9b": {PLATFORM_MACOS_ARM64: "qwen3.5:9b-mlx"},
}

_ARM_ARCHES = frozenset({"arm64", "aarch64", "arm"})
_X86_ARCHES = frozenset({"x86_64", "amd64", "x64", "i386", "i686"})


def normalize_platform(os_name: Any, arch: Any = None) -> Optional[str]:
    """One of ``PLATFORMS`` from an OS/arch pair, or None when unrecognised.

    Accepts what ``platform.system()`` / ``platform.machine()`` produce on each
    OS — including Windows' ``AMD64`` and Linux' ``aarch64`` — because that is
    the shape the node reports in ``system_info``.

    macOS with an unreadable arch answers None rather than guessing
    ``macos-arm64``: guessing Apple Silicon on an Intel Mac is precisely the
    404 this module exists to prevent, and None already means "use the
    portable build".
    """
    system = str(os_name or "").strip().lower()
    machine = str(arch or "").strip().lower()
    if system in ("darwin", "macos", "mac os x", "macosx", "osx"):
        if machine in _ARM_ARCHES:
            return PLATFORM_MACOS_ARM64
        if machine in _X86_ARCHES:
            return PLATFORM_MACOS_X86_64
        return None
    if system.startswith("win"):
        return PLATFORM_WINDOWS
    if system == "linux":
        return PLATFORM_LINUX
    return None


def _strip_provider(tag: Any) -> str:
    """``"ollama/qwen3.5:9b"`` -> ``"qwen3.5:9b"``. Case is preserved."""
    text = str(tag or "").strip()
    return text.rsplit("/", 1)[-1] if "/" in text else text


def build_of(tag: Any) -> Optional[str]:
    """The hardware build `tag` names, or None when it is the portable build."""
    _, _, version = _strip_provider(tag).partition(":")
    lowered = version.lower()
    for build in HARDWARE_BUILD_PLATFORMS:
        if lowered.endswith(f"-{build}"):
            return build
    return None


def base_tag(tag: Any) -> str:
    """`tag` with any hardware build suffix removed — the portable build.

    ``qwen3.5:9b-mlx`` → ``qwen3.5:9b``. A tag with no hardware suffix comes
    back unchanged, quantizations and their casing included: ``:9b-q4_K_M``
    runs everywhere, so there is nothing to strip and nothing to normalise.
    """
    text = _strip_provider(tag)
    build = build_of(text)
    return text[: -len(f"-{build}")] if build is not None else text


def tag_for_platform(tag: Any, platform: Optional[str]) -> str:
    """`tag` built for `platform` — accelerated where one is published, else portable.

    ``("qwen3.5:9b", "macos-arm64")`` → ``qwen3.5:9b-mlx``
    ``("qwen3.5:9b-mlx", "linux")``   → ``qwen3.5:9b``
    ``("qwen3.5:9b-mlx", None)``      → ``qwen3.5:9b``
    ``("llama3.2:latest", "macos-arm64")`` → ``llama3.2:latest`` (no MLX build)

    An unknown platform takes the portable build deliberately: a node we have
    not heard from is not an Apple-Silicon node, and the portable build runs
    everywhere.
    """
    base = base_tag(tag)
    if not base:
        return ""
    key = str(platform or "").strip().lower()
    return ACCELERATED_BUILDS.get(base.lower(), {}).get(key, base)


def tag_runs_on(tag: Any, platform: Optional[str]) -> bool:
    """Whether `tag`'s build can run on `platform`.

    True for an unknown platform: "we did not check" must not read as "this
    machine cannot run it" and demote a binding on no evidence — the same
    None-is-not-a-finding rule ``installed_local_models`` is built on.
    """
    build = build_of(tag)
    if build is None:
        return True
    key = str(platform or "").strip().lower()
    if key not in PLATFORMS:
        return True
    return key in HARDWARE_BUILD_PLATFORMS.get(build, ())


def current_platform() -> Optional[str]:
    """This machine's platform, or None when the interpreter will not say.

    Not cached: it is two stdlib calls, and the callers are settings defaults
    and tests that need to patch the answer.
    """
    try:
        return normalize_platform(_platform.system(), _platform.machine())
    except Exception:  # noqa: BLE001 — a settings default may not fail to import
        return None


def tag_for_this_machine(tag: Any) -> str:
    """`tag` built for the machine this node is running on."""
    return tag_for_platform(tag, current_platform())
