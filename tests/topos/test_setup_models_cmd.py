"""`topos-node setup-models` — the terminal path stops needing the web UI.

The terminal install path was three lines that never mentioned Ollama, so its
owner finished setup with a running node, no model, and only a macOS-flavoured
web card to turn to. These pin the behaviour that closes that, and in
particular the two rules a setup command must not break: never download
multiple gigabytes without saying so first, and never print an instruction that
belongs to somebody else's operating system.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from topos.cli.setup_models_cmd import STARTER_TAG, setup_models_command
from topos.engine import ollama_setup_guidance as guidance_mod


class _Adapter:
    def __init__(self, reachable: bool, models: list):
        self._reachable = reachable
        self._models = models

    def is_reachable(self) -> bool:
        return self._reachable

    def list_models(self) -> list:
        return list(self._models)


def _run(args, *, platform, reachable, models, input_text=None, space_verdict=None):
    runner = CliRunner()
    adapter = _Adapter(reachable, models)
    with patch.object(guidance_mod, "current_platform", return_value=platform):
        with patch("topos.engine.backends.ollama.OllamaAdapter", lambda: adapter):
            with patch(
                "topos.cli.setup_models_cmd._pull_with_progress", return_value=0
            ) as pull:
                # The command asks Ollama directly (not via the adapter) so a
                # failed list raises instead of reading as an empty library.
                with patch(
                    "topos.cli.setup_models_cmd._list_tags_or_raise", return_value=models
                ):
                    # The starter's preflight reads the REAL volume otherwise, so
                    # whether these tests pass depends on the free space of the
                    # machine running them: the floor defaults to 10 GB and the
                    # starter needs 2, so any developer or runner under 12 GB free
                    # fails the pull tests for a reason none of them are about.
                    # It failed exactly that way here — green alone, red inside a
                    # full lane whose own temp databases had taken the volume below
                    # the floor by the time it ran. Disk is `space_verdict`'s
                    # subject and nothing else's.
                    with patch(
                        "topos.engine.disk_space.check_space_for",
                        return_value=space_verdict,
                    ):
                        result = runner.invoke(
                            setup_models_command, args, input=input_text
                        )
    return result, pull


@pytest.mark.parametrize(
    "platform,expected,forbidden",
    [("windows", "winget", "brew"), ("linux", "install.sh", "brew")],
)
def test_a_missing_ollama_names_the_command_for_this_platform(platform, expected, forbidden):
    result, _ = _run([], platform=platform, reachable=False, models=[])

    assert expected in result.output
    assert forbidden not in result.output
    # Non-zero so a scripted setup stops here rather than continuing as if a
    # model were present.
    assert result.exit_code == 1


def test_macos_gets_the_cask_which_actually_starts_the_server():
    result, _ = _run([], platform="macos-arm64", reachable=False, models=[])

    assert "brew install --cask ollama" in result.output


def test_an_installed_library_is_reported_and_nothing_is_downloaded():
    result, pull = _run(
        [], platform="linux", reachable=True, models=["llama3.2:latest", "mistral:7b"]
    )

    assert result.exit_code == 0
    assert "llama3.2:latest" in result.output
    pull.assert_not_called()


def test_an_embedding_only_library_does_not_count_as_having_a_chat_model():
    """topos pulls embedding models itself for enrichment; they answer no role."""
    result, pull = _run(
        [],
        platform="linux",
        reachable=True,
        models=["nomic-embed-text:latest", "bge-m3:latest"],
        input_text="n\n",
    )

    assert "no chat model is installed" in result.output
    pull.assert_not_called()


def test_the_download_names_its_size_before_asking():
    """No silent multi-GB pull, matching the web CTA's rule (D4)."""
    result, _ = _run([], platform="linux", reachable=True, models=[], input_text="n\n")

    assert STARTER_TAG in result.output
    assert "2.0 GB" in result.output


def test_declining_the_download_leaves_the_command_to_run_later():
    result, pull = _run([], platform="linux", reachable=True, models=[], input_text="n\n")

    pull.assert_not_called()
    assert f"ollama pull {STARTER_TAG}" in result.output
    assert result.exit_code == 0


def test_yes_skips_the_prompt_and_pulls_the_starter():
    result, pull = _run(["--yes"], platform="linux", reachable=True, models=[])

    assert result.exit_code == 0
    pull.assert_called_once_with(STARTER_TAG)


def test_an_explicit_model_overrides_the_starter():
    result, pull = _run(
        ["--yes", "--model", "mistral:7b"], platform="linux", reachable=True, models=[]
    )

    assert result.exit_code == 0
    pull.assert_called_once_with("mistral:7b")


