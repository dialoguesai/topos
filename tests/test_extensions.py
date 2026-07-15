from __future__ import annotations

import pytest

from topos.core.handlers.registry import handles
from topos.extensions import load_extensions


@pytest.mark.public
def test_load_extensions_does_not_raise_when_no_plugins_installed() -> None:
    load_extensions()


@pytest.mark.public
def test_load_extensions_invokes_entry_point_callables(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _init() -> None:
        seen.append("ran")

    class _FakeEntryPoint:
        name = "test"

        def load(self) -> object:
            return _init

    monkeypatch.setattr(
        "topos.extensions._iter_extension_entry_points",
        lambda: [_FakeEntryPoint()],
    )
    load_extensions()
    assert seen == ["ran"]
