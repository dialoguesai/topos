"""The live-engine key resolution, pinned.

tests/conftest.py sets TOPOS_KEY="test-key" so Settings validation passes for
the whole suite. The live pressure module reads that env var, and a plain
`if not TOPOS_KEY` cannot tell the placeholder from a real key — so the tests
stopped skipping, sent "Bearer test-key" to a healthy node, and failed 401.
Those 401s were then read as "engine not reachable" across three sessions and
waved through as environmental, which is exactly how a lane stops meaning
anything.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest

MODULE = "tests.release.iteration4.test_live_engine_pressure"


def _reload(monkeypatch, *, env_key, home):
    if env_key is None:
        monkeypatch.delenv("TOPOS_KEY", raising=False)
    else:
        monkeypatch.setenv("TOPOS_KEY", env_key)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: pathlib.Path(home)))
    return importlib.reload(importlib.import_module(MODULE))


def _node_env(tmp_path, key: str) -> pathlib.Path:
    home = tmp_path / "home"
    (home / ".topos").mkdir(parents=True)
    (home / ".topos" / ".env").write_text(f'TOPOS_KEY={key}\nALLOWED_ORIGINS=x\n')
    return home


def test_the_conftest_placeholder_is_not_treated_as_a_key(monkeypatch, tmp_path):
    """The exact failure: "test-key" must never be sent to a real node."""
    home = _node_env(tmp_path, "real-node-key")
    mod = _reload(monkeypatch, env_key="test-key", home=home)
    assert mod.TOPOS_KEY == "real-node-key"


def test_an_explicit_key_beats_the_node_env_file(monkeypatch, tmp_path):
    home = _node_env(tmp_path, "real-node-key")
    mod = _reload(monkeypatch, env_key="deliberate-key", home=home)
    assert mod.TOPOS_KEY == "deliberate-key"


def test_no_key_anywhere_resolves_empty_so_the_tests_skip(monkeypatch, tmp_path):
    """CI has no node env file — these must skip, not fail against nothing."""
    home = tmp_path / "bare"
    home.mkdir()
    mod = _reload(monkeypatch, env_key="test-key", home=home)
    assert mod.TOPOS_KEY == ""
    with pytest.raises(Exception) as exc:   # pytest.skip raises Skipped
        mod._auth_headers()
    assert "no TOPOS_KEY" in str(exc.value)


def test_a_quoted_key_in_the_env_file_is_unquoted(monkeypatch, tmp_path):
    home = _node_env(tmp_path, '"quoted-key"')
    mod = _reload(monkeypatch, env_key="test-key", home=home)
    assert mod.TOPOS_KEY == "quoted-key"
