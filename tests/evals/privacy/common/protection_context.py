"""Real, empty protection state for synthetic memory-adapter evaluations.

An absent connection means unknown protection state and must withhold. These
fixtures deliberately observe an empty migrated database so their permitted
controls still exercise disclosure and minimization. No owner data is used.
"""

import sqlite3
import weakref
from contextlib import contextmanager

from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.storage.adapters.fakes import InMemorySignalFeatureStore
from topos.storage.db.migrations import apply_all_migrations


def observed_empty_signal_store():
    store = InMemorySignalFeatureStore()
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    store._conn = conn
    weakref.finalize(store, conn.close)
    return store


@contextmanager
def query_principal(*, owner):
    """Model the authenticated door, separately from payload identity labels."""
    principal = Principal(OWNER_APP, "uds") if owner else Principal(THIRD_PARTY, "cp_relay")
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)
