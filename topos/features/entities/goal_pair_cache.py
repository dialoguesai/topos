"""The goal lane's similar-pair decisions, kept from one graph rebuild to the next.

``graph_enrichers._similar_goal_pairs`` decides each pair of goal groups on its own: cosine of the two texts'
vectors against GOAL_EMBED_MERGE_SCORE, else token-set similarity of the two keys against GOAL_TOKEN_MERGE_SCORE.
Nothing about a pair depends on any other goal. So a pair decided once stays decided for as long as both sides keep
their key, their text and the embedder that scored them, and a rebuild only has to score the goals that are new (or
changed) against all of them. The full comparison grows as n²: 9,848 goals took 574 s of a 627 s rebuild on a copy
of the owner's node (4 Nov 2026), and a rebuild reran it whenever any message arrived.

An *item* is one goal group as the comparison saw it: a digest of the rule (the two thresholds and RULE_VERSION),
the embedder tag (``tokens`` when there were no vectors), the key and the text. The tables hold the items that were
all compared with each other at the last rebuild, and the merged pairs among them. Each rebuild makes both its own
items and pairs (writing only the difference), so a goal that left takes its pairs with it.

Both tables are the rebuild's own output. They are not graph inputs (``graph_inputs``), and reading or writing them
never changes what the graph reads. A cache that cannot be read is empty: the rebuild then compares every pair, as
it always did, and stores the result for the next one.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("topos.features.entities.goal_pair_cache")

#: Bump when ``_similar_goal_pairs`` starts deciding a pair differently for the same key, text and vectors.
RULE_VERSION = 1

ITEMS_TABLE = "graph_goal_pair_items"
PAIRS_TABLE = "graph_goal_pairs"


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(f"CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (item TEXT PRIMARY KEY) WITHOUT ROWID")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {PAIRS_TABLE} (a TEXT NOT NULL, b TEXT NOT NULL, PRIMARY KEY (a, b)) WITHOUT ROWID"
    )


class GoalPairCache:
    """Load and store the pair decisions for one embedder tag. ``tag`` names how vectors were made."""

    def __init__(self, conn: sqlite3.Connection, tag: str) -> None:
        self.conn = conn
        self.tag = str(tag)
        # After a failed read, ``store`` replaces both tables: a difference against an empty read would leave the
        # items it could not see, and one of them coming back would skip its pairs with goals scored meanwhile.
        self._replace = False

    def items(self, keys: Sequence[str], texts: Sequence[str], *, vectors: bool) -> List[str]:
        from .graph_enrichers import GOAL_EMBED_MERGE_SCORE, GOAL_TOKEN_MERGE_SCORE

        rule = f"{RULE_VERSION}|{GOAL_EMBED_MERGE_SCORE!r}|{GOAL_TOKEN_MERGE_SCORE!r}|{self.tag if vectors else 'tokens'}"
        return [
            hashlib.blake2b(f"{rule}\0{key}\0{text}".encode("utf-8"), digest_size=16).hexdigest()
            for key, text in zip(keys, texts)
        ]

    def load(self) -> Tuple[Set[str], Set[Tuple[str, str]]]:
        """(items compared with each other last time, merged pairs among them); empty when unreadable."""
        try:
            known = {str(r[0]) for r in self.conn.execute(f"SELECT item FROM {ITEMS_TABLE}")}
            pairs = {(str(a), str(b)) for a, b in self.conn.execute(f"SELECT a, b FROM {PAIRS_TABLE}")}
        except sqlite3.Error as exc:
            self._replace = True
            if "no such table" not in str(exc):
                logger.info("goal pair cache unreadable (%s); comparing every pair", type(exc).__name__)
            return set(), set()
        return known, pairs

    def store(
        self,
        items: Sequence[str],
        pairs: Iterable[Tuple[str, str]],
        known: Set[str],
        cached: Set[Tuple[str, str]],
    ) -> None:
        """Make the cache this rebuild's items and pairs, given what ``load`` returned; a rebuild whose goals did not
        change writes nothing. The rows it does write count in the rebuild's ``rows_written["enrichments"]``.
        A failure only costs the next rebuild a full pass."""
        from ...storage.db.write_gate import commit_connection, with_db_write

        now = set(items)
        now_pairs = {(min(a, b), max(a, b)) for a, b in pairs}
        gone, new = known - now, now - known
        gone_pairs, new_pairs = cached - now_pairs, now_pairs - cached
        if self._replace:
            gone, gone_pairs, new, new_pairs = set(), set(), now, now_pairs
        elif not (gone or new or gone_pairs or new_pairs):
            return
        try:
            with with_db_write():
                _ensure(self.conn)
                if self._replace:
                    self.conn.execute(f"DELETE FROM {ITEMS_TABLE}")
                    self.conn.execute(f"DELETE FROM {PAIRS_TABLE}")
                self.conn.executemany(f"DELETE FROM {ITEMS_TABLE} WHERE item = ?", [(i,) for i in gone])
                self.conn.executemany(f"DELETE FROM {PAIRS_TABLE} WHERE a = ? AND b = ?", sorted(gone_pairs))
                self.conn.executemany(f"INSERT OR IGNORE INTO {ITEMS_TABLE} (item) VALUES (?)", [(i,) for i in new])
                self.conn.executemany(f"INSERT OR IGNORE INTO {PAIRS_TABLE} (a, b) VALUES (?, ?)", sorted(new_pairs))
                commit_connection(self.conn)
        except sqlite3.Error as exc:
            try:
                self.conn.rollback()
            except sqlite3.Error:
                pass
            logger.info("goal pair cache not stored (%s); the next rebuild compares every pair", type(exc).__name__)


def similar_pairs_with_cache(
    keys: List[str], texts: Sequence[str], vectors, cache: Optional[GoalPairCache]
) -> Tuple[List[Tuple[int, int]], Dict[str, int]]:
    """``_similar_goal_pairs(keys, vectors)``, scoring only the items the cache has not compared yet.

    Returns the same sorted (i, j) list the full comparison returns, and counts for the rebuild report.
    """
    from .graph_enrichers import _similar_goal_pairs

    if cache is None:
        return _similar_goal_pairs(keys, vectors), {"goal_items": len(keys), "goal_items_scored": len(keys)}
    items = cache.items(keys, texts, vectors=vectors is not None)
    known, cached = cache.load()
    index = {item: i for i, item in enumerate(items)}
    fresh = [i for i, item in enumerate(items) if item not in known]
    pairs = {
        (min(index[a], index[b]), max(index[a], index[b]))
        for a, b in cached
        if a in index and b in index and a in known and b in known
    }
    if fresh:
        pairs.update(_similar_goal_pairs(keys, vectors, rows=fresh))
    result = sorted(pairs)
    cache.store(items, [(items[i], items[j]) for i, j in result], known, cached)
    return result, {"goal_items": len(keys), "goal_items_scored": len(fresh)}
