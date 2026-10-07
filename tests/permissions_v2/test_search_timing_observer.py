import logging
import sqlite3
from types import SimpleNamespace

from topos.permissions_v2.runtime import Runtime


def test_opt_in_timing_logs_only_known_stages(monkeypatch, caplog, tmp_path):
    runtime = Runtime.__new__(Runtime)
    # A stand-in for the protocol with the one thing a share read asks of it before anything else: the canonical
    # database's path. Since `4dbacaf5` every share read first asks whether the exclude carry is owed on that
    # database (`Runtime.hold_for_the_exclude_carry`). A real protocol always has the path (`NodePolicyProtocol`
    # resolves it and refuses one that is no file); the bare `object()` this test used had none, so the test went
    # red with that commit and stayed red, unseen, until the whole public lane was run. An empty database owes
    # nothing.
    canonical = tmp_path / "canonical.db"
    sqlite3.connect(canonical).close()
    runtime.protocol = SimpleNamespace(canonical_database=canonical)
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
    # The setup gate's wait, the stage, who holds the gate before admission, then the one reported stage.
    assert [message.split()[2] for message in messages] == ["stage=gate_wait", "stage=runtime_setup",
                                                            "stage=gate_probe", "stage=recheck"]
    assert "point=runtime_setup" in messages[0] and "point=admit" in messages[2]
    assert "stage=recheck elapsed_ms=125.000" in messages[3]
    joined = " ".join(messages)
    assert "canary" not in joined and "recheck_facts" not in joined and "elapsed_ms=999000.000" not in joined
