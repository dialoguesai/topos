"""Install guidance is answered for the machine the NODE runs on.

Three surfaces used to answer this differently and all three were wrong off
macOS: the one-click refusal said only "available on macOS", the web card
hard-coded `brew install ollama` for everyone, and the terminal path never
mentioned Ollama at all. They now share this table.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from topos.config.local_model_builds import (
    PLATFORM_LINUX,
    PLATFORM_MACOS_ARM64,
    PLATFORM_MACOS_X86_64,
    PLATFORM_WINDOWS,
)
from topos.engine import ollama_install
from topos.engine.ollama_setup_guidance import (
    can_auto_install,
    install_command_lines,
    install_guidance,
    manual_install_message,
)


@pytest.fixture(autouse=True)
def _clean_job():
    """Reset around each test, and let any install worker finish first.

    The worker is a daemon thread that writes the module global; resetting
    without waiting lets its write land AFTER the reset and repopulate _JOB for
    whatever runs next.
    """
    ollama_install.reset_job()
    yield
    for thread in threading.enumerate():
        if thread.name == "ollama-install":
            thread.join(timeout=5.0)
    ollama_install.reset_job()


def test_no_platform_is_told_to_run_homebrew_unless_it_is_a_mac():
    """The exact defect: a Windows owner handed `brew install ollama`."""
    for platform in (PLATFORM_WINDOWS, PLATFORM_LINUX):
        commands = " ".join(install_command_lines(platform))
        assert "brew" not in commands, f"{platform} was told to run: {commands}"


def test_each_platform_names_a_command_that_belongs_to_it():
    assert "brew" in install_command_lines(PLATFORM_MACOS_ARM64)[0]
    assert "brew" in install_command_lines(PLATFORM_MACOS_X86_64)[0]
    assert "winget" in install_command_lines(PLATFORM_WINDOWS)[0]
    assert "install.sh" in install_command_lines(PLATFORM_LINUX)[0]


def test_only_macos_may_be_installed_by_the_node_itself():
    """Linux wants sudo/systemd; Windows has no unattended contract we tested."""
    assert can_auto_install(PLATFORM_MACOS_ARM64) is True
    assert can_auto_install(PLATFORM_MACOS_X86_64) is True
    assert can_auto_install(PLATFORM_WINDOWS) is False
    assert can_auto_install(PLATFORM_LINUX) is False
    # An OS we cannot identify is not a Mac, so it is not auto-installable.
    assert can_auto_install("solaris") is False


def test_passing_no_platform_answers_for_this_machine():
    """`None` here means "this node's machine" — NOT "unknown", which is what
    the same sentinel means in `local_model_builds`. Pinned because the two
    modules are read together and the mismatch is easy to trip over."""
    with patch(
        "topos.engine.ollama_setup_guidance.current_platform", return_value=PLATFORM_WINDOWS
    ):
        assert install_guidance()["platform"] == PLATFORM_WINDOWS
        assert can_auto_install() is False
        assert "winget" in manual_install_message()


def test_an_unknown_platform_still_gets_a_usable_card():
    """The download page covers every OS Ollama ships — an empty card does not."""
    guidance = install_guidance("solaris")

    assert guidance["download_url"].startswith("https://")
    assert guidance["auto_install"] is False
    assert guidance["download_url"] in manual_install_message("solaris")


@pytest.mark.parametrize(
    "system,machine,expect",
    [("Windows", "AMD64", "winget"), ("Linux", "x86_64", "install.sh")],
)
def test_the_one_click_refusal_names_the_command_for_this_machine(system, machine, expect):
    """It used to say what macOS could do, then point at a Homebrew card."""
    with patch.object(ollama_install.platform_mod, "machine", return_value=machine):
        record = ollama_install.start_install(platform=system)

    assert record["refused"] is True
    assert record["reason"] == "unsupported_platform"
    assert expect in record["error"], record["error"]
    assert "brew" not in record["error"]
    # No `guidance` key by design: OllamaInstallStatusResponse does not declare
    # one, so pydantic drops it at the control plane and no browser ever sees
    # it. The card reads guidance from the models payload instead.
    assert "guidance" not in record
    # Every field here must survive that model, or the refusal says nothing.
    assert set(record) <= {"state", "status", "error", "refused", "reason"}


def test_macos_still_installs_rather_than_refusing():
    """The path that already worked must be untouched by this.

    The installer runs on a daemon thread, so the returned record is a snapshot
    taken before any work happens — asserting only on it would pass even if the
    worker never started. Wait for a terminal state and check the runner ran.
    """
    import time

    started = threading.Event()

    def _run(**_):
        started.set()
        return (0, "ok")

    record = ollama_install.start_install(
        platform="Darwin",
        run_install=_run,
        app_present=lambda: False,
        open_app=lambda: None,
        is_reachable=lambda: True,
    )

    assert record.get("refused") is not True
    assert record["state"] == "installing"
    assert started.wait(timeout=5.0), "the install worker never ran"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ollama_install.install_status()["state"] != "installing":
            break
        time.sleep(0.01)
    assert ollama_install.install_status()["state"] == "started"


def test_an_unrecognised_platform_refusal_does_not_recommend_this_machine():
    """`normalize_platform` says None for "unknown"; `install_guidance(None)`
    means "this machine". Piping one into the other made a refusal for a foreign
    OS come back recommending macOS one-click on a Mac."""
    with patch.object(ollama_install.platform_mod, "machine", return_value="arm64"):
        record = ollama_install.start_install(platform="SunOS")

    assert record["refused"] is True
    assert "brew" not in record["error"], record["error"]
    assert "macOS" not in record["error"], record["error"]
