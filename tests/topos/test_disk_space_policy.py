"""The owner's minimum-free-disk floor: what it defaults to, and what reads it.

The setting is one number, so the interesting part is not storing it — it is
that losing it must fail safe. A node that cannot read its floor keeps the
10 GB default rather than dropping to zero, because zero is the value that
lets a download fill the volume the node's SQLite lives on.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from topos.api.disk_space_config import (
    normalize_put_min_free_bytes,
    policy_payload,
)
from topos.config.settings import (
    ENGINE_CONFIG_KEY_MIN_FREE_DISK_BYTES,
    MIN_FREE_DISK_BYTES_DEFAULT,
    MIN_FREE_DISK_BYTES_MAX,
    resolve_min_free_disk_bytes,
    settings,
)
from topos.engine import disk_space

GB = 1024**3


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT)")
    yield db
    db.close()


def _store(conn, value):
    conn.execute(
        "INSERT OR REPLACE INTO engine_config (key, value) VALUES (?, ?)",
        (ENGINE_CONFIG_KEY_MIN_FREE_DISK_BYTES, str(value)),
    )


def test_the_shipped_default_is_ten_gigabytes(conn):
    assert MIN_FREE_DISK_BYTES_DEFAULT == 10 * GB
    assert resolve_min_free_disk_bytes(settings, conn) == 10 * GB


def test_the_owners_value_wins_over_the_default(conn):
    _store(conn, 25 * GB)
    assert resolve_min_free_disk_bytes(settings, conn) == 25 * GB


def test_zero_is_a_real_choice_not_a_missing_one(conn):
    """"Do not hold anything back" has to be expressible, or an owner with a
    small disk cannot download anything at all."""
    _store(conn, 0)
    assert resolve_min_free_disk_bytes(settings, conn) == 0


def test_a_stored_value_that_makes_no_sense_degrades_to_something_usable(conn):
    _store(conn, "not a number")
    assert resolve_min_free_disk_bytes(settings, conn) == MIN_FREE_DISK_BYTES_DEFAULT

    _store(conn, -5)
    assert resolve_min_free_disk_bytes(settings, conn) == 0

    _store(conn, 900 * 1024**4)
    assert resolve_min_free_disk_bytes(settings, conn) == MIN_FREE_DISK_BYTES_MAX


def test_no_database_keeps_the_floor_rather_than_dropping_it():
    """Losing the setting must never be the thing that fills the volume."""
    assert resolve_min_free_disk_bytes(settings, None) == MIN_FREE_DISK_BYTES_DEFAULT
    with patch(
        "topos.core.state.get_db_connection", side_effect=RuntimeError("no db")
    ):
        assert disk_space.min_free_bytes() == disk_space.DEFAULT_MIN_FREE_BYTES


def test_the_floor_is_what_the_space_check_reserves(conn):
    """The setting is not decoration: raising it refuses a download that a
    lower floor would have allowed."""
    _store(conn, 30 * GB)
    with patch.object(disk_space, "free_bytes", return_value=20 * GB):
        assert disk_space.check_space_for(2 * GB, conn=conn) is not None

    _store(conn, 5 * GB)
    with patch.object(disk_space, "free_bytes", return_value=20 * GB):
        assert disk_space.check_space_for(2 * GB, conn=conn) is None


def test_a_put_refuses_out_of_range_rather_than_quietly_clamping():
    """The form has to show the number the node agreed to."""
    assert normalize_put_min_free_bytes({"min_free_bytes": 20 * GB}) == 20 * GB
    assert normalize_put_min_free_bytes({"min_free_bytes": "0"}) == 0

    with pytest.raises(ValueError, match="required"):
        normalize_put_min_free_bytes({})
    with pytest.raises(ValueError, match="number of bytes"):
        normalize_put_min_free_bytes({"min_free_bytes": "lots"})
    with pytest.raises(ValueError, match="between"):
        normalize_put_min_free_bytes({"min_free_bytes": -1})
    with pytest.raises(ValueError, match="between"):
        normalize_put_min_free_bytes({"min_free_bytes": 900 * 1024**4})


def test_the_policy_payload_carries_the_bounds_the_form_needs(conn):
    payload = policy_payload(conn)
    assert payload["min_free_bytes"] == 10 * GB
    assert payload["default_min_free_bytes"] == 10 * GB
    assert payload["min_allowed_bytes"] == 0
    assert payload["max_allowed_bytes"] == MIN_FREE_DISK_BYTES_MAX


def test_status_reports_below_floor_only_on_a_real_measurement(conn):
    _store(conn, 10 * GB)
    with patch.object(disk_space, "free_bytes", return_value=3 * GB):
        status = disk_space.disk_status(conn)
    assert status["below_floor"] is True
    assert status["shortfall_bytes"] == 7 * GB
    assert status["min_free_bytes"] == 10 * GB

    with patch.object(disk_space, "free_bytes", return_value=40 * GB):
        assert disk_space.disk_status(conn)["below_floor"] is False

    with patch.object(disk_space, "free_bytes", return_value=None):
        unreadable = disk_space.disk_status(conn)
    assert unreadable["below_floor"] is False, "an unreadable volume is not a full one"
    assert unreadable["free_bytes"] is None


def test_status_declines_to_report_a_remote_ollamas_disk(conn):
    """The badge must not warn the owner about a machine that is not theirs."""
    with patch.object(disk_space, "free_bytes", return_value=0):
        status = disk_space.disk_status(conn, base_url="http://gpu-box:11434")
    assert status["applies"] is False
    assert status["free_bytes"] is None
    assert status["below_floor"] is False
