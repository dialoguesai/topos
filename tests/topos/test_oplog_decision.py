"""Oplog decision logic (migrated from tests/engine). Topos has topos.storage.oplog.decision."""

import pytest
from topos.storage.oplog.decision import should_write_to_oplog


def test_should_write_to_oplog_returns_bool():
    """should_write_to_oplog returns a boolean."""
    result = should_write_to_oplog("message_created", {}, enable_sync=False)
    assert isinstance(result, bool)


def test_should_write_to_oplog_accepts_op_type_payload_enable_sync():
    """should_write_to_oplog accepts op_type, payload, enable_sync without error."""
    should_write_to_oplog("device_paired", {"device_id": "x"}, enable_sync=True)
    should_write_to_oplog("message_append", {}, enable_sync=False)
