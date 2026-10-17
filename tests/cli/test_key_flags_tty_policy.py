"""Key flags need a terminal; `.env` beats an exported TOPOS_KEY (DIGEST J5, security 9).

Guards for CONTRACTS §4 last row and PLAN §2.2 "warn about, and ignore, a TOPOS_KEY
exported in the environment that differs from .env".
"""

from __future__ import annotations

import os
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from topos.cli import commands
from topos.cli.commands import main

KEY_FLAGS_MESSAGE = (
    "Error: `--set-topos-key` and `--topos-key` need a terminal. Run `topos-node pair` "
    "instead — it connects this computer without ever showing the key."
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOPOS_ENV_FILE", raising=False)
    # setenv-then-delenv: delenv alone records nothing for an ABSENT variable, so a
    # value the code under test sets for real would leak into later tests.
    monkeypatch.setenv("TOPOS_KEY", "")
    monkeypatch.delenv("TOPOS_KEY")
    return tmp_path


def test_set_topos_key_without_a_tty_exits_2_and_names_pair(home: Path, monkeypatch):
    monkeypatch.setattr(commands, "_can_prompt_for_input", lambda: False)
    result = CliRunner().invoke(main, ["--set-topos-key", "tpn_" + "a" * 40])
    assert result.exit_code == 2
    assert result.stderr.strip() == KEY_FLAGS_MESSAGE
    assert not (home / ".topos" / ".env").exists()


def test_topos_key_flag_without_a_tty_exits_2(home: Path, monkeypatch):
    monkeypatch.setattr(commands, "_can_prompt_for_input", lambda: False)
    monkeypatch.setattr(commands, "_run_node", lambda **_kw: (_ for _ in ()).throw(AssertionError("must not boot")))
    result = CliRunner().invoke(main, ["--topos-key", "tpn_" + "a" * 40])
    assert result.exit_code == 2
    assert result.stderr.strip() == KEY_FLAGS_MESSAGE
    assert "TOPOS_KEY" not in os.environ


def test_set_topos_key_with_a_tty_still_saves(home: Path, monkeypatch):
    monkeypatch.setattr(commands, "_can_prompt_for_input", lambda: True)
    result = CliRunner().invoke(main, ["--set-topos-key", "tpn_" + "b" * 40])
    assert result.exit_code == 0, result.output
    assert ("TOPOS_KEY=tpn_" + "b" * 40) in (home / ".topos" / ".env").read_text()


def test_non_tty_key_error_names_pair(home: Path, monkeypatch):
    monkeypatch.setattr(commands, "_can_prompt_for_input", lambda: False)
    with pytest.raises(click.ClickException) as excinfo:
        commands._resolve_topos_key(None, env_path=home / ".topos" / ".env")
    assert excinfo.value.message == (
        "TOPOS_KEY is not configured. Run `topos-node pair` to connect this computer "
        "(the key never appears on screen), or `topos-node pair --manual` in your own terminal."
    )
    assert "--set-topos-key" not in excinfo.value.message


def test_exported_key_that_differs_from_env_file_is_ignored_with_a_warning(home: Path, monkeypatch):
    env_path = home / ".topos" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("TOPOS_KEY=tpn_" + "f" * 40 + "\n")
    monkeypatch.setenv("TOPOS_KEY", "tpn_" + "e" * 40)
    warning = commands._apply_env_file_key_policy(env_path)
    assert warning == (
        "Ignoring TOPOS_KEY from the shell environment: it differs from the key in "
        f"{env_path}, which is the one this computer is paired with. Unset it or run "
        "`topos-node pair` again."
    )
    assert os.environ["TOPOS_KEY"] == "tpn_" + "f" * 40
    assert "tpn_" + "e" * 40 not in warning


def test_exported_key_is_used_when_env_file_has_none(home: Path, monkeypatch):
    env_path = home / ".topos" / ".env"
    monkeypatch.setenv("TOPOS_KEY", "tpn_" + "e" * 40)
    assert commands._apply_env_file_key_policy(env_path) is None
    assert os.environ["TOPOS_KEY"] == "tpn_" + "e" * 40


def test_matching_exported_key_warns_nothing(home: Path, monkeypatch):
    env_path = home / ".topos" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("TOPOS_KEY=tpn_" + "f" * 40 + "\n")
    monkeypatch.setenv("TOPOS_KEY", "tpn_" + "f" * 40)
    assert commands._apply_env_file_key_policy(env_path) is None


def test_user_env_path_honours_topos_env_file(home: Path, monkeypatch):
    assert commands._user_env_path() == home / ".topos" / ".env"
    monkeypatch.setenv("TOPOS_ENV_FILE", str(home / "elsewhere.env"))
    assert commands._user_env_path() == home / "elsewhere.env"
