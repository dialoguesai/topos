from __future__ import annotations

from typing import Dict

from .raw_store import RawRecordRef, RawStore


class SQLiteRawStore(RawStore):
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def write_file(self, file):  # pragma: no cover - not used for sqlite raw store
        raise NotImplementedError

    def write_record(self, record: Dict[str, str]) -> RawRecordRef:
        _ = record
        raise NotImplementedError("SQLiteRawStore not implemented yet")
