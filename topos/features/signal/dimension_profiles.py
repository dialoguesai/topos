"""Persist signal dimension profiles + data health rows."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import List, Optional

from ...storage.adapters.factory import AdapterBundle
from .data_health import DataHealthComputer
from .dimension_registry import MVP_DIMENSIONS


class DimensionProfileUpdater:
    def __init__(self, adapters: AdapterBundle, conn: Optional[sqlite3.Connection] = None) -> None:
        self._adapters = adapters
        self._conn = conn

    def upsert_all(self, deferred_jobs: Optional[List[str]] = None) -> int:
        profiles = DataHealthComputer(self._adapters).compute(deferred_jobs=deferred_jobs)
        if not self._conn:
            return len(profiles)
        written = 0
        for dim in MVP_DIMENSIONS:
            dim_id = dim["id"]
            profile = profiles.get(dim_id) or {}
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
            score = float(profile.get("coverage_score") or 0.0)
            self._conn.execute(
                """
                INSERT INTO data_health_dimension (
                    health_id, signal_dimension, source_id, score, model, provider, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (health_id, dim_id, None, score, None, "wiki_mvp", payload),
            )
            written += 1
        self._conn.commit()
        return written
