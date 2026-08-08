"""Persist signal dimension profiles + data health rows."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import List, Optional

from ...storage.adapters.factory import AdapterBundle
from ...storage.db.write_gate import batched_writes
from .data_health import DataHealthComputer
from .dimension_registry import SIGNAL_DIMENSIONS


class DimensionProfileUpdater:
    def __init__(self, adapters: AdapterBundle, conn: Optional[sqlite3.Connection] = None) -> None:
        self._adapters = adapters
        self._conn = conn

    def upsert_all(self, deferred_jobs: Optional[List[str]] = None) -> int:
        profiles = DataHealthComputer(self._adapters, self._conn).compute(
            deferred_jobs=deferred_jobs
        )
        measured = {
            dim_id: profile for dim_id, profile in profiles.items() if profile.get("measured")
        }
        if not self._conn:
            return len(measured)
        written = 0
        # Profiles are computed above; the hold only covers these inserts.
        with batched_writes(self._conn):
            for dim in SIGNAL_DIMENSIONS:
                dim_id = dim["id"]
                profile = measured.get(dim_id)
                if not profile:
                    # Honesty rule: no real signal → no health row; the dimension
                    # renders as "not yet measured", never a zero bar.
                    continue
                profile_id = str(uuid.uuid4())
                payload = json.dumps(profile)
                self._conn.execute(
                    """
                    INSERT INTO signal_dimension_profiles (
                        profile_id, signal_dimension, source_id, profile_json, model, provider
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id) DO NOTHING
                    """,
                    (profile_id, dim_id, None, payload, profile.get("model"), profile.get("provider")),
                )
                health_id = str(uuid.uuid4())
                score = float(profile.get("score") or 0.0)
                self._conn.execute(
                    """
                    INSERT INTO data_health_dimension (
                        health_id, signal_dimension, source_id, score, model, provider, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (health_id, dim_id, None, score, None, "wiki_mvp", payload),
                )
                written += 1
        return written
