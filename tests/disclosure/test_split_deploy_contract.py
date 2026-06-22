"""Split deploy contract: database path must not require torch for privacy layer client."""

import importlib


def test_privacy_layer_client_import_without_torch(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "torch", None)  # type: ignore[arg-type]
    mod = importlib.import_module("topos.disclosure.privacy_layer")
    assert mod.PrivacyLayerClient is not None
