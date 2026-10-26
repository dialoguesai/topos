"""Owner viewer summaries and names, restricted to the already selected rows/scope."""
import json
import os
from functools import lru_cache
from pathlib import Path
from ...principal import current_principal


@lru_cache(maxsize=1)
def _read_codex_titles(path, modified_ns, size):
    # Codex's state database may retain the first prompt as `title`; the named
    # session index carries the actual UI title, including later renames.
    titles = {}
    try:
        with Path(path).open() as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("thread_name"), str):
                    title = entry["thread_name"].strip()
                    if title:
                        titles["codex:" + entry["id"]] = title
    except OSError:
        pass
    return titles


def _codex_titles():
    if getattr(current_principal(), "cls", None) != "owner_app":
        return {}
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "session_index.jsonl"
    try:
        stat = path.stat()
    except OSError:
        return {}
    return _read_codex_titles(str(path), stat.st_mtime_ns, stat.st_size)


TABLES = {"ai_chat_messages", "conversation_messages", "transcript_segments"}


def _columns(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _title_expression(conn, table, alias="m"):
    if table == "ai_chat_messages":
        cols = _columns(conn, "ai_chat_conversations")
        if not cols:
            return "NULL"
        title = "COALESCE(NULLIF(p.latest_title, ''), p.title)" if "latest_title" in cols else "p.title"
        return f'(SELECT {title} FROM ai_chat_conversations p WHERE p.conversation_id = {alias}.conversation_id)'
    if table == "transcript_segments" and _columns(conn, "transcripts"):
        return f'(SELECT p.title FROM transcripts p WHERE p.transcript_id = {alias}.transcript_id AND p.dataset_id = {alias}.dataset_id)'
    return "NULL"


def conversation_index_sql(conn, table, where, values, payload, limit, offset):
    """Search the full authorized set, then paginate distinct matching conversations."""
    if table not in TABLES or getattr(current_principal(), "cls", None) != "owner_app":
        raise ValueError("Conversation search is an owner surface")
    if payload.get("filter_column") or payload.get("anchor_id"):
        raise ValueError("Conversation search cannot be combined with a message filter")
    search = payload.get("search", "")
    if not isinstance(search, str) or len(search) > 256:
        raise ValueError("Search must be at most 256 characters")
    thread = "transcript_id" if table == "transcript_segments" else "conversation_id"
    key = "segment_id" if table == "transcript_segments" else "message_id"
    cols = _columns(conn, table)
    partition = ', '.join(f'm."{c}"' for c in [thread, "dataset_id", "source_id"] if c in cols)
    match = ""
    if search.strip():
        pattern = '%' + search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        expressions = ["m.content", _title_expression(conn, table)]
        if "sender_id" in cols:
            expressions.append("m.sender_id")
        conditions = [f"{expr} LIKE ? ESCAPE '\\'" for expr in expressions]
        values += (pattern,) * len(expressions)
        if table == "conversation_messages" and _columns(conn, "conversation_participants") and _columns(conn, "contacts"):
            conditions.append("""EXISTS (SELECT 1 FROM conversation_participants cp JOIN contacts c
                ON c.contact_id = cp.contact_id AND c.dataset_id = cp.dataset_id
                WHERE cp.conversation_id = m.conversation_id AND cp.dataset_id = m.dataset_id
                  AND cp.source_id = m.source_id AND c.display_name LIKE ? ESCAPE '\\')""")
            values += (pattern,)
        if table == "ai_chat_messages":
            named_ids = [key for key, title in _codex_titles().items() if search.strip().casefold() in title.casefold()]
            if named_ids:
                conditions.append("(m.source_id = 'codex_sessions' AND m.conversation_id IN (" + ','.join('?' for _ in named_ids) + "))")
                values += tuple(named_ids)
        match = ' WHERE (' + ' OR '.join(conditions) + ')'
    sql = f'''WITH scoped AS (SELECT * FROM "{table}"{where}),
        ranked AS (SELECT m.*, ROW_NUMBER() OVER (PARTITION BY {partition}
            ORDER BY m.event_at DESC, m."{key}" DESC) AS viewer_rank FROM scoped m{match})
        SELECT * FROM ranked WHERE viewer_rank = 1 ORDER BY event_at DESC, "{key}" DESC LIMIT ? OFFSET ?'''
    return sql, values + (limit + 1, offset)


def enrich_conversation_rows(conn, table, rows):
    if table not in TABLES or getattr(current_principal(), "cls", None) != "owner_app":
        return
    title_expr = _title_expression(conn, table)
    thread = "transcript_id" if table == "transcript_segments" else "conversation_id"
    titles, participants = {}, {}
    codex_titles = _codex_titles() if table == "ai_chat_messages" else {}
    have_roster = bool(_columns(conn, "conversation_participants") and _columns(conn, "contacts"))
    have_identifiers = bool(_columns(conn, "contact_identifiers") and _columns(conn, "contacts"))
    for row in rows:
        row.pop("viewer_rank", None)
        identity = (row.get(thread), row.get("dataset_id"), row.get("source_id"))
        if table != "conversation_messages":
            if identity not in titles:
                key = "segment_id" if table == "transcript_segments" else "message_id"
                result = conn.execute(f'SELECT {title_expr} FROM "{table}" m WHERE m."{key}" = ?', (row[key],)).fetchone()
                titles[identity] = result[0] if result else None
            title = codex_titles.get(identity[0]) if row.get("source_id") == "codex_sessions" else None
            if title or titles[identity]:
                row["conversation_title"] = title or titles[identity]
            continue
        if identity not in participants:
            names = []
            if have_roster:
                names = [r[0] for r in conn.execute('''SELECT DISTINCT c.display_name
                    FROM conversation_participants cp JOIN contacts c
                      ON c.contact_id = cp.contact_id AND c.dataset_id = cp.dataset_id
                    WHERE cp.conversation_id = ? AND cp.dataset_id = ? AND cp.source_id = ?
                      AND COALESCE(c.is_self, 0) = 0 AND c.display_name IS NOT NULL AND c.display_name != ''
                    ORDER BY c.display_name''', identity)]
            # Older imports may have messages but no participant roster yet.
            if not names and have_identifiers:
                names = [r[0] for r in conn.execute('''SELECT DISTINCT c.display_name FROM conversation_messages m
                    JOIN contact_identifiers ci ON ci.identifier = m.sender_id AND ci.dataset_id = m.dataset_id
                      AND ci.source_id IN (m.source_id, '*')
                    JOIN contacts c ON c.contact_id = ci.contact_id AND c.dataset_id = ci.dataset_id
                    WHERE m.conversation_id = ? AND m.dataset_id = ? AND m.source_id = ?
                      AND COALESCE(m.is_from_self, 0) = 0 AND COALESCE(c.is_self, 0) = 0
                      AND c.display_name IS NOT NULL AND c.display_name != '' ORDER BY c.display_name''', identity)]
            participants[identity] = names
        if participants[identity]:
            row["participant_names"] = participants[identity]


def enrich_timeline_conversations(conn, entries):
    """Resolve just this page's owner-visible records using the viewer's names.

    Read records once per kind, never fetch message bodies or issue a browser
    request per event. IDs and event order remain unchanged.
    """
    if getattr(current_principal(), "cls", None) != "owner_app":
        return
    for table in TABLES:
        selected = [entry for entry in entries if entry.get("canonical_table") == table]
        if not selected:
            continue
        columns = _columns(conn, table)
        key = "segment_id" if table == "transcript_segments" else "message_id"
        thread = "transcript_id" if table == "transcript_segments" else "conversation_id"
        wanted = [key, thread, "dataset_id", "source_id"]
        if not set(wanted).issubset(columns):
            continue  # Older/minimal schemas can still render the timeline.
        ids = list(dict.fromkeys(entry["record_id"] for entry in selected))
        placeholders = ','.join('?' for _ in ids)
        cursor = conn.execute(f'SELECT {", ".join(wanted)} FROM "{table}" WHERE "{key}" IN ({placeholders})', ids)
        records = [dict(zip(wanted, row)) for row in cursor]
        enrich_conversation_rows(conn, table, records)
        by_id = {row[key]: row for row in records}
        for entry in selected:
            record = by_id.get(entry["record_id"], {})
            title = record.get("conversation_title")
            if table == "conversation_messages":
                title = ", ".join(record.get("participant_names") or [])
            if isinstance(title, str) and title.strip():
                entry["display_title"] = title.strip()
