from datetime import datetime
import sqlite3
from topos.principal import Principal, set_principal, reset_principal
from topos.query.retrieval import _rrf_fuse_summary_lists, _annotate_owner_ai_threads

NOW = datetime.fromisoformat("2026-10-12T15:45:00-05:00")
WINDOW = ("2026-10-05T15:45:00-05:00", NOW.isoformat())

def test_dense_latest_day_cannot_leave_only_one_earlier_work_thread():
    items = [{"record_id":f"new-{i}", "summary_text":"Recent work", "event_at":f"2026-10-12T12:{i:02d}:00-05:00", "conversation_id":"new-thread"} for i in range(20)]
    items += [{"record_id":f"earlier-{i}", "summary_text":"Earlier work", "event_at":f"2026-10-09T12:{i:02d}:00-05:00", "conversation_id":f"earlier-thread-{i}"} for i in range(4)]
    fused = _rrf_fuse_summary_lists([("recent",1,items)],cap=8,now=NOW,activity_time_range=WINDOW)
    assert len(fused) == 8
    assert {f"earlier-{i}" for i in range(4)} <= {i["record_id"] for i in fused}
    assert [i["event_at"] for i in fused] == sorted((i["event_at"] for i in fused),reverse=True)

def test_duplicate_goals_do_not_replace_source_threads_in_activity():
    goals = [{"record_id":"same-message", "summary_text":f"Goal {i}", "retrieval_source":"user_goal", "conversation_id":"same-thread", "event_at":NOW.isoformat()} for i in range(15)]
    source = {"record_id":"same-message", "summary_text":"Discussed design alternatives", "retrieval_source":"canonical:ai_chat_messages", "conversation_id":"same-thread", "event_at":NOW.isoformat()}
    earlier = {"record_id":"earlier", "summary_text":"Drafted another study", "conversation_id":"another-thread", "retrieval_source":"recent", "event_at":"2026-10-09T12:00:00-05:00"}
    fused = _rrf_fuse_summary_lists([("goals",3,goals),("canonical",1,[source]),("recent",1,[earlier])],cap=8,now=NOW,activity_time_range=WINDOW,goal_cap=2)
    assert source["summary_text"] in [i["summary_text"] for i in fused]
    assert earlier["record_id"] in [i["record_id"] for i in fused]
    assert sum(i.get("retrieval_source")=="user_goal" for i in fused) <= 2

def test_conversation_context_only_annotates_existing_owner_evidence():
    conn=sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT, conversation_id TEXT, event_at TEXT, content TEXT)")
    conn.execute("CREATE TABLE ai_chat_conversations(conversation_id TEXT, title TEXT)")
    conn.execute("INSERT INTO ai_chat_messages VALUES ('message','thread','2026-10-09T12:00:00Z','The source body')")
    conn.execute("INSERT INTO ai_chat_conversations VALUES ('thread','Icon design study')")
    for cls in ["third_party","cp_relay"]:
        token=set_principal(Principal(cls,"internal"))
        try:
            assert _annotate_owner_ai_threads([{"record_id":"message"}],conn)==[{"record_id":"message"}]
        finally:reset_principal(token)
    for cls in ["owner_app","owner_automation"]:
        token=set_principal(Principal(cls,"internal"))
        try:
            items=_annotate_owner_ai_threads([{"record_id":"message"},{"record_id":"absent"}],conn)
            assert items==[{"record_id":"message","conversation_id":"thread","conversation_title":"Icon design study","conversation_title_source":"conversation_record"},{"record_id":"absent"}]
        finally:reset_principal(token)
    conn.close()

def test_missing_title_can_use_an_explicit_document_heading_with_its_own_clock():
    conn=sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT, conversation_id TEXT, event_at TEXT, content TEXT)")
    conn.execute("CREATE TABLE ai_chat_conversations(conversation_id TEXT, title TEXT)")
    conn.executemany("INSERT INTO ai_chat_messages VALUES (?,?,?,?)", [
        ("heading","thread","2026-10-08T12:00:00Z","# Workforce Evidence Study\nDraft abstract"),
        ("source","thread","2026-10-09T12:00:00Z","Discussed revisions"),
        ("section","thread","2026-10-09T13:00:00Z","# 2. Research methods\nA section"),
        ("subsection","thread","2026-10-09T14:00:00Z","## Propositions\nA later subsection"),
        ("future","thread","2026-10-13T14:00:00Z","# Unrelated Future Revision\nA future title"),
    ])
    conn.execute("INSERT INTO ai_chat_conversations VALUES ('thread',NULL)")
    token=set_principal(Principal("owner_automation","internal"))
    try:
        item=_annotate_owner_ai_threads([{"record_id":"source","event_at":"2026-10-09T12:00:00Z"}],conn,now=NOW)[0]
        assert item["conversation_title"]=="Workforce Evidence Study"
        assert item["conversation_title_source"]=="message_heading"
        assert item["conversation_title_event_at"]=="2026-10-08T12:00:00Z"
        assert item["event_at"]=="2026-10-09T12:00:00Z"
    finally:reset_principal(token)
    conn.close()
