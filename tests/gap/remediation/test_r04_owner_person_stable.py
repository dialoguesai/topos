"""Gap: owner person stable — PRD_04"""
import pytest
from topos.storage.person.identity_resolver import IdentityResolver
from remediation_helpers import sqlite_conn
pytestmark = pytest.mark.gap

def test_owner_person_id_stable() -> None:
    conn = sqlite_conn()
    r = IdentityResolver(conn, owner_user_id="owner-abc")
    first = r.ensure_owner_person()
    second = r.ensure_owner_person()
    assert first == second
