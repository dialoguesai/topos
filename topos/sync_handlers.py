from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from .core import state
from .storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.sync_handlers")


async def handle_sync_op(op: Dict[str, Any]) -> None:
    if not state.oplog_manager or not state.projection_manager or not state.db_conn:
        logger.error("Database not initialized, cannot handle sync op")
        return

    op_id = op.get("op_id")
    if not op_id:
        logger.warning("Received sync op without op_id")
        return

    existing = state.oplog_manager.get_op(op_id) if state.oplog_manager else None
    if existing:
        state.set_engine_config_value(state.db_conn, "last_sync_at", datetime.now(timezone.utc).isoformat())
        state.set_engine_config_value(state.db_conn, "last_received_hlc_ts", op.get("hlc_ts", ""))
        state.set_engine_config_value(state.db_conn, "last_received_op_id", op_id)
        return

    ciphertext_b64 = op.get("ciphertext", "")
    if isinstance(ciphertext_b64, str):
        try:
            ciphertext = base64.b64decode(ciphertext_b64)
        except Exception as exc:
            logger.error("Failed to decode ciphertext for op %s: %s", op_id[:8], exc)
            return
    else:
        ciphertext = ciphertext_b64

    try:
        with with_db_write():
            state.db_conn.execute(
                """
                INSERT OR IGNORE INTO oplog (
                    op_id, dataset_id, actor_id, hlc_ts,
                    protocol_version, op_type, payload_version, crypto_version, ciphertext
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    op.get("op_id"),
                    op.get("dataset_id"),
                    op.get("actor_id"),
                    op.get("hlc_ts"),
                    op.get("protocol_version", 1),
                    op.get("op_type"),
                    op.get("payload_version", 1),
                    op.get("crypto_version", 1),
                    ciphertext,
                ),
            )
            commit_connection(state.db_conn)

        decrypted_op = state.oplog_manager.get_op(op_id) if state.oplog_manager else None
        if decrypted_op and state.projection_manager:
            state.projection_manager.apply_op(decrypted_op)
        state.set_engine_config_value(state.db_conn, "last_sync_at", datetime.now(timezone.utc).isoformat())
        state.set_engine_config_value(state.db_conn, "last_received_hlc_ts", op.get("hlc_ts", ""))
        state.set_engine_config_value(state.db_conn, "last_received_op_id", op_id)
    except Exception as exc:
        logger.error("Failed to apply sync op %s: %s", op_id[:8], exc)
        state.db_conn.rollback()
