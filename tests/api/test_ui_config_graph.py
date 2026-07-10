"""ui-config carries per-user graph view prefs (time window) in the node DB.

The graph's 'Active in' window is personal — right size depends on the
volume/velocity of the owner's data (2 days for some, 2 months for others) —
so it persists engine-side and loads as the default."""

from topos.api.ui_config import _normalize_ui_config


def test_graph_time_window_roundtrips():
    out = _normalize_ui_config({"graph": {"timeWindowDays": 60}})
    assert out["graph"]["timeWindowDays"] == 60


def test_graph_time_window_null_means_all_time():
    out = _normalize_ui_config({"graph": {"timeWindowDays": None}})
    assert out["graph"]["timeWindowDays"] is None


def test_graph_track_lookback_roundtrips():
    out = _normalize_ui_config({"graph": {"trackLookbackDays": 120}})
    assert out["graph"]["trackLookbackDays"] == 120


def test_graph_track_lookback_null_means_full_extent():
    out = _normalize_ui_config({"graph": {"trackLookbackDays": None}})
    assert out["graph"]["trackLookbackDays"] is None


def test_garbage_track_lookback_dropped():
    for bad in ("soon", -3, 0, 99999, {"x": 1}):
        out = _normalize_ui_config({"graph": {"trackLookbackDays": bad}})
        assert "trackLookbackDays" not in out["graph"]


def test_graph_section_absent_stays_absent():
    out = _normalize_ui_config({"topbar": {"pinnedAnalytics": []}})
    assert out.get("graph") == {}


def test_garbage_window_dropped():
    for bad in ("soon", -3, 0, 99999, {"x": 1}):
        out = _normalize_ui_config({"graph": {"timeWindowDays": bad}})
        assert "timeWindowDays" not in out["graph"]


def test_topbar_behavior_unchanged():
    out = _normalize_ui_config({"topbar": {"pinnedAnalytics": ["umaAllTime", "nope"]}})
    assert out["topbar"]["pinnedAnalytics"] == ["umaAllTime"]


def test_graph_node_color_mode_roundtrips():
    for mode in ("type", "community"):
        out = _normalize_ui_config({"graph": {"nodeColorMode": mode}})
        assert out["graph"]["nodeColorMode"] == mode


def test_graph_node_color_mode_garbage_dropped():
    out = _normalize_ui_config({"graph": {"nodeColorMode": "rainbow"}})
    assert "nodeColorMode" not in out["graph"]


def test_ws_handler_uses_shared_normalizer():
    """The WS path (CP proxy) must share the HTTP normalizer — a drifted copy
    silently stripped graph prefs saved through the deployed app."""
    from topos.api.ui_config import _normalize_ui_config as http_norm
    from topos.core.handlers.config import _normalize_ui_config as ws_norm

    assert ws_norm is http_norm