def test_the_terminal_path_never_tells_the_owner_to_open_the_web_ui():
    """The whole point: setup completes without leaving the shell."""
    result, _ = _run([], platform="windows", reachable=False, models=[])

    lowered = result.output.lower()
    for phrase in ("open the app", "in the browser", "settings →", "web"):
        assert phrase not in lowered, f"terminal path sent the owner to the UI: {phrase}"


def test_a_failed_model_list_never_offers_a_download():
    """A timed-out /api/tags must not read as "your library is empty".

    `OllamaAdapter.list_models` swallows every error into `[]`, so a blip on a
    machine holding twenty models used to land in the no-chat-model branch and
    offer another 2 GB — unconditionally, under `--yes`.
    """
    runner = CliRunner()
    adapter = _Adapter(True, [])
    with patch.object(guidance_mod, "current_platform", return_value="linux"):
        with patch("topos.engine.backends.ollama.OllamaAdapter", lambda: adapter):
            with patch(
                "topos.cli.setup_models_cmd._pull_with_progress", return_value=0
            ) as pull:
                with patch(
                    "topos.cli.setup_models_cmd._list_tags_or_raise",
                    side_effect=TimeoutError("read timed out"),
                ):
                    result = runner.invoke(setup_models_command, ["--yes"])

    pull.assert_not_called()
    assert result.exit_code == 1
    assert "would not list its models" in result.output
    assert "Nothing was downloaded" in result.output


def test_the_starter_refuses_before_the_transfer_when_it_will_not_fit():
    """The starter's own preflight, which until now only ran by accident.

    `--yes --model X` is guarded by the pull stream (below); the curated starter
    is guarded earlier, by `check_space_for`, because the CLI knows its size
    without asking. That branch had no test — it was exercised only on a machine
    that happened to be short of space, which is the one condition under which a
    passing suite tells you nothing.
    """
    from topos.engine.disk_space import SpaceVerdict

    verdict = SpaceVerdict(
        needed_bytes=2_000_000_000,
        free_bytes=11_000_000_000,
        reserve_bytes=10_000_000_000,
        path="/home/x/.ollama/models",
    )

    result, pull = _run(
        ["--yes"], platform="linux", reachable=True, models=[], space_verdict=verdict
    )

    # Refused before the transfer, not at 97%.
    pull.assert_not_called()
    assert result.exit_code == 1
    assert "Not enough disk space" in result.output
    assert "Free up some space" in result.output


def test_an_arbitrary_model_is_now_guarded_too():
    """The asymmetry this closes: only the curated starter had a size to check.

    The CLI used to shell out to `ollama pull`, so it never saw the stream's own
    total — a `--model` the owner named downloaded unguarded until the disk
    filled. Pulling through the adapter puts every tag under the same rule.
    """
    from topos.engine.backends.ollama import PullAborted
    from topos.engine.disk_space import SpaceVerdict

    verdict = SpaceVerdict(
        needed_bytes=40_000_000_000,
        free_bytes=1_000_000_000,
        reserve_bytes=2 * 1024**3,
        path="/home/x/.ollama/models",
    )
    runner = CliRunner()
    adapter = _Adapter(True, [])

    with patch.object(guidance_mod, "current_platform", return_value="linux"):
        with patch("topos.engine.backends.ollama.OllamaAdapter", lambda: adapter):
            with patch("topos.cli.setup_models_cmd._list_tags_or_raise", return_value=[]):
                with patch(
                    "topos.cli.setup_models_cmd._pull_with_progress",
                    side_effect=PullAborted(verdict.message("huge:latest")),
                ):
                    result = runner.invoke(
                        setup_models_command, ["--yes", "--model", "huge:latest"]
                    )

    assert result.exit_code == 1
    assert "Not enough disk space" in result.output
    assert "Free up some space" in result.output


def test_the_pull_goes_to_the_host_we_probed_not_the_local_binary():
    """A node pointed at a remote Ollama has no local binary and must not need one.

    The old subprocess path ran `ollama pull` locally while reachability had
    been probed against engine_ollama_base_url — so the download landed in the
    wrong daemon, or failed for want of a binary the setup does not use.
    """
    import ast
    import inspect
    import textwrap

    from topos.cli import setup_models_cmd

    # Parsed, not grepped: the function's own docstring names Popen to explain
    # why it does not use it, and a substring check reads that as the defect.
    tree = ast.parse(textwrap.dedent(inspect.getsource(setup_models_cmd._pull_with_progress)))
    called = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not any("Popen" in name for name in called), f"the CLI is shelling out again: {called}"
    assert any("pull_model" in name for name in called), (
        f"the CLI is not pulling through the adapter: {called}"
    )
