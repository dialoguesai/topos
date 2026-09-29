import logging
from types import SimpleNamespace

from topos.permissions_v2.runtime import Runtime


def test_opt_in_timing_logs_only_known_stages(monkeypatch, caplog):
    runtime = Runtime.__new__(Runtime)
    runtime.protocol = object()
    runtime.message_search_index = lambda: SimpleNamespace(resolver=object(), reviews=object())
    monkeypatch.setattr("topos.permissions_v2.search_release.MessageSearchRelease", lambda **kw: kw)
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_SEARCH_TIMINGS", raising=False)
    assert runtime.message_search()["observe"] is None
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SEARCH_TIMINGS", "true")
    with caplog.at_level(logging.INFO, logger="topos.permissions_v2.search_timing"):
        observer = runtime.message_search()["observe"]
        observer("recheck", 0.125)
        observer("recheck_facts", 999)
        observer("invented-private-query-canary", 123)
    messages = [r.message for r in caplog.records]
    assert len(messages) == 2
    assert "stage=runtime_setup" in messages[0]
    assert "stage=recheck elapsed_ms=125.000" in messages[1]
    assert "canary" not in " ".join(messages) and "999" not in " ".join(messages)
