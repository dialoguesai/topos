"""How much of a multilingual node's fact table the SQL keys cannot key exactly (count only).

Migration 78 keys a fact in SQL only when SQLite and Python must read it identically: for
a claim, when its predicate and object are plain printable ASCII. The owner's corpus is not
only English, so this reports what share of facts falls to the Python completion pass, and
what share stays always-checked because Python cannot key it either (the design session's
condition 1). Synthetic text; no real corpus is read.

    TOPOS_ENV_FILE=<scratch> python scripts/permissions_v2/bk3_opaque_fraction.py
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# A node whose owner writes in several languages; the shares are the axis, not a measurement
# of anyone's corpus.
LANGUAGES = {
    "ascii": ["works on the roadmap", "pays the invoice", "meets the vendor"],
    "latin1": ["wohnt in Zürich", "déjeuner avec l'équipe", "mudou-se para São Paulo"],
    "greek_cyrillic": ["μένει στην Αθήνα", "работает в Москве"],
    "cjk": ["東京に住んでいる", "在上海工作"],
    "emoji": ["ships the release 🚀", "loves ☕ in the morning"],
}


def build(share_non_ascii: float, facts: int, seed: int) -> dict:
    from tests.permissions_v2 import production_corpus as pc
    from topos.storage.db.migrations import permissions_fact_lineage_keys_v1 as lk

    rng = random.Random(seed)
    with tempfile.TemporaryDirectory(prefix="bk3-opaque-") as scratch:
        path = Path(scratch) / "canonical.db"
        with sqlite3.connect(path) as conn:
            pc.production_schema(conn)
            from topos.features.facts.store import FactStore
            store = FactStore(conn)
            for number in range(facts):
                if rng.random() < share_non_ascii:
                    kind = rng.choice([name for name in LANGUAGES if name != "ascii"])
                else:
                    kind = "ascii"
                store.assert_fact(subject_entity_id=pc.OWNER_ENTITY, predicate=rng.choice(("works_on", "lives_in", "knows")),
                                  object_value=f"{rng.choice(LANGUAGES[kind])} {number}", disclosure="scoped",
                                  source_refs=[{"table": "conversation_messages", "record_id": f"imessage:{number}",
                                                "source_id": "imessage", "dataset_id": pc.DATASET}], asserted_by="owner")
            lk.complete_pending(conn)  # what the node's next start does
            counts = dict(conn.execute("SELECT family || '/' || state, count(*) FROM permissions_v2_fact_key_opaque GROUP BY 1"))
            keyed = conn.execute("SELECT count(*) FROM permissions_v2_fact_claim_keys").fetchone()[0]
            completion = conn.execute("SELECT count(*) FROM permissions_v2_fact_key_completion").fetchone()[0]
    return {"facts": facts, "share_non_ascii": share_non_ascii, "claims_keyed_in_sql": keyed,
            "completion_rows": completion, "opaque_by_family_and_state": counts,
            "always_checked": sum(count for key, count in counts.items() if key.endswith("/2"))}


def main() -> int:
    report = [build(share, facts=400, seed=11) for share in (0.0, 0.1, 0.5, 0.9)]
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
