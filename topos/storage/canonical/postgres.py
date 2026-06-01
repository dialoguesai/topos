from __future__ import annotations


class PostgresCanonicalStore:
    def __init__(self, conn):
        self.conn = conn

    def upsert(self, record):
        _ = record
        raise NotImplementedError("PostgresCanonicalStore not implemented yet")
