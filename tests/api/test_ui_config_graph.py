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
