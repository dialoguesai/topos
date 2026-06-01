from __future__ import annotations

import builtins

from topos import app as topos_app


def test_log_runtime_banner_includes_version(monkeypatch):
    captured: list[str] = []

    def _fake_print(*args, **_kwargs):
        captured.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr(builtins, "print", _fake_print)

    topos_app._log_runtime_banner()

    assert captured
    assert any("TTTTTTTT    OOOOOOO    PPPPPPPP" in line for line in captured)
    assert any("Topos Node (topos-node)" in line for line in captured)
    assert any("Version : v" in line for line in captured)
    assert any("Mode    : uvicorn" in line for line in captured)
