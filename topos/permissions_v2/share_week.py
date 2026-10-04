"""``permissions_v2_share_week`` (A2A-3 §7.3; N4): what a share's recipients used in a window, from the ledger's receipts.

The node writes one private receipt per read (``PolicyLedger._checkpoint``) and, from N7, one per answer (A2A-4
§4.4). Nothing in product code read them until now (E1 surprise 2). This is that reader, for the owner's "This week"
line: counts only, never a request id, a record, a question or an answer.

- ``items_used``: the ``record_count`` of every permit receipt (``topos-local-receipt/v3``, one per search, batch item
  or locator read under a set-level capability) plus the ``records_used`` of every answer receipt
  (``topos-local-receipt/answer-v1``). An item released twice counts twice.
- ``answered`` and ``no_answer``: answer receipts by outcome. Zero until N7 writes them.

Which grant a receipt belongs to. A v3 receipt names no grant; it names the hash of the exact policy version it was
decided under, and that version's binding names the grant (``p2a_policies``, every version a grant ever had). The
contract's join through ``p2a_requests`` cannot be used: retention empties a request's envelope, the only place that
names its grant, five minutes after it expires (``ledger_retention.compact_expired``), so the join would find almost
nothing older than minutes. An answer receipt names its grant itself.

Which time. A v3 receipt's ``checked_at`` (the read's own checkpoint). An answer receipt has no ``checked_at``
(A2A-4 §4.4); its ``finished_at`` (when its job ended and the receipt was written) stands in, the window being
``[since, until)`` either way.

One read-only transaction on the ledger file; the write gate is not taken and nothing is written.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

VERSION = "topos-share-week/v1"
PERMIT_RECEIPT = "topos-local-receipt/v3"
ANSWER_RECEIPT = "topos-local-receipt/answer-v1"
MAX_GRANTS = 20


def week(ledger_path: Path, grant_ids: list, *, since: int, until: int) -> dict:
    """Counts for ``grant_ids`` (already checked: 1 to 20 distinct ids) with receipt times in ``[since, until)``."""
    wanted = sorted(set(grant_ids))
    marks = ",".join("?" for _ in wanted)
    conn = sqlite3.connect(Path(ledger_path).as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("BEGIN")
        hashes = sorted({row[0] for row in conn.execute(
            f"SELECT policy_hash FROM p2a_policies WHERE json_extract(policy_json, '$.binding.grant_id') IN ({marks})",
            wanted)})
        items = 0
        if hashes:
            hash_marks = ",".join("?" for _ in hashes)
            items = conn.execute(
                "SELECT COALESCE(SUM(json_extract(receipt_json, '$.record_count')), 0) FROM p2a_receipts "
                "WHERE json_extract(receipt_json, '$.version') = ? AND json_extract(receipt_json, '$.verdict') = 'permit' "
                "AND json_extract(receipt_json, '$.checked_at') >= ? AND json_extract(receipt_json, '$.checked_at') < ? "
                f"AND json_extract(receipt_json, '$.policy_hash') IN ({hash_marks})",
                (PERMIT_RECEIPT, since, until, *hashes)).fetchone()[0]
        answers = conn.execute(
            "SELECT COALESCE(SUM(json_extract(receipt_json, '$.records_used')), 0), "
            "COALESCE(SUM(json_extract(receipt_json, '$.outcome') = 'answered'), 0), "
            "COALESCE(SUM(json_extract(receipt_json, '$.outcome') = 'no_answer'), 0) FROM p2a_receipts "
            "WHERE json_extract(receipt_json, '$.version') = ? "
            "AND json_extract(receipt_json, '$.finished_at') >= ? AND json_extract(receipt_json, '$.finished_at') < ? "
            f"AND json_extract(receipt_json, '$.grant_id') IN ({marks})",
            (ANSWER_RECEIPT, since, until, *wanted)).fetchone()
    finally:
        conn.close()
    return {"version": VERSION, "items_used": int(items) + int(answers[0]), "answered": int(answers[1]),
            "no_answer": int(answers[2])}
