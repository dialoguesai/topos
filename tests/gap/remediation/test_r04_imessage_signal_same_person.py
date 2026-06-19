"""Gap: cross-source person — PRD_04"""
import pytest
from topos.storage.person.identity_resolver import IdentityResolver
from remediation_helpers import sqlite_conn
pytestmark = pytest.mark.gap

def test_same_phone_one_person_id() -> None:
    conn = sqlite_conn()
    resolver = IdentityResolver(conn, owner_user_id="user-1")
    p1 = resolver.resolve("phone", "+15551212").person_id
    p2 = resolver.resolve("phone", "+15551212").person_id
    assert p1 == p2
