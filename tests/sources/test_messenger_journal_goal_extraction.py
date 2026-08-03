"""B11: messenger + journal sources schedule goal_extraction."""

from __future__ import annotations

from topos.query.manifest_validation import resolve_scope_manifest
from topos.sources.canonical_signal_defaults import resolved_signal_derivation_jobs
from topos.sources.registry import (
    DEMO_JOURNAL_FILE,
    DEMO_MESSENGER_FILE,
    IMESSAGE,
    SIGNAL,
    get_sources_by_scope,
)


def test_messenger_sources_schedule_goal_extraction() -> None:
    for source in (IMESSAGE, SIGNAL, DEMO_MESSENGER_FILE):
        assert "goal_extraction" in source.signal_derivation_jobs, source.source_id
        jobs = resolved_signal_derivation_jobs(source)
        assert "goal_extraction" in jobs, source.source_id


def test_journal_demo_schedules_goal_extraction_and_work_scope() -> None:
    assert "goal_extraction" in DEMO_JOURNAL_FILE.signal_derivation_jobs
    assert "work_context:read" in (DEMO_JOURNAL_FILE.allowed_scope_ids or [])
    jobs = resolved_signal_derivation_jobs(DEMO_JOURNAL_FILE)
    assert "goal_extraction" in jobs
    work = set(get_sources_by_scope("work_context:read"))
    assert DEMO_JOURNAL_FILE.source_id in work


def test_messages_scope_loads_user_goals() -> None:
    manifest = resolve_scope_manifest("messages:read")
    assert "user_goals" in (manifest.signal_objects or [])
    assert "imessage" in (manifest.default_source_ids or [])


def test_work_context_includes_journal_goal_sources() -> None:
    manifest = resolve_scope_manifest("work_context:read")
    defaults = set(manifest.default_source_ids or [])
    assert "demo_journal_file" in defaults
    assert "grow_journal" in defaults
    assert "grow_data_file" in defaults
