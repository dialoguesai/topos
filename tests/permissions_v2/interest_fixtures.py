"""Synthetic browsing for the interest family tests. Every name, site and title here is invented."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from topos.permissions_v2 import capture_receipts as cr
from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
from topos.storage.db.migrations import apply_all_migrations

OWNER = "owner-uuid-1"
RESOURCE = "resource-1"
SOURCE = "browser_visits"
DATASET = f"{OWNER}:topos:default"
APP = "browser-history-plugin"
# 2026-09-20T12:00:00Z: September is the current, incomplete month.
NOW_US = int(datetime(2026, 9, 20, 12, tzinfo=timezone.utc).timestamp()) * 1_000_000
WRITER_COLUMNS = (("writer_class", "TEXT"), ("writer_app_id", "TEXT"), ("writer_dataset_id", "TEXT"))


def at(month: int, day: int, hour: int = 10, year: int = 2026) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:00:00.000Z"


_TEMPLATE: dict = {}


def open_db(path) -> sqlite3.Connection:
    """A migrated database at ``path``. The migrations run once per session into a template that is
    copied, so each test still starts from a fresh file."""
    import shutil
    import tempfile
    from pathlib import Path

    if "path" not in _TEMPLATE:
        template = Path(tempfile.mkdtemp(prefix="interest-template-")) / "template.db"
        seed = sqlite3.connect(str(template))
        # The chat tables are created lazily by their first writer; the migrations then add writer columns.
        from topos.storage.canonical.ai_chat import CanonicalTablesManager
        from topos.storage.canonical.conversations_tables import (ensure_conversation_messages_table,
                                                                  ensure_conversations_table)
        CanonicalTablesManager(seed)
        ensure_conversations_table(seed)
        ensure_conversation_messages_table(seed)
        apply_all_migrations(seed)
        seed.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
        seed.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('user_id', ?)", (OWNER,))
        seed.commit()
        seed.close()
        _TEMPLATE["path"] = template
    shutil.copyfile(_TEMPLATE["path"], str(path))
    conn = sqlite3.connect(str(path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(activity_events)")}
    # The P1 migration's three columns (activity_writer_columns_v1), added here until it lands.
    for column, kind in WRITER_COLUMNS:
        if column not in columns:
            conn.execute(f"ALTER TABLE activity_events ADD COLUMN {column} {kind}")
    conn.execute(TOMBSTONES_SQL)
    conn.execute("CREATE TABLE IF NOT EXISTS browser_visits (record_id TEXT PRIMARY KEY, url TEXT, title TEXT, "
                 "hostname TEXT, visited_at TEXT, incognito INTEGER)")
    conn.commit()
    return conn


def install(conn, *, source: str = SOURCE, dataset=DATASET, user: str = OWNER, active: int = 1) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS source_runtime_installs (
        install_id TEXT PRIMARY KEY, scope_key TEXT, source_id TEXT, version_id TEXT, status TEXT, is_active INTEGER,
        source_definition_json TEXT, source_version_row_json TEXT, failure_reason TEXT, created_at TEXT, updated_at TEXT)""")
    count = conn.execute("SELECT COUNT(*) FROM source_runtime_installs").fetchone()[0]
    scope = json.dumps({"user_id": user, "topos_id": RESOURCE, "device_id": "*", "dataset_id": dataset})
    conn.execute("INSERT INTO source_runtime_installs (install_id, scope_key, source_id, version_id, status, "
                 "is_active, source_definition_json) VALUES (?,?,?,?,?,?,?)",
                 (f"install-{count}", scope, source, "v1", "active", active, json.dumps({"source_id": source})))
    conn.commit()


def cluster(conn, cluster_id: str, label: str, *, other_members: int = 0) -> None:
    conn.execute("INSERT OR REPLACE INTO topic_clusters (cluster_id, label, dimension) VALUES (?,?,'interests')",
                 (cluster_id, label))
    for i in range(other_members):
        conn.execute("INSERT INTO topic_cluster_members (member_id, cluster_id, record_id, source_id) "
                     "VALUES (?,?,?,'chatgpt')", (f"{cluster_id}-msg-{i}", cluster_id, f"msg-{cluster_id}-{i}"))


def visit(conn, n, when: str, *, cluster_id: str = "tc_hobby", url: str | None = None, title: str | None = None,
          host: str = "example.test", writer: str | None = "owner_app", app: str | None = APP,
          dataset: str | None = DATASET, metadata=None, incognito: int | None = None, preview: str | None = None,
          source: str = SOURCE) -> str:
    """One visit, stamped by the owner's plugin by default, and its membership in a cluster."""
    event_id = f"browser:v{n}"
    url = url or f"https://{host}/page/{n}"
    conn.execute("INSERT OR REPLACE INTO activity_events (event_id, activity_type, url, title, occurred_at, source_id, "
                 "source_record_id, metadata_json, hostname, writer_class, writer_app_id, writer_dataset_id) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (event_id, "browser_visit", url, title or f"Synthetic page {n}", when, source, f"v{n}",
                  json.dumps(metadata or {}), host, writer, app if writer else None, dataset if writer else None))
    if incognito is not None:
        conn.execute("INSERT OR REPLACE INTO browser_visits (record_id, url, title, hostname, visited_at, incognito) "
                     "VALUES (?,?,?,?,?,?)", (f"v{n}", url, title, host, when, incognito))
    if cluster_id is not None:
        conn.execute("INSERT OR REPLACE INTO topic_cluster_members (member_id, cluster_id, record_id, source_id, "
                     "record_type, text_preview) VALUES (?,?,?,?,?,?)",
                     (f"m-{cluster_id}-{n}", cluster_id, event_id, source, "activity_event", preview))
    return event_id


def month_of_visits(conn, start: int, count: int, days, *, month: int = 8, **kwargs) -> list:
    """``count`` visits spread round-robin over ``days`` of one month."""
    return [visit(conn, start + i, at(month, days[i % len(days)]), **kwargs) for i in range(count)]


def attest_app(conn, *, app: str = APP) -> dict:
    """The owner attests the plugin (and any pre-stamp visits at their current revision)."""
    preview = cr.preview(conn, owner_id=OWNER, table="activity_events", source_id=SOURCE, app_id=app)
    receipt = cr.attest(conn, owner_id=OWNER, table="activity_events", source_id=SOURCE, app_id=app,
                        preview_digest=preview["preview_digest"], confirm=True, now=1_700_000_000)
    conn.commit()
    return receipt
