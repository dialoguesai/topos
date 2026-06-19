"""Activity events canonical table manager."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .canonical_store import SQLiteCanonicalStore


class ActivityEventsManager:
    def __init__(self, conn) -> None:
        self.conn = conn
        self._store = SQLiteCanonicalStore(conn)

    def upsert_batch(
        self,
        records: List[Dict[str, Any]],
        *,
        source_id: str,
        sync_batch_id: Optional[str] = None,
    ) -> Dict[str, int]:
        if not records:
            return {"events_created": 0, "events_unchanged": 0}
        payloads = [{**record, "source_id": source_id} for record in records]
        refs = self._store.upsert_batch("activity_events", payloads, sync_batch_id=sync_batch_id)
        created = sum(1 for ref in refs if ref.created)
        return {"events_created": created, "events_unchanged": len(refs) - created}
