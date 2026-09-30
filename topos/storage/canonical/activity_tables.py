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
        writer_class: Optional[str] = None,
        writer_app_id: Optional[str] = None,
        writer_dataset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upsert a batch of activity rows under the door that wrote them.

        The writer fields always come from the caller (the door), never from a
        record: a mapped payload that carried its own would otherwise choose its
        provenance. ``refs`` holds one CanonicalRef per record, in order, so the
        caller can see a write the store refused.
        """
        if not records:
            return {"events_created": 0, "events_unchanged": 0, "refs": []}
        payloads = [
            {
                **record,
                "source_id": source_id,
                "writer_class": writer_class,
                "writer_app_id": writer_app_id,
                "writer_dataset_id": writer_dataset_id,
            }
            for record in records
        ]
        refs = self._store.upsert_batch("activity_events", payloads, sync_batch_id=sync_batch_id)
        created = sum(1 for ref in refs if ref.created)
        return {"events_created": created, "events_unchanged": len(refs) - created, "refs": refs}
