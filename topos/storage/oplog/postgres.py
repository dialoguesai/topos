from __future__ import annotations


class PostgresOplogStore:
    def __init__(self, conn):
        self.conn = conn

    def append(self, entry):
        _ = entry
        raise NotImplementedError("PostgresOplogStore not implemented yet")
