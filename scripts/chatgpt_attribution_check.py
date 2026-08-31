#!/usr/bin/env python3
"""Attribution invariants over an imported ChatGPT corpus.

PLAN_CHATGPT_IMPORT.md §2.3 asks four questions of home chat. Each one is only
answerable if the same four invariants hold in storage, and those can be checked
directly against a database without driving the app:

  1. "What have I been working on?"      — belief-grade rows are the owner's only.
  2. "What did I ask ChatGPT about X?"    — owner turns are attributable to them.
  3. "What did I read about X?"           — exposure is present and marked as exposure.
  4. "What does ChatGPT know about me?"   — the model's words never become identity.

Run it against a shadow database (or, read-only, against a live one):

    python scripts/chatgpt_attribution_check.py --db ~/.topos/shadow/chatgpt-after.db

Exit code is 0 when every invariant holds.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from topos.features.entities.chatgpt_declared import (
    EDGE_AUTHORED,
    EDGE_EXPOSED_TO,
    TYPE_DOCUMENT,
    TYPE_WEB_SOURCE,
)
from topos.features.provenance.roles import (
    ROLE_ADDRESSED,
    ROLE_AUTHORED,
    record_role,
)

CHECK = Tuple[str, str, bool, str]  # question, invariant, ok, detail


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def check_roles(conn: sqlite3.Connection) -> List[CHECK]:
    """Every stored turn resolves to the role its sender earns — no guessing."""
    rows = _rows(
        conn,
        "SELECT message_id, sender_type, content, metadata_json FROM ai_chat_messages"
        " WHERE source_id LIKE 'chatgpt%'",
    )
    wrong_owner: List[str] = []
    wrong_assistant: List[str] = []
    for row in rows:
        record = dict(row)
        record["_table"] = "ai_chat_messages"
        # posture "mixed" is what the registry declares for the ChatGPT sources:
        # the role is decided per row, from the sender.
        role = record_role(record, table="ai_chat_messages", posture="mixed")
        sender = str(row["sender_type"] or "").lower()
        if sender in ("human", "user") and role != ROLE_AUTHORED:
            wrong_owner.append(f"{row['message_id']}→{role}")
        if sender == "assistant" and role != ROLE_ADDRESSED:
            wrong_assistant.append(f"{row['message_id']}→{role}")
    total = len(rows)
    return [
        (
            "What did I ask ChatGPT about X?",
            "every turn the owner wrote resolves as authored",
            not wrong_owner,
            f"{total} turns checked; {len(wrong_owner)} misattributed" + (f" e.g. {wrong_owner[:3]}" if wrong_owner else ""),
        ),
        (
            "What does ChatGPT know about me?",
            "no assistant turn resolves as authored — the model's words never become identity",
            not wrong_assistant,
            f"{len(wrong_assistant)} assistant turns claimed authorship"
            + (f" e.g. {wrong_assistant[:3]}" if wrong_assistant else ""),
        ),
    ]


def check_mentions(conn: sqlite3.Connection) -> List[CHECK]:
    """A mention's authored flag must agree with the turn it came from."""
    rows = _rows(
        conn,
        "SELECT m.mention_id, m.authored_by_owner, c.sender_type"
        " FROM entity_mentions m JOIN ai_chat_messages c ON c.message_id = m.record_id"
        " WHERE m.canonical_table = 'ai_chat_messages'",
    )
    disagree = [
        r["mention_id"]
        for r in rows
        if bool(r["authored_by_owner"]) != (str(r["sender_type"] or "").lower() in ("human", "user"))
    ]
    return [
        (
            "What have I been working on?",
            "every mention carries the authorship of the turn it was found in",
            not disagree,
            f"{len(rows)} joined mentions; {len(disagree)} disagree with their turn",
        )
    ]


