"""Re-apply the NSFW cutoff to rows the classifier already flagged, from the stored score. No model runs.

Ingest tags a row NSFW only when the classifier's NSFW label scores strictly above ``nsfw_classifier_threshold``
(``nsfw_classifier.is_nsfw_result``). Rows tagged before that rule counted an NSFW label at any confidence, and
they keep that tag until this re-check, which an owner runs on purpose (``topos.api.nsfw_maintenance``). It
decides each row from what is stored:

* Only rows tagged ``content_nsfw = 1`` are selected. A row tagged 0 is never touched: the old rule flagged every
  NSFW label, so a 0 from the classifier means its top label was the safe one, and a cutoff cannot raise that.
* For a row the configured classifier flagged, ``content_nsfw_score`` is its confidence in its top label, and
  that label was NSFW: the shipped classifier has two labels, and its safe one never flags. So keeping the tag
  exactly when ``score > threshold`` is what ingest decides today. The others are written 0.
* A row whose ``content_nsfw_model`` is not the configured classifier (another model, none recorded) keeps its
  tag: its score means something this cutoff was not chosen for.
* The token heuristic (no ML stack, or a pipeline error) stored the classifier's id with its fixed score
  ``HEURISTIC_NSFW_SCORE``. A float32 classifier output cannot equal that double, so a row carrying it exactly
  was decided by the heuristic, which the cutoff never gates: it keeps its tag. So does a row with no usable score.

A cleared row is written through the canonical writer under the node write gate, with its stored score kept and
``content_nsfw_model`` set to :func:`recheck_model_id`, so the row says which rule cleared it and at which
cutoff. Each row is re-read under the gate and written only if it is still exactly as decided; one that changed
in between (a re-ingest re-classified it) is left alone and counted. The re-check only lowers tags: a cutoff
lowered later re-flags nothing until those rows are classified again.

Counts only, here and in the log: no row id, score or text leaves this module.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..sanitization.nsfw_classifier import DEFAULT_NSFW_CLASSIFIER_MODEL, HEURISTIC_NSFW_SCORE, nsfw_threshold
from ..storage.db.write_gate import batched_writes
from .canonical_writer import upsert_nsfw_fields
from .field_registry import CANONICAL_ID_COLUMN

logger = logging.getLogger("topos.disclosure.nsfw_recheck")

VERSION = "nsfw-cutoff-recheck/v1"
#: The canonical tables that carry NSFW tags (migration ``canonical_nsfw_v1``).
TABLES = ("journal_entries", "conversation_messages", "ai_chat_messages")
#: Where each flagged row lands, in the order the checks run.
OUTCOMES = ("kept_other_model", "kept_no_score", "kept_heuristic", "kept_above_threshold", "below_threshold")
#: Per table: ``flagged`` = the sum of OUTCOMES; ``below_threshold`` = ``cleared`` + ``not_written`` in a write run.
COUNTS = ("flagged", *OUTCOMES, "cleared", "not_written")
_TAG_COLUMNS = ("content_nsfw", "content_nsfw_score", "content_nsfw_model")
#: Rows written per hold of the write gate. A message table can carry thousands of flags; short holds keep
#: ingest and the owner's other writers moving between them. Each chunk is its own commit.
WRITE_CHUNK = 500


def configured_model() -> str:
    from ..config.settings import settings

    return str(getattr(settings, "nsfw_classifier_model", None) or DEFAULT_NSFW_CLASSIFIER_MODEL).strip()


def recheck_model_id(model: str, threshold: float) -> str:
    """``content_nsfw_model`` of a row this re-check cleared: the classifier, the rule, and its strict cutoff."""
    return f"{model}+cutoff-recheck>{float(threshold)!r}"


def outcome(score: Any, model_id: Any, *, model: str, threshold: float) -> str:
    """One flagged row's outcome, from its stored score and model id alone."""
    if model_id != model:
        return "kept_other_model"
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        return "kept_no_score"
    if float(score) == HEURISTIC_NSFW_SCORE:
        return "kept_heuristic"
    return "kept_above_threshold" if score > threshold else "below_threshold"


