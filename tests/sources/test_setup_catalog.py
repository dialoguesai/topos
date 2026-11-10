"""The setup catalog: which word means which source, pinned to the registry.

protects: ``topos-node sources enable <word>`` and a setup prompt's checkbox lines resolve through
``sources/setup_catalog.py``. If an id here is not a registry source, or the name the app shows for a
source resolves to a different id (or to nothing), a person who ticks "iMessage" gets another source
or an error. Pure data; nothing is read from disk.
"""
from __future__ import annotations

import pytest

from topos.sources import setup_catalog
from topos.sources.registry import REGISTRY
from topos.sources.setup_catalog import KIND_CODING_HISTORY, KIND_EXTENSION, KIND_MESSAGES

pytestmark = pytest.mark.public


def test_every_catalog_id_is_a_registry_source_and_its_display_name_resolves_to_it() -> None:
    for entry in setup_catalog.CATALOG:
        definition = REGISTRY.get(entry.source_id)
        assert definition is not None, entry.source_id
        found = setup_catalog.resolve(definition.display_name)
        assert found is not None and found.source_id == entry.source_id, definition.display_name


def test_the_kinds_match_how_the_registry_delivers_each_source() -> None:
    from topos.ingestion.ai_tool_sync import SOURCE_ID_TO_TOOL
    from topos.ingestion.local_sync_jobs import LOCAL_SYNC_RUNNERS
    from topos.sources.definitions import accepts_app_ingest

    by_kind = {kind: {e.source_id for e in setup_catalog.CATALOG if e.kind == kind}
               for kind in (KIND_CODING_HISTORY, KIND_MESSAGES, KIND_EXTENSION)}
    assert by_kind[KIND_CODING_HISTORY] == set(SOURCE_ID_TO_TOOL)
    assert by_kind[KIND_MESSAGES] == set(setup_catalog.MESSENGER_SOURCE_IDS)
    # A messenger source the node cannot sync, or an extension source an app cannot push, would be a
    # row in `sources list` that can never be turned on.
    assert by_kind[KIND_MESSAGES] <= set(LOCAL_SYNC_RUNNERS)
    for source_id in by_kind[KIND_EXTENSION]:
        assert accepts_app_ingest(REGISTRY[source_id]), source_id


def test_an_extension_entry_names_an_enrolable_app_and_only_push_sources() -> None:
    from topos import local_pushers

    for entry in setup_catalog.CATALOG:
        if entry.kind != KIND_EXTENSION:
            assert entry.app is None and entry.app_sources == ()
            continue
        assert entry.app in local_pushers.PUSHER_DISPLAY_NAMES and entry.app_name
        assert entry.source_id in entry.app_sources
        assert local_pushers.parse_sources(entry.app_sources) == entry.app_sources
        assert str(entry.install_url).startswith("https://github.com/dialoguesai/")


@pytest.mark.parametrize("word, source_id", [
    ("imessage", "imessage"), ("iMessage Sync", "imessage"), ("  IMESSAGE  ", "imessage"), ("Messages", "imessage"),
    ("signal", "signal"), ("Signal Sync", "signal"), ("Signal-Desktop", "signal"),
    ("Browser History Plugin", "browser_visits"), ("browser history", "browser_visits"),
    ("browser-history-plugin", "browser_visits"), ("browser_events", "browser_events"),
    ("ChatGPT Shadow", "chatgpt_ui_conversation"), ("chatgpt-shadow-extension", "chatgpt_ui_conversation"),
    ("ChatGPT Shadow extension", "chatgpt_ui_conversation"), ("chatgpt_ui_conversation", "chatgpt_ui_conversation"),
    ("Claude Code", "claude_code_sessions"), ("codex", "codex_sessions"), ("Cursor history", "cursor_sessions"),
])
def test_a_checkbox_label_a_name_and_an_id_land_on_the_same_source(word: str, source_id: str) -> None:
    found = setup_catalog.resolve(word)
    assert found is not None and found.source_id == source_id


@pytest.mark.parametrize("word", ["", "   ", "whatsapp", "gmail", "sync", "plugin", "messages app for work"])
def test_a_word_that_names_no_source_is_never_guessed_at(word: str) -> None:
    assert setup_catalog.resolve(word) is None


def test_no_two_sources_share_a_word() -> None:
    seen: dict[str, str] = {}
    for entry in setup_catalog.CATALOG:
        for word in (entry.source_id, entry.name, *entry.aliases):
            key = setup_catalog._key(word)
            assert seen.setdefault(key, entry.source_id) == entry.source_id, word
