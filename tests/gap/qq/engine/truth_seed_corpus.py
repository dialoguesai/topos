"""TRU truthfulness corpus: a deterministic scratch fact store for the
mode-gated verify_claim path (PLAN_TRUTHFULNESS_PLUGIN.md §P0/D9).

Sibling of imbalance_seed_corpus.py in spirit but much smaller: fun-mode v1
verifies against the FACT layer only, so the corpus is a handful of facts with
controlled provenance (asserted_by), disclosure, and category placement. No
models, no network, no vectors — byte-identical across machines.

Two families:
  * FUN-SAFE facts the TRU correctness cases score against (hobby, food,
    sports, music) across all three lanes (owner / contact / page-author).
  * TRAP facts that must NEVER become evidence: a sensitive health fact and an
    owner_only-but-fun-category fact. The leak gates assert these are invisible
    at every layer (no retrieval touch for sensitive claims; zero eligible
    evidence for owner_only facts; no fact text in any response).

Bump TRUTH_CORPUS_VERSION when facts or canaries change.
"""

from __future__ import annotations

import sqlite3

TRUTH_CORPUS_VERSION = "qq-tru-1"

OWNER_ENTITY_ID = "tru-self-1"

# Canary strings: if any of these appear in a verify_claim RESPONSE, evidence
# leaked. (They are expected in the DB — the invariant is about responses.)
FACT_TEXT_CANARIES = (
    "playing the mandolin",
    "cilantro",
    "tennis on sundays",
    "vinyl records",
    "pollen allergy",
    "knitting tiny hats",
)


def build_truth_corpus(conn: sqlite3.Connection) -> None:
    """Seed the fact store on an already-migrated connection."""
    from topos.features.facts.store import FactStore

    store = FactStore(conn)

    def _fact(predicate: str, value: str, *, dimension: str, disclosure: str = "scoped",
              asserted_by: str = "owner") -> None:
        store.assert_fact(
            subject_entity_id=OWNER_ENTITY_ID,
            predicate=predicate,
            object_value=value,
            dimension=dimension,
            confidence=0.8,
            source_refs=[{"table": "seed", "record_id": f"tru-{predicate}"}],
            disclosure=disclosure,
            asserted_by=asserted_by,
        )

    # --- Fun-safe facts (the correctness surface) --------------------------------------
    # self lane
    _fact("enjoys", "playing the mandolin", dimension="interests")
    _fact("dislikes", "cilantro", dimension="interests")
    # attributed lane (a contact holds this to be true about the owner)
    _fact("plays", "tennis on sundays", dimension="interests", asserted_by="contact:saskia")
    # ambient lane (page-author claim the owner was merely exposed to)
    _fact("collects", "vinyl records", dimension="interests", asserted_by="page-author")

    # --- Trap facts (must never surface through fun mode) ------------------------------
    # Sensitive category AND owner_only: double-excluded.
    _fact("diagnosed_with", "pollen allergy", dimension="wellbeing", disclosure="owner_only")
    # Fun category but owner_only: excluded by the disclosure ceiling alone.
    _fact("secretly_enjoys", "knitting tiny hats", dimension="interests", disclosure="owner_only")
