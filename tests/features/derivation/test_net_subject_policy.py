"""Three gates stand between an assertion and someone else's dossier.

The derivation writer can route `about=other:<name>` onto that person's entity and
write a durable fact there. That capability shipped before anything authorised it;
for a while the only thing preventing third-party dossiers was that person-name
resolution happened to fail, which is an accident, not a policy.

Every gate fails closed, and each refusal names itself so the review queue can say
what would have to change:

    net_subject_pack_denies      the pack may not describe other people at all
    net_subject_policy_absent    the consent plane is not installed
    net_subject_not_opted_in     nobody has decided about this person
    net_subject_blackholed       the owner excluded this person
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.derivation.net_subject_policy import (
    ALLOW,
    DENY,
    may_write_about,
    set_subject_policy,
    subject_policy,
)
from topos.storage.db.migrations.net_subject_policy_v1 import (
    apply_net_subject_policy_v1_up,
)


@pytest.fixture()
def bare(tmp_path):
    """A node whose migration has not landed yet — the shipping window."""
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    yield c
    c.close()


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "p.db"))
    c.execute("CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
    c.execute(
        """CREATE TABLE entity_blackholes (blackhole_id TEXT PRIMARY KEY,
           entity_id TEXT NOT NULL DEFAULT '', normalized_name TEXT NOT NULL)"""
    )
    apply_net_subject_policy_v1_up(c)
    yield c
    c.close()


# --- gate 1: the pack ---

def test_a_pack_that_does_not_declare_net_subject_is_refused(conn):
    set_subject_policy(conn, "ent_nora", ALLOW)
    d = may_write_about(conn, "ent_nora", pack_allows_net_subject=False)
    assert not d.allowed
    assert d.reason == "net_subject_pack_denies"


def test_pack_permission_alone_is_not_enough(conn):
    """A pack saying 'allow' authorises the CLASS of writes, not any person."""
    d = may_write_about(conn, "ent_nora", pack_allows_net_subject=True)
    assert not d.allowed
    assert d.reason == "net_subject_not_opted_in"


# --- gate 2: the subject ---

def test_absence_of_a_row_is_a_refusal(conn):
    """'Off until asked' — a person nobody has ruled on is not a subject."""
    assert subject_policy(conn, "ent_stranger") is None
    assert not may_write_about(conn, "ent_stranger", pack_allows_net_subject=True).allowed


def test_an_explicit_deny_is_a_refusal(conn):
    set_subject_policy(conn, "ent_nora", DENY)
    d = may_write_about(conn, "ent_nora", pack_allows_net_subject=True)
    assert not d.allowed
    assert d.reason == "net_subject_not_opted_in"


def test_an_opted_in_subject_with_an_allowing_pack_is_permitted(conn):
    """The one path that opens — everything else refuses."""
    set_subject_policy(conn, "ent_nora", ALLOW, note="asked her, she agreed")
    d = may_write_about(conn, "ent_nora", pack_allows_net_subject=True)
    assert d.allowed
    assert d.reason == "net_subject_allowed"


def test_a_decision_can_be_reversed(conn):
    set_subject_policy(conn, "ent_nora", ALLOW)
    assert may_write_about(conn, "ent_nora", pack_allows_net_subject=True).allowed
    set_subject_policy(conn, "ent_nora", DENY)
    assert not may_write_about(conn, "ent_nora", pack_allows_net_subject=True).allowed


def test_only_allow_or_deny_are_recordable(conn):
    for bad in ("maybe", "", "ALLOWED", None):
        with pytest.raises(ValueError):
            set_subject_policy(conn, "ent_nora", bad)


# --- gate 3: the blackhole, consulted at WRITE time ---

def test_a_blackholed_subject_is_refused_even_when_opted_in(conn):
    """The blackhole used to be a read-side filter only, so an excluded person
    kept accruing facts that were merely hidden afterwards. Excluding someone
    should stop the node thinking about them, not just stop it talking."""
    set_subject_policy(conn, "ent_nora", ALLOW)
    conn.execute(
        "INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name)"
        " VALUES ('bh1', 'ent_nora', 'nora')"
    )
    conn.commit()
    d = may_write_about(conn, "ent_nora", pack_allows_net_subject=True)
    assert not d.allowed
    assert d.reason == "net_subject_blackholed"


def test_an_unreadable_blackhole_table_denies(tmp_path):
    """A sick database must not read as permission."""
    c = sqlite3.connect(str(tmp_path / "sick.db"))
    c.execute("CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
    apply_net_subject_policy_v1_up(c)  # policy table exists, blackholes do not
    set_subject_policy(c, "ent_nora", ALLOW)
    d = may_write_about(c, "ent_nora", pack_allows_net_subject=True)
    assert not d.allowed
    assert d.reason in ("net_subject_blackholed", "net_subject_blackhole_unreadable")
    c.close()


# --- the shipping window: code before migration ---

def test_a_missing_policy_table_denies_everything(bare):
    d = may_write_about(bare, "ent_nora", pack_allows_net_subject=True)
    assert not d.allowed
    assert d.reason == "net_subject_policy_absent"


def test_the_absent_reason_is_distinct_from_not_opted_in(bare, conn):
    """An operator reading 'policy absent' knows to ship the migration; 'not opted
    in' would send them hunting for a decision to make."""
    absent = may_write_about(bare, "ent_x", pack_allows_net_subject=True).reason
    present = may_write_about(conn, "ent_x", pack_allows_net_subject=True).reason
    assert absent != present


def test_recording_a_decision_without_the_table_raises(bare):
    """Silently dropping the owner's decision would be worse than refusing it."""
    with pytest.raises(RuntimeError):
        set_subject_policy(bare, "ent_nora", ALLOW)


# --- the pack contract ---

def test_every_shipped_pack_defaults_to_deny():
    """No pack may describe other people until someone writes it down.

    Before this field existed, any enabled pack could emit an outward fact —
    including the special-class health.* packs.
    """
    import pathlib

    from topos.features.derivation.packs import load_packs

    pack_dir = pathlib.Path(__file__).resolve().parents[3].parent / "derivation-packs"
    if not pack_dir.exists():  # pragma: no cover - packs live outside the wheel
        pytest.skip("derivation-packs not on disk")
    packs = load_packs(pack_dir)
    assert packs, "no packs loaded"
    outward = [p.pack for p in packs.values() if p.net_subject == ALLOW]
    assert outward == [], f"packs claiming net_subject=allow: {outward}"


def test_a_pack_may_not_declare_a_nonsense_net_subject(tmp_path):
    import yaml

    from topos.features.derivation.packs import PackValidationError, load_pack

    spec = {
        "pack": "t.test", "version": "0.1.0", "title": "T",
        "sensitivity_class": "personal", "role_policy": "authored_addressed",
        "disclosure_default": "owner_only", "net_subject": "sometimes",
        "routing": {}, "guidance": {}, "consumers": ["x"],
        "eval": {"gold": [{"a": 1}], "negative_controls": [{"b": 2}]},
        "predicates": {},
    }
    f = tmp_path / "t.yaml"
    f.write_text(yaml.safe_dump(spec))
    with pytest.raises(PackValidationError):
        load_pack(f)
