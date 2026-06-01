from __future__ import annotations

from types import SimpleNamespace

from topos.cli import commands


def test_update_check_installs_when_user_confirms(monkeypatch):
    monkeypatch.setattr(commands, "_get_installed_package_version", lambda _: "0.1.0")
    monkeypatch.setattr(commands, "_get_latest_pypi_version", lambda _: "0.1.1")
    monkeypatch.setattr(commands, "_can_prompt_for_input", lambda: True)
    monkeypatch.setattr(commands.click, "confirm", lambda *_args, **_kwargs: True)

    called = {"upgrade": False}

    def _fake_run(cmd, check):
        called["upgrade"] = cmd == ["uv", "tool", "upgrade", "topos-node"] and check is False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(commands.subprocess, "run", _fake_run)

    assert commands._maybe_offer_self_update(skip_update_check=False) is True
    assert called["upgrade"] is True


def test_update_check_skips_when_not_tty(monkeypatch):
    monkeypatch.setattr(commands, "_get_installed_package_version", lambda _: "0.1.0")
    monkeypatch.setattr(commands, "_get_latest_pypi_version", lambda _: "0.1.1")
    monkeypatch.setattr(commands, "_can_prompt_for_input", lambda: False)
    monkeypatch.setattr(commands.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run")))

    assert commands._maybe_offer_self_update(skip_update_check=False) is False


def test_update_check_noop_when_up_to_date(monkeypatch):
    monkeypatch.setattr(commands, "_get_installed_package_version", lambda _: "0.1.1")
    monkeypatch.setattr(commands, "_get_latest_pypi_version", lambda _: "0.1.1")
    monkeypatch.setattr(commands.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run")))

    assert commands._maybe_offer_self_update(skip_update_check=False) is False


def test_update_check_skips_via_env(monkeypatch):
    monkeypatch.setenv("TOPOS_SKIP_UPDATE_CHECK", "true")
    monkeypatch.setattr(commands, "_get_installed_package_version", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not check")))

    assert commands._maybe_offer_self_update(skip_update_check=False) is False
