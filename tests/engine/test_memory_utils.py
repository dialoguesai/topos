"""RSS reporting must degrade, not crash, where the resource module is absent.

Windows has no `resource`; the module used to import it unconditionally, which
made `topos.engine.memory_utils` (and everything importing it) unimportable on
win32 — a hard blocker for the Windows shell (PLAN_APP_SHELL_DISTRIBUTION.md §6.2).
"""

from topos.engine import memory_utils


def test_rss_reports_a_positive_number_where_resource_exists():
    rss = memory_utils.get_process_rss_mb()
    assert rss is not None and rss > 0


def test_rss_is_none_without_resource_module(monkeypatch):
    # Simulate win32: the guarded import leaves the module attribute as None.
    monkeypatch.setattr(memory_utils, "resource", None)
    assert memory_utils.get_process_rss_mb() is None
