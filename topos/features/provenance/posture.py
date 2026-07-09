"""Posture resolution for the derivation path (PLAN_PROVENANCE_SPLIT P1.4).

The pure ``record_role`` contract (roles.py) takes an EFFECTIVE posture string
and applies the override cap. THIS module is the impure companion that resolves
that string per canonical row at enrichment time: it reads the owner's
per-connector override (user_ingestion_sources) via
``sources.registry.effective_posture``, falling back to the registry default,
then 'mixed'.

Enrichment jobs iterate large batches, so :func:`make_posture_resolver` returns
a closure that caches the resolution per ``(source_id, dataset_id)`` — the DB
override is read at most once per source per batch. Kept out of roles.py so
that module stays pure stdlib with no engine/DB imports.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("topos.features.provenance.posture")


def _resolve(source_id: str, dataset_id: str, conn) -> str:
    try:
        from ...sources.registry import effective_posture

        return effective_posture(source_id, dataset_id, conn)
    except Exception as exc:  # noqa: BLE001
        logger.debug("effective_posture resolution failed (%s); defaulting mixed", exc)
        return "mixed"


def make_posture_resolver(conn=None) -> Callable[[Dict[str, Any]], str]:
    """Return ``resolver(row) -> posture`` with per-source memoization.

    Pass a DB connection to honour the owner's stored per-connector override;
    without one (or when a row carries no source_id) resolution falls through
    to the registry default / 'mixed'. A row may also carry a pre-resolved
    ``_posture`` stamp (set upstream at canonicalization) which is trusted
    verbatim so re-resolution is skipped."""
    if conn is None:
        try:
            from ...core.state import get_db_connection

            conn = get_db_connection()
        except Exception:  # noqa: BLE001
            conn = None

    cache: Dict[Tuple[str, str], str] = {}

    def resolver(row: Dict[str, Any]) -> str:
        stamped = row.get("_posture")
        if stamped:
            return str(stamped).strip().lower()
        source_id = str(row.get("source_id") or "").strip()
        if not source_id:
            return "mixed"
        dataset_id = str(row.get("dataset_id") or "").strip()
        key = (source_id, dataset_id)
        cached = cache.get(key)
        if cached is not None:
            return cached
        resolved = _resolve(source_id, dataset_id, conn)
        cache[key] = resolved
        return resolved

    return resolver
