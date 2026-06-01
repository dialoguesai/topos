import sqlite3

from topos.analytics.messenger_labels import enrich_conversation_thread_previews, sender_matches_focus_identifier
from topos.storage.canonical.conversations_tables import ConversationsTablesManager, ensure_all_tables


def test_sender_matches_focus_identifier_phone_variants():
    assert sender_matches_focus_identifier("+15550001111", "15550001111")
    assert sender_matches_focus_identifier("15550001111", "+15550001111")
    assert not sender_matches_focus_identifier("+15550001112", "15550001111")


def test_get_contact_conversation_thread_previews_includes_other_senders():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_all_tables(conn)
    dataset_id = "ds:threads"
    source_id = "imessage"
    conv = "conv-1"

    mgr = ConversationsTablesManager(conn)

    def ins(mid: str, sender: str, content: str, event_at: str) -> None:
        conn.execute(
            """
            INSERT INTO conversation_messages
            (message_id, conversation_id, dataset_id, sender_type, sender_id, event_at, source_id, content, is_from_self)
            VALUES (?, ?, ?, 'participant', ?, ?, ?, ?, 0)
            """,
            (mid, conv, dataset_id, sender, event_at, source_id, content),
        )

    ins("m1", "+15550001111", "hi", "2025-01-01T10:00:00Z")
    ins("m2", "+14440002222", "hello back", "2025-01-01T10:01:00Z")
    ins("m3", "+15550001111", "second", "2025-01-01T10:02:00Z")
    conn.commit()

    previews = mgr.get_contact_conversation_thread_previews(
        dataset_id=dataset_id,
        source_id=source_id,
        profile_identifier="+15550001111",
        max_conversations=4,
        messages_per_conversation=20,
    )
    assert len(previews) == 1
    assert previews[0]["conversation_id"] == conv
    msgs = previews[0]["messages"]
    assert len(msgs) == 3
    assert [m["message_id"] for m in msgs] == ["m3", "m2", "m1"]

    enrich_conversation_thread_previews(
        conn,
        dataset_id=dataset_id,
        profile_identifier="+15550001111",
        previews=previews,
    )
    focus_flags = [bool(m.get("is_focus_contact")) for m in msgs]
    assert focus_flags == [True, False, True]
    assert all(m.get("sender_display_name") for m in msgs)
