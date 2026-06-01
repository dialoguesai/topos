from __future__ import annotations

import os
from pathlib import Path

import click
import pytest

from topos.cli import commands


def test_resolve_topos_key_prompts_and_persists(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TOPOS_KEY", raising=False)
    monkeypatch.setattr(commands, "_can_prompt_for_input", lambda: True)
    monkeypatch.setattr(commands, "_prompt_for_topos_key", lambda: "prompted-test-key")

    messages: list[str] = []
    monkeypatch.setattr(commands.click, "echo", lambda msg: messages.append(msg))

    env_path = tmp_path / ".topos" / ".env"
    resolved = commands._resolve_topos_key(None, env_path=env_path)

    assert resolved == "prompted-test-key"
    assert os.environ["TOPOS_KEY"] == "prompted-test-key"
    assert env_path.exists()
    assert env_path.read_text(encoding="utf-8").strip() == "TOPOS_KEY=prompted-test-key"
    assert any("Saved TOPOS_KEY" in msg for msg in messages)
    assert any("Connecting with saved TOPOS_KEY" in msg for msg in messages)


def test_resolve_topos_key_raises_when_not_promptable(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TOPOS_KEY", raising=False)
    monkeypatch.setattr(commands, "_can_prompt_for_input", lambda: False)

    with pytest.raises(click.ClickException, match="TOPOS_KEY is not configured"):
        commands._resolve_topos_key(None, env_path=tmp_path / ".topos" / ".env")


def test_resolve_topos_key_uses_cli_value_without_persist(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TOPOS_KEY", raising=False)

    env_path = tmp_path / ".topos" / ".env"
    resolved = commands._resolve_topos_key("cli-override-key", env_path=env_path)

    assert resolved == "cli-override-key"
    assert os.environ["TOPOS_KEY"] == "cli-override-key"
    assert not env_path.exists()