def _tagged(conn: sqlite3.Connection, table: str) -> bool:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return False
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return {CANONICAL_ID_COLUMN[table], *_TAG_COLUMNS} <= columns


def _clear(conn: sqlite3.Connection, table: str, rows: List[Tuple[Any, Any, Any]], *, recorded: str) -> Tuple[int, int]:
    """Write 0 to each row still exactly as read, under the write gate, ``WRITE_CHUNK`` rows per hold and commit."""
    id_col = CANONICAL_ID_COLUMN[table]
    cleared = not_written = 0
    for start in range(0, len(rows), WRITE_CHUNK):
        with batched_writes(conn):
            for record_id, score, model_id in rows[start:start + WRITE_CHUNK]:
                current = conn.execute(
                    f"SELECT content_nsfw, content_nsfw_score, content_nsfw_model FROM {table} WHERE {id_col}=?",
                    (record_id,),
                ).fetchmany(2)
                if len(current) != 1 or tuple(current[0]) != (1, score, model_id):
                    not_written += 1
                    continue
                if upsert_nsfw_fields(conn, table, record_id, is_nsfw=False, score=float(score), model_id=recorded):
                    cleared += 1
                else:
                    not_written += 1
    return cleared, not_written


def recheck_nsfw_tags(
    conn: sqlite3.Connection,
    *,
    threshold: Optional[float] = None,
    model: Optional[str] = None,
    dry_run: bool = True,
    tables: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Re-apply the cutoff to every row tagged NSFW. Counts per table and in total; nothing else.

    ``threshold`` defaults to the configured ``nsfw_classifier_threshold`` and ``model`` to the configured
    classifier. ``dry_run`` (the default) reads and counts and writes nothing. ``tables`` limits the pass to some
    of :data:`TABLES` (all of them by default), so an owner can take the journal first.
    """
    if tables is not None and (not tables or not set(tables) <= set(TABLES)):
        raise ValueError("tables must name some of the NSFW-tagged tables")
    cut = nsfw_threshold(threshold)
    model = (model or configured_model()).strip()
    per_table: Dict[str, Dict[str, int]] = {}
    for table in TABLES:
        if tables is not None and table not in tables:
            continue
        if not _tagged(conn, table):
            continue
        counts = dict.fromkeys(COUNTS, 0)
        to_clear: List[Tuple[Any, Any, Any]] = []
        rows = conn.execute(
            f"SELECT {CANONICAL_ID_COLUMN[table]}, content_nsfw_score, content_nsfw_model FROM {table} "
            "WHERE content_nsfw = 1"
        ).fetchall()
        for record_id, score, model_id in rows:
            counts["flagged"] += 1
            decided = outcome(score, model_id, model=model, threshold=cut)
            counts[decided] += 1
            if decided == "below_threshold":
                to_clear.append((record_id, score, model_id))
        if to_clear and not dry_run:
            counts["cleared"], counts["not_written"] = _clear(
                conn, table, to_clear, recorded=recheck_model_id(model, cut)
            )
        per_table[table] = counts
    totals = {key: sum(counts[key] for counts in per_table.values()) for key in COUNTS}
    logger.info(
        "NSFW cutoff re-check: dry_run=%s threshold=%r flagged=%d kept_above=%d below=%d cleared=%d not_written=%d",
        dry_run,
        cut,
        totals["flagged"],
        totals["kept_above_threshold"],
        totals["below_threshold"],
        totals["cleared"],
        totals["not_written"],
    )
    return {
        "version": VERSION,
        "dry_run": dry_run,
        "threshold": cut,
        "comparison": "score > threshold",
        "model": model,
        "tables": per_table,
        "totals": totals,
    }
