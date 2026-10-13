import sqlite3
from topos.query.retrieval import _load_user_goal_summaries


def database():
    c=sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE user_goals(goal_id TEXT,record_id TEXT,source_id TEXT,goal_text TEXT,created_at TEXT);
    CREATE TABLE timeline(record_id TEXT,event_at TEXT);
    CREATE TABLE ai_chat_messages(message_id TEXT,conversation_id TEXT,sender_type TEXT,event_at TEXT,content TEXT);''')
    c.execute("INSERT INTO user_goals VALUES ('g','m','chat','Review the garden proposal','2026-10-12T18:00:00Z')")
    return c


def test_canonical_source_clock_wins_over_later_timeline_and_ingest():
    c=database()
    c.execute("INSERT INTO timeline VALUES ('m','2026-10-12T18:00:00Z')")
    c.execute("INSERT INTO ai_chat_messages VALUES ('m','thread','human','2026-10-11T09:00:00Z','I want to review the garden proposal')")
    item=_load_user_goal_summaries('my goals',conn=c,goal_intent=True)[0]
    assert item['event_at']=='2026-10-11T09:00:00Z'
    assert item['event_time_basis']=='canonical_source'
    assert item['owner_authored'] is True


def test_ingestion_time_is_not_substituted_for_missing_event_time():
    item=_load_user_goal_summaries('my goals',conn=database(),goal_intent=True)[0]
    assert item['event_at'] is None
    assert item['created_at']=='2026-10-12T18:00:00Z'


def test_canonical_clock_survives_missing_timeline_and_ingestion_dates():
    c=database()
    c.execute("UPDATE user_goals SET created_at = ''")
    c.execute("INSERT INTO ai_chat_messages VALUES ('m','thread','human','2026-10-11T09:00:00Z','I want to review the garden proposal')")
    item=_load_user_goal_summaries('my goals',conn=c,goal_intent=True)[0]
    assert item['event_at']=='2026-10-11T09:00:00Z'
    assert item['event_time_basis']=='canonical_source'
    assert item['owner_authored'] is True
