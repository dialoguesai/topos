from __future__ import annotations


def should_write_to_oplog(op_type: str, payload: dict, enable_sync: bool) -> bool:
    _ = (op_type, payload, enable_sync)
    return True
