"""``preview_ai_tool_source`` lists the project folders a person can pass back as ``--skip`` (CONTRACTS §2.6, §2.8).

Every tool's REAL reader runs over its synthetic fixture tree; nothing here opens ``~/.claude``, ``~/.codex``,
``~/.cursor`` or ``~/.topos``. The ``folders`` list is the facet each reader lifts off its rows: the repo folder's
basename, never a home path or a Claude Code path slug. Every expected value is a literal from the fixtures.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fixtures.ai_coding_tools.codex.build_state_db import build_state_db as build_codex_db
from tests.fixtures.ai_coding_tools.common_stub import stubbed_common
from tests.fixtures.ai_coding_tools.cursor.build_state_db import PROJECT_SLUG, build_state_db as build_cursor_db
from topos.ingestion import ai_tool_sync

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ai_coding_tools"
# Shapes a listed folder must never take: a path, a home-path slug, the home placeholder.
PATH_SHAPED = ("/", "\\", "-Users-", "-home-", "~")


def _preview(monkeypatch: pytest.MonkeyPatch, read, source_id: str, **kwargs):
    monkeypatch.setattr(ai_tool_sync, "_reader_for", lambda tool: read)
    monkeypatch.setattr(ai_tool_sync, "node_secret_values", lambda conn: ())
    return ai_tool_sync.preview_ai_tool_source(None, source_id, **kwargs)


def _assert_listed_names_are_bare(folders) -> None:
    assert folders, "a preview over sessions with a project must list at least one folder"
    for name in folders:
        assert isinstance(name, str) and name
        assert not any(marker in name for marker in PATH_SHAPED), name


# --- Claude Code ---------------------------------------------------------------------------------


@pytest.fixture
def claude_code_read():
    from topos.ingestion.sources.claude_code_reader import read_claude_code_batch

    def read(**kwargs):
        return read_claude_code_batch(**{**kwargs, "root_path": FIXTURES / "claude_code" / "two_folders"},
                                      hostnames=(), usernames=())

    return read


def test_claude_code_preview_lists_each_project_folder_by_basename(claude_code_read, monkeypatch) -> None:
    result = _preview(monkeypatch, claude_code_read, "claude_code_sessions")
    assert result["sessions"] == 2 and result["turns"] == 4
    assert result["folders"] == ["secretproj", "widget"]
    _assert_listed_names_are_bare(result["folders"])


def test_claude_code_preview_skip_takes_a_listed_name_and_drops_its_sessions(claude_code_read, monkeypatch) -> None:
    result = _preview(monkeypatch, claude_code_read, "claude_code_sessions", skip=["secretproj"])
    assert result["folders"] == ["widget"]
    assert result["sessions"] == 1 and result["turns"] == 2
    assert result["skip"] == ["secretproj"]
    # The merged ledger counts the folder once per batch walked (the reader's own guard pins one per read).
    assert result["ledger"]["dropped"]["skipped_folder"] >= 1 and set(result["ledger"]["dropped"]) == {"skipped_folder"}


def test_claude_code_preview_never_lists_the_path_slug_a_folder_is_stored_under(claude_code_read, monkeypatch) -> None:
    result = _preview(monkeypatch, claude_code_read, "claude_code_sessions")
    blob = json.dumps(result["folders"])
    for forbidden in ("-Users-fake", "-~-dev", "/Users/fake", "fake"):
        assert forbidden not in blob, (forbidden, blob)


# --- Codex ---------------------------------------------------------------------------------------


@pytest.fixture
def codex_read(tmp_path: Path):
    root = tmp_path / "codex_home"
    shutil.copytree(FIXTURES / "codex", root, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    build_codex_db(root)
    with stubbed_common():
        reader = importlib.import_module("topos.ingestion.sources.codex_reader")

    def read(**kwargs):
        return reader.read_codex_batch(**{**kwargs, "root_path": root})

    return read


def test_codex_preview_lists_project_basenames(codex_read, monkeypatch) -> None:
    result = _preview(monkeypatch, codex_read, "codex_sessions")
    assert result["folders"] == ["alpha-project", "delta-project", "epsilon-project", "eta-project", "gamma-project"]
    assert result["sessions"] == 6 and result["turns"] == 23  # test_codex_reader.TOTAL_ROWS
    _assert_listed_names_are_bare(result["folders"])


def test_codex_preview_skip_takes_a_listed_name(codex_read, monkeypatch) -> None:
    result = _preview(monkeypatch, codex_read, "codex_sessions", skip=["alpha-project"])
    assert result["folders"] == ["delta-project", "epsilon-project", "eta-project", "gamma-project"]
    assert result["sessions"] == 4 and result["turns"] == 17  # alpha's two threads hold 6 of the 23 rows


def _real_reader(name: str):
    """The reader bound to L4's REAL ``common`` (the stub's scrub is identity, so it cannot show a home folder
    being withheld). ``stubbed_common`` drops reader modules on exit, so a fresh import binds the real one."""
    sys.modules.pop(f"topos.ingestion.sources.{name}", None)
    return importlib.import_module(f"topos.ingestion.sources.{name}")


def test_codex_preview_withholds_the_login_name_for_a_session_run_from_the_home_folder(tmp_path, monkeypatch) -> None:
    root = tmp_path / "codex_home"
    shutil.copytree(FIXTURES / "codex", root, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    build_codex_db(root)
    gamma = next(root.glob("sessions/2026/10/15/rollout-*-a7a7a7a70007.jsonl"))
    lines = gamma.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    assert meta["payload"]["cwd"] == "/Users/someone/dev/gamma-project"
    meta["payload"]["cwd"] = "/Users/someone"
    gamma.write_text("\n".join([json.dumps(meta)] + lines[1:]) + "\n", encoding="utf-8")
    reader = _real_reader("codex_reader")
    result = _preview(monkeypatch, lambda **kw: reader.read_codex_batch(**{**kw, "root_path": root}), "codex_sessions")
    assert result["folders"] == ["alpha-project", "delta-project", "epsilon-project", "eta-project"]
    assert result["sessions"] == 6 and result["turns"] == 23
    assert "someone" not in json.dumps(result["folders"])


def test_cursor_preview_withholds_the_login_name_for_a_workspace_opened_on_the_home_folder(tmp_path, monkeypatch) -> None:
    home = tmp_path / "cursor_home"
    shutil.copytree(FIXTURES / "cursor" / "projects", home / "projects")
    user_dir = tmp_path / "CursorUser"
    db = build_cursor_db(user_dir)
    specs = list(user_dir.glob("workspaceStorage/*/workspace.json"))
    assert len(specs) == 1 and json.loads(specs[0].read_text())["folder"] == "file:///Users/someone/dev/beta-project"
    specs[0].write_text(json.dumps({"folder": "file:///Users/someone"}), encoding="utf-8")
    reader = _real_reader("cursor_reader")
    result = _preview(monkeypatch, lambda **kw: reader.read_cursor_batch(**{**kw, "root_path": home, "state_db_path": db}),
                      "cursor_sessions")
    assert result["folders"] == []
    assert result["sessions"] == 4 and result["turns"] == 16
    assert "someone" not in json.dumps(result)


# --- Cursor --------------------------------------------------------------------------------------


@pytest.fixture
def cursor_read(tmp_path: Path):
    home = tmp_path / "cursor_home"
    shutil.copytree(FIXTURES / "cursor" / "projects", home / "projects")
    db = build_cursor_db(tmp_path / "CursorUser")
    assert (home / "projects" / PROJECT_SLUG / "agent-transcripts").is_dir()
    with stubbed_common():
        reader = importlib.import_module("topos.ingestion.sources.cursor_reader")

    def read(**kwargs):
        return reader.read_cursor_batch(**{**kwargs, "root_path": home, "state_db_path": db})

    return SimpleNamespace(read=read)


def test_cursor_preview_lists_project_basenames(cursor_read, monkeypatch) -> None:
    result = _preview(monkeypatch, cursor_read.read, "cursor_sessions")
    assert result["folders"] == ["beta-project"]
    assert result["sessions"] == 4 and result["turns"] == 16  # test_cursor_reader.TOTAL_ROWS
    _assert_listed_names_are_bare(result["folders"])


def test_cursor_preview_skip_takes_a_listed_name(cursor_read, monkeypatch) -> None:
    result = _preview(monkeypatch, cursor_read.read, "cursor_sessions", skip=["beta-project"])
    assert result["folders"] == []
    # The empty-window composer has no project and stays; only beta-project's sessions leave.
    assert result["sessions"] == 1 and result["turns"] == 2
