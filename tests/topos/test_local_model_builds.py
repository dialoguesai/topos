"""The node's engine defaults name a model THIS machine can actually pull.

`ollama_extraction_model` and `privacy_judge_model` both defaulted to
`qwen3.5:9b-mlx` — an MLX build, so Apple Silicon and nothing else. On Windows,
Linux and Intel Macs that is a tag Ollama can never serve, and it is the tag
`config/model_packs.resolve_model` demotes a missing local pack role *to*: the
safety net and the thing it catches were the same dead model.

These defaults are now resolved from the running machine. On Apple Silicon
nothing changes, which is most of the point — this is a portability fix, not a
model change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from topos.config import local_model_builds as builds
from topos.config.local_model_builds import (
    PLATFORM_LINUX,
    PLATFORM_MACOS_ARM64,
    PLATFORM_MACOS_X86_64,
    PLATFORM_WINDOWS,
    PLATFORMS,
    current_platform,
    normalize_platform,
    tag_for_platform,
    tag_for_this_machine,
)
from topos.config.settings import DEFAULT_LOCAL_9B_MODEL, Settings

#: The control-plane copy, when this repo is checked out beside it. The two
#: files are written twice on purpose (the node cannot import the control
#: plane) and must not drift — a node that resolves a build differently from
#: the seeder would demote every pack role the seeder just bound.
_CP_COPY = (
    Path(__file__).resolve().parents[2].parent
    / "topos-control-plane"
    / "control_plane"
    / "local_model_builds.py"
)


@pytest.fixture()
def machine(monkeypatch):
    """Pretend this node runs on a given OS/arch."""

    def _set(os_name: str, arch: str) -> None:
        monkeypatch.setattr(builds._platform, "system", lambda: os_name)
        monkeypatch.setattr(builds._platform, "machine", lambda: arch)

    return _set


@pytest.mark.parametrize(
    "os_name,arch,expected",
    [
        ("Darwin", "arm64", PLATFORM_MACOS_ARM64),
        ("Darwin", "x86_64", PLATFORM_MACOS_X86_64),
        ("Windows", "AMD64", PLATFORM_WINDOWS),
        ("Linux", "x86_64", PLATFORM_LINUX),
        ("Linux", "aarch64", PLATFORM_LINUX),
        ("Haiku", "riscv64", None),
    ],
)
def test_current_platform_reads_the_running_machine(machine, os_name, arch, expected):
    machine(os_name, arch)
    assert current_platform() == expected


@pytest.mark.parametrize(
    "os_name,arch,expected_model",
    [
        ("Darwin", "arm64", "qwen3.5:9b-mlx"),
        ("Darwin", "x86_64", "qwen3.5:9b"),
        ("Windows", "AMD64", "qwen3.5:9b"),
        ("Linux", "x86_64", "qwen3.5:9b"),
        ("Linux", "aarch64", "qwen3.5:9b"),
        # An OS we do not recognise takes the portable build rather than a guess.
        ("Haiku", "riscv64", "qwen3.5:9b"),
    ],
)
def test_engine_defaults_follow_the_machine(machine, monkeypatch, os_name, arch, expected_model):
    machine(os_name, arch)
    monkeypatch.delenv("TOPOS_OLLAMA_EXTRACTION_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_EXTRACTION_MODEL", raising=False)
    monkeypatch.delenv("TOPOS_PRIVACY_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("PRIVACY_JUDGE_MODEL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.ollama_extraction_model == expected_model
    assert settings.privacy_judge_model == expected_model


@pytest.mark.parametrize("os_name,arch", [("Windows", "AMD64"), ("Linux", "x86_64"), ("Darwin", "x86_64")])
def test_no_non_apple_machine_ever_defaults_to_an_mlx_build(machine, monkeypatch, os_name, arch):
    machine(os_name, arch)
    for var in (
        "TOPOS_OLLAMA_EXTRACTION_MODEL",
        "OLLAMA_EXTRACTION_MODEL",
        "TOPOS_PRIVACY_JUDGE_MODEL",
        "PRIVACY_JUDGE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)

    assert "mlx" not in settings.ollama_extraction_model
    assert "mlx" not in settings.privacy_judge_model


def test_apple_silicon_defaults_are_unchanged_by_this_fix(machine, monkeypatch):
    """The Mac path must be byte-identical to what shipped before.

    A portability fix that also moved Macs off the build they were measured on
    would be two changes wearing one commit message.
    """
    machine("Darwin", "arm64")
    for var in ("TOPOS_OLLAMA_EXTRACTION_MODEL", "TOPOS_PRIVACY_JUDGE_MODEL"):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)

    assert settings.ollama_extraction_model == "qwen3.5:9b-mlx"
    assert settings.privacy_judge_model == "qwen3.5:9b-mlx"


def test_an_env_override_still_wins_on_every_platform(machine, monkeypatch):
    """The default is a default. Making it dynamic must not make it sticky."""
    machine("Linux", "x86_64")
    monkeypatch.setenv("TOPOS_OLLAMA_EXTRACTION_MODEL", "mistral:7b")
    monkeypatch.setenv("TOPOS_PRIVACY_JUDGE_MODEL", "gemma4:12b")

    settings = Settings(_env_file=None)

    assert settings.ollama_extraction_model == "mistral:7b"
    assert settings.privacy_judge_model == "gemma4:12b"


def test_the_default_model_constant_is_the_portable_build():
    """If this constant ever carries a build suffix, every non-Mac breaks again."""
    assert DEFAULT_LOCAL_9B_MODEL == "qwen3.5:9b"
    assert tag_for_platform(DEFAULT_LOCAL_9B_MODEL, None) == DEFAULT_LOCAL_9B_MODEL


def test_tag_for_this_machine_is_idempotent(machine):
    for os_name, arch in (("Darwin", "arm64"), ("Linux", "x86_64"), ("Windows", "AMD64")):
        machine(os_name, arch)
        once = tag_for_this_machine(DEFAULT_LOCAL_9B_MODEL)
        assert tag_for_this_machine(once) == once


@pytest.mark.skipif(not _CP_COPY.exists(), reason="control plane not checked out beside this repo")
def test_the_node_copy_agrees_with_the_control_plane_copy():
    """Both halves resolve the same tag, or the seeder and the node disagree.

    Asserted on behaviour rather than on file bytes: the node copy carries two
    extra functions (`current_platform`, `tag_for_this_machine`) that the
    control plane has no use for, so the files are deliberately not identical.
    """
    namespace: dict = {}
    exec(compile(_CP_COPY.read_text(), str(_CP_COPY), "exec"), namespace)

    assert namespace["PLATFORMS"] == PLATFORMS
    assert namespace["HARDWARE_BUILD_PLATFORMS"] == builds.HARDWARE_BUILD_PLATFORMS
    assert namespace["ACCELERATED_BUILDS"] == builds.ACCELERATED_BUILDS

    cases = [
        "qwen3.5:9b",
        "qwen3.5:9b-mlx",
        "ollama/qwen3.5:9b-mlx",
        "llama3.2:latest",
        "qwen3.5:9b-q4_K_M",
        "gemma4:12b",
        "",
    ]
    for tag in cases:
        for platform in (*PLATFORMS, None, "solaris"):
            assert namespace["tag_for_platform"](tag, platform) == tag_for_platform(tag, platform), (
                f"copies disagree on ({tag!r}, {platform!r})"
            )
            assert namespace["tag_runs_on"](tag, platform) == builds.tag_runs_on(tag, platform)

    for os_name, arch in (
        ("Darwin", "arm64"),
        ("Darwin", "x86_64"),
        ("Windows", "AMD64"),
        ("Linux", "aarch64"),
        ("Haiku", "riscv64"),
    ):
        assert namespace["normalize_platform"](os_name, arch) == normalize_platform(os_name, arch)