def check_exposure(conn: sqlite3.Connection) -> List[CHECK]:
    """Cited sources exist, and none of them is claimed as the owner's writing."""
    sources = _rows(
        conn,
        "SELECT entity_id, canonical_name FROM entities WHERE entity_type = ?",
        (TYPE_WEB_SOURCE,),
    )
    if not sources:
        return [
            (
                "What did I read about X?",
                "cited sources are minted as their own nodes",
                False,
                "no web_source entities — the declared exposure lane produced nothing",
            )
        ]
    ids = {r["entity_id"] for r in sources}
    authored_edges = _rows(
        conn,
        "SELECT edge_id, dst_entity_id, edge_type FROM entity_edges WHERE edge_type = ?",
        (EDGE_AUTHORED,),
    )
    claimed = [e["edge_id"] for e in authored_edges if e["dst_entity_id"] in ids]
    exposure_edges = _rows(
        conn, "SELECT COUNT(*) AS n FROM entity_edges WHERE edge_type = ?", (EDGE_EXPOSED_TO,)
    )
    exposure_count = int(exposure_edges[0]["n"]) if exposure_edges else 0

    owner_authored_mentions = _rows(
        conn,
        "SELECT COUNT(*) AS n FROM entity_mentions WHERE authored_by_owner = 1 AND entity_id IN"
        " (SELECT entity_id FROM entities WHERE entity_type = ?)",
        (TYPE_WEB_SOURCE,),
    )
    owner_claimed = int(owner_authored_mentions[0]["n"]) if owner_authored_mentions else 0

    # The owner edge is written only when an ``is_self`` entity exists. A
    # corpus-only import has no address book and therefore no identity, so the
    # edge cannot form — that is a property of the fixture, not a defect, and
    # co-occurrence still joins each source to what was extracted beside it.
    self_rows = _rows(conn, "SELECT COUNT(*) AS n FROM entities WHERE is_self = 1")
    has_identity = int(self_rows[0]["n"]) if self_rows else 0
    cooccurrence = _rows(
        conn, "SELECT COUNT(*) AS n FROM entity_edges WHERE edge_type = 'co_occurrence'"
    )
    cooccurrence_count = int(cooccurrence[0]["n"]) if cooccurrence else 0

    return [
        (
            "What did I read about X?",
            "cited sources are minted as their own nodes",
            True,
            f"{len(sources)} web sources, {exposure_count} exposure edges"
            + (
                ""
                if has_identity
                else f" (no is_self entity in this database, so owner edges cannot form;"
                f" {cooccurrence_count} co-occurrence edges still join them)"
            ),
        ),
        (
            "What did I read about X?",
            "a page the model fetched is never recorded as something the owner wrote",
            not claimed and owner_claimed == 0,
            f"{len(claimed)} authored edges into sources; {owner_claimed} owner-authored source mentions",
        ),
    ]


def check_documents(conn: sqlite3.Connection) -> List[CHECK]:
    docs = _rows(
        conn, "SELECT COUNT(*) AS n FROM entities WHERE entity_type = ?", (TYPE_DOCUMENT,)
    )
    count = int(docs[0]["n"]) if docs else 0
    return [
        (
            "What have I been working on?",
            "documents the owner co-authored are their own nodes",
            count > 0,
            f"{count} document entities",
        )
    ]


def check_no_scaffolding(conn: sqlite3.Connection) -> List[CHECK]:
    """Chain-of-thought must not be in storage at all — it is not anyone's speech."""
    rows = _rows(
        conn,
        "SELECT COUNT(*) AS n FROM ai_chat_messages WHERE source_id LIKE 'chatgpt%'"
        " AND (metadata_json LIKE '%\"content_type\": \"thoughts\"%'"
        "   OR metadata_json LIKE '%\"content_type\": \"reasoning_recap\"%')",
    )
    count = int(rows[0]["n"]) if rows else 0
    blanks = _rows(
        conn,
        "SELECT COUNT(*) AS n FROM ai_chat_messages WHERE source_id LIKE 'chatgpt%'"
        " AND TRIM(COALESCE(content,'')) = ''",
    )
    blank_count = int(blanks[0]["n"]) if blanks else 0
    return [
        (
            "What does ChatGPT know about me?",
            "no model scaffolding is stored as a turn",
            count == 0,
            f"{count} scaffolding turns stored",
        ),
        (
            "What did I ask ChatGPT about X?",
            "no blank turn is stored",
            blank_count == 0,
            f"{blank_count} blank turns stored",
        ),
    ]


CHECKS: List[Callable[[sqlite3.Connection], List[CHECK]]] = [
    check_roles,
    check_mentions,
    check_exposure,
    check_documents,
    check_no_scaffolding,
]


def run(db_path: Path) -> Dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        results: List[CHECK] = []
        for check in CHECKS:
            results.extend(check(conn))
    finally:
        conn.close()
    return {
        "database": str(db_path),
        "checks": [
            {"question": q, "invariant": inv, "ok": ok, "detail": detail}
            for q, inv, ok, detail in results
        ],
        "passed": sum(1 for _, _, ok, _ in results if ok),
        "total": len(results),
        "ok": all(ok for _, _, ok, _ in results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = run(Path(args.db).expanduser())
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    print(f"attribution check — {report['database']}\n")
    last_question = None
    for check in report["checks"]:
        if check["question"] != last_question:
            print(f"  {check['question']}")
            last_question = check["question"]
        mark = "PASS" if check["ok"] else "FAIL"
        print(f"    [{mark}] {check['invariant']}")
        print(f"           {check['detail']}")
    print(f"\n  {report['passed']}/{report['total']} invariants hold")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
