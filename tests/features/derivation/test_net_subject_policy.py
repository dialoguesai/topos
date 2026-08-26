"""Three gates stand between an assertion and someone else's dossier.

The derivation writer can route `about=other:<name>` onto that person's entity and
write a durable fact there. That capability shipped before anything authorised it;
for a while the only thing preventing third-party dossiers was that person-name
resolution happened to fail, which is an accident, not a policy.

Every gate fails closed, and each refusal names itself so the review queue can say
what would have to change:

    net_subject_pack_denies          the pack may not describe other people at all
    net_subject_unnamed_subject      the node cannot name this person (a bare phone
                                     number), and nobody has said otherwise
    net_subject_opted_out            the owner explicitly excluded this person
    net_subject_blackholed           this person is black-holed
    net_subject_blackhole_unreadable the exclusion list could not be consulted

Posture revised 2026-08-26 (owner): the subject gate is a RULE, not a list. See the
module docstring for why, and for the live measurement behind it.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.derivation.net_subject_policy import (
    ALLOW,
    DENY,
    is_nameable_subject,
    may_write_about,
    set_subject_policy,
    subject_policy,
)
from topos.storage.db.migrations.net_subject_policy_v1 import (
    apply_net_subject_policy_v1_up,
)


def _identities(c):
    """The two tables the nameability rule reads.

    `ent_nora` is nameable; `ent_unnamed` is a bare phone number with no contact behind
    it — the 154-of-1,505 case measured on the live node.
    """
    c.execute("CREATE TABLE entities (entity_id TEXT PRIMARY KEY, canonical_name TEXT,"
              " contact_id TEXT)")
    c.execute("CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT)")
    c.executemany("INSERT INTO entities VALUES (?,?,?)", [
        ("ent_nora", "Nora Whitfield", None),
        ("ent_unnamed", "+15126331615", "c_bare"),
        ("ent_via_contact", "+13109275698", "c_named"),
    ])
    c.executemany("INSERT INTO contacts VALUES (?,?)",
                  [("c_bare", None), ("c_named", "Lima Mike")])


@pytest.fixture()
def bare(tmp_path):
    """A node whose net_subject migration has not landed — the real shipping window.

    Everything ELSE is normal: `entity_blackholes` predates this work and exists on every
    node, so leaving it out here would test a database that has never shipped and would
    mask the rule behind gate 3's (correct) refusal to proceed without an exclusion list.
    """
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    c.execute(
        """CREATE TABLE entity_blackholes (blackhole_id TEXT PRIMARY KEY,
           entity_id TEXT NOT NULL DEFAULT '', normalized_name TEXT NOT NULL)"""
    )
    _identities(c)
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
    _identities(c)
    apply_net_subject_policy_v1_up(c)
    yield c
    c.close()


# --- gate 1: the pack ---

def test_a_pack_that_does_not_declare_net_subject_is_refused(conn):
    set_subject_policy(conn, "ent_nora", ALLOW)
    d = may_write_about(conn, "ent_nora", pack_allows_net_subject=False)
    assert not d.allowed
    assert d.reason == "net_subject_pack_denies"


# --- gate 2: the subject must be NAMEABLE, or explicitly decided ---
#
# Revised 2026-08-26 (owner). The old posture was "off until asked": absence of a row
# denied, so every third party was a manual decision. That put a human in front of a lane
# meant to be automatic. The default is now a RULE — the node may hold facts about people
# it can actually name — and the table holds OVERRIDES that win in both directions.

def test_pack_permission_alone_is_not_enough(conn):
    """A pack saying 'allow' authorises the CLASS of writes; the subject still has to
    clear the rule."""
    d = may_write_about(conn, "ent_unnamed", pack_allows_net_subject=True)
    assert not d.allowed
    assert d.reason == "net_subject_unnamed_subject"


def test_a_nameable_subject_needs_no_row(conn):
    """The change, in one assertion: nobody decided about Nora, and Nora qualifies."""
    assert subject_policy(conn, "ent_nora") is None
    d = may_write_about(conn, "ent_nora", pack_allows_net_subject=True)
    assert d.allowed
    assert d.reason == "net_subject_allowed"


def test_a_bare_phone_number_is_not_a_subject(conn):
    """Quality as much as privacy: a dossier keyed to '+1555…' is not intelligence.
    Measured live 2026-08-26: 154 of 1,505 person entities are this shape."""
    assert not is_nameable_subject(conn, "ent_unnamed")
    assert not may_write_about(conn, "ent_unnamed", pack_allows_net_subject=True).allowed


def test_a_name_on_the_linked_CONTACT_counts(conn):
    """The address-book case — the entity is a phone number, the contact has a name."""
    assert is_nameable_subject(conn, "ent_via_contact")
    assert may_write_about(conn, "ent_via_contact", pack_allows_net_subject=True).allowed


def test_the_contact_LINK_alone_proves_nothing(conn):
    """The bug this rule was caught having. Keying on `contact_id` admitted 100% of
    entities, because a contact row is minted for every messenger participant whether or
    not anyone ever named them — 1,249 of 1,505 carry one, bare phone numbers included."""
    linked = conn.execute(
        "SELECT contact_id FROM entities WHERE entity_id='ent_unnamed'").fetchone()[0]
    assert linked, "fixture must have a contact link"
    assert not is_nameable_subject(conn, "ent_unnamed"), "a link is not a name"


def test_an_unknown_subject_is_not_nameable(conn):
    """'We could not check' must never read as 'yes'."""
    assert not is_nameable_subject(conn, "ent_ghost")
    assert not may_write_about(conn, "ent_ghost", pack_allows_net_subject=True).allowed


def test_an_explicit_deny_overrides_the_rule(conn):
    """The override that matters most: a person the rule would admit, excluded by name."""
    set_subject_policy(conn, "ent_nora", DENY, note="asked her, she declined")
    d = may_write_about(conn, "ent_nora", pack_allows_net_subject=True)
    assert not d.allowed
    assert d.reason == "net_subject_opted_out"


def test_an_explicit_allow_overrides_the_rule(conn):
    """And the other direction: 'I know exactly who this number is.'"""
    set_subject_policy(conn, "ent_unnamed", ALLOW, note="my landlord")
    assert may_write_about(conn, "ent_unnamed", pack_allows_net_subject=True).allowed


def test_a_decision_can_be_reversed(conn):
    set_subject_policy(conn, "ent_nora", DENY)
    assert not may_write_about(conn, "ent_nora", pack_allows_net_subject=True).allowed
    set_subject_policy(conn, "ent_nora", ALLOW)
    assert may_write_about(conn, "ent_nora", pack_allows_net_subject=True).allowed


def test_only_allow_or_deny_are_recordable(conn):
    for bad in ("maybe", "", "ALLOWED", None):
        with pytest.raises(ValueError):
            set_subject_policy(conn, "ent_nora", bad)


# --- gate 3: the blackhole, consulted at WRITE time ---

def test_a_blackholed_subject_is_refused_even_when_allowed(conn):
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

def test_a_missing_policy_table_no_longer_denies_everything(bare):
    """The posture reversal, stated as a test.

    While the table was the ONLY authorisation, its absence had to deny everything. Now
    it holds overrides, and having recorded none is an ordinary state — the rule decides.
    """
    d = may_write_about(bare, "ent_nora", pack_allows_net_subject=True)
    assert d.allowed
    assert d.reason == "net_subject_allowed"


def test_the_rule_still_excludes_the_unnameable_without_the_table(bare):
    """Losing the override table must not lose the floor."""
    d = may_write_about(bare, "ent_unnamed", pack_allows_net_subject=True)
    assert not d.allowed
    assert d.reason == "net_subject_unnamed_subject"


def test_without_the_table_the_owner_cannot_record_a_deny(bare):
    """The cost of not registering the migration yet, made explicit: until it lands the
    only way to exclude a NAMEABLE person is the blackhole. Silently dropping the owner's
    decision would be worse than refusing it."""
    with pytest.raises(RuntimeError):
        set_subject_policy(bare, "ent_nora", DENY)


# --- the pack contract ---

#: The packs allowed to describe someone other than the owner. Enumerated on purpose: this
#: list is the whole outward surface, and it should be impossible to grow it by accident.
OUTWARD_PACKS = {"net.capability"}


def test_only_the_declared_packs_describe_other_people():
    """The invariant tightened rather than relaxed when the first outward pack shipped.

    It used to read "no pack may". It now reads "only these, and only from the wheel" —
    which is the claim that actually needs guarding, because the failure mode is not the
    first outward pack, it is the fifth one nobody reviewed.
    """
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir

    packs = load_packs(bundled_pack_dir())
    assert packs, "no packs loaded"
    outward = {p.pack for p in packs.values() if p.net_subject == ALLOW}
    assert outward == OUTWARD_PACKS, f"outward surface changed: {outward ^ OUTWARD_PACKS}"


def test_an_outward_pack_must_be_first_party():
    """D9, checked against the shipped catalog rather than against a fixture."""
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir

    for p in load_packs(bundled_pack_dir()).values():
        if p.net_subject == ALLOW:
            assert p.first_party, f"{p.pack} declares allow but is not first-party"


def test_the_repo_catalog_copy_of_an_outward_pack_is_refused():
    """The mirror IS the trust boundary. The catalog copy of net.capability must fail to
    load from outside the wheel, or 'first-party' would mean 'in our repo somewhere'."""
    import pathlib

    from topos.features.derivation.packs import PackValidationError, load_pack

    f = (pathlib.Path(__file__).resolve().parents[3].parent
         / "derivation-packs" / "net.capability.yaml")
    if not f.exists():  # pragma: no cover
        pytest.skip("catalog not on disk")
    with pytest.raises(PackValidationError):
        load_pack(f)


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


# --- gate 0: first-party forever (D9) ---
#
# The three gates above decide WHO may be written about. This one decides who may
# ASK — and it exists because the other three are only as good as the ontology that
# invokes them. A community pack declaring `net_subject: allow` would be an ontology
# for profiling people who never consented to this node, which is precisely the threat
# F5's red-team gate names. The rule is first-party forever, and it lives in the loader
# because a rule that lives only in a review checklist is a policy nobody applies.

def _spec(**over):
    d = {
        "pack": "t.test", "version": "0.1.0", "title": "T",
        "sensitivity_class": "personal", "role_policy": "authored_addressed",
        "disclosure_default": "owner_only",
        "routing": {}, "guidance": {}, "consumers": ["x"],
        "eval": {"gold": [{"a": 1}], "negative_controls": [{"b": 2}]},
        "predicates": [{"name": "t.thing", "value_type": "string", "cardinality": "single",
                        "temporal": "interval", "altitude": "stated"}],
    }
    d.update(over)
    return d


def _write(tmp_path, **over):
    import yaml
    f = tmp_path / "t.yaml"
    f.write_text(yaml.safe_dump(_spec(**over)))
    return f


def test_a_third_party_pack_may_not_declare_net_subject_allow(tmp_path):
    from topos.features.derivation.packs import PackValidationError, load_pack

    with pytest.raises(PackValidationError) as e:
        load_pack(_write(tmp_path, net_subject="allow"))
    assert "first-party" in str(e.value)


def test_a_third_party_pack_loads_fine_when_it_stays_owner_subject(tmp_path):
    """The rule bans one field value, not community packs."""
    from topos.features.derivation.packs import load_pack

    assert load_pack(_write(tmp_path, net_subject="deny")).net_subject == "deny"
    assert load_pack(_write(tmp_path)).net_subject == "deny", "absent means deny"


def test_a_first_party_pack_may_declare_allow(tmp_path, monkeypatch):
    """The one path that opens — and it opens on the file's LOCATION, not its content."""
    from topos.features.derivation import packs as m

    monkeypatch.setattr(m, "first_party_pack_dirs", lambda: (tmp_path.resolve(),))
    assert m.load_pack(_write(tmp_path, net_subject="allow")).net_subject == "allow"


def test_a_symlink_does_not_inherit_first_party_trust(tmp_path, monkeypatch):
    """Trust belongs to the file, not to the name it is reachable by.

    Dropping a symlink into the shipped pack directory is the cheapest way to smuggle a
    foreign ontology in, so the check resolves the path before comparing.
    """
    from topos.features.derivation import packs as m

    blessed = tmp_path / "blessed"
    blessed.mkdir()
    outside = _write(tmp_path, net_subject="allow")
    link = blessed / "smuggled.yaml"
    link.symlink_to(outside)

    monkeypatch.setattr(m, "first_party_pack_dirs", lambda: (blessed.resolve(),))
    assert not m.is_first_party_pack(link)
    with pytest.raises(m.PackValidationError):
        m.load_pack(link)


def test_the_only_first_party_root_is_the_shipped_pack_dir():
    """One root, and it is the one production actually loads.

    Not the repo catalog: its path would have to be derived as `parents[3]`, which names
    a different directory in every topology — measured 2026-08-26 it lands on
    `~/.topos/derivation-packs` under the deployed layout, i.e. user-writable app data
    beside `database.db`. A trust boundary that moves when you deploy is not one.
    """
    from topos.features.derivation.packs import first_party_pack_dirs, is_first_party_pack
    from topos.features.derivation.registry import bundled_pack_dir

    roots = first_party_pack_dirs()
    assert len(roots) == 1, f"exactly one blessed root; got {roots}"
    assert roots[0].resolve() == bundled_pack_dir().resolve(), (
        "the blessed root must be the directory production loads")
    assert is_first_party_pack(roots[0] / "relationships.social.yaml")


def test_the_repo_catalog_is_not_first_party():
    """Recorded so nobody re-blesses it for developer convenience: the mirror into
    `bundled_packs/` IS the trust boundary, so a catalog pack declaring `allow` must fail
    until it is mirrored."""
    from pathlib import Path

    from topos.features.derivation.packs import is_first_party_pack

    catalog = Path(__file__).resolve().parents[3].parent / "derivation-packs"
    if not catalog.is_dir():  # pragma: no cover — catalog lives outside the wheel
        pytest.skip("repo catalog not on disk")
    assert not is_first_party_pack(catalog / "relationships.social.yaml")


def test_a_nonsense_value_still_reports_as_nonsense(tmp_path):
    """Ordering: the value check runs first, so a typo does not read as a policy violation."""
    from topos.features.derivation.packs import PackValidationError, load_pack

    with pytest.raises(PackValidationError) as e:
        load_pack(_write(tmp_path, net_subject="sometimes"))
    assert "must be" in str(e.value) and "first-party" not in str(e.value)


# --- D-E: the owner's own hand ---
#
# Resolved 2026-08-26 (owner): promoting a quarantined fact, or writing a note onto
# someone's card, IS the consent decision. So this path skips gate 1 (does the pack allow
# outward writes) and gate 2 (can the node name this person) — the owner just supplied by
# hand the judgement both gates approximate. It does NOT skip the blackhole, which has to
# outrank every actor including the one allowed through everything else.

from topos.features.derivation.net_subject_policy import (  # noqa: E402
    may_owner_write_about,
    record_owner_decision,
)


@pytest.fixture()
def owned(conn):
    conn.execute("INSERT INTO entities VALUES ('ent_owner', 'Owner', NULL)")
    conn.execute("UPDATE entities SET canonical_name='Owner' WHERE entity_id='ent_owner'")
    try:
        conn.execute("ALTER TABLE entities ADD COLUMN is_self INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id='ent_owner'")
    conn.commit()
    return conn


def test_a_write_onto_the_owner_needs_no_gate(owned):
    d = may_owner_write_about(owned, "ent_owner")
    assert d.allowed and d.reason == "owner_subject"


def test_the_owner_may_write_onto_a_person_the_rule_would_refuse(owned):
    """The point of D-E: `ent_unnamed` is a bare phone number that gate 2 rejects, and the
    owner can still put a note on their card."""
    assert not may_write_about(owned, "ent_unnamed", pack_allows_net_subject=True).allowed
    d = may_owner_write_about(owned, "ent_unnamed")
    assert d.allowed and d.reason == "owner_directed"


def test_the_blackhole_still_stops_the_owner(owned):
    """The one gate the owner does not outrank. Otherwise the only thing between an
    exclusion and its reversal is the owner remembering they made it."""
    owned.execute("INSERT INTO entity_blackholes VALUES ('bh1', 'ent_nora', 'nora')")
    owned.commit()
    d = may_owner_write_about(owned, "ent_nora")
    assert not d.allowed and d.reason == "net_subject_blackholed"


def test_an_unreadable_blackhole_stops_the_owner_too(tmp_path):
    c = sqlite3.connect(str(tmp_path / "noBH.db"))
    _identities(c)
    d = may_owner_write_about(c, "ent_nora")
    assert not d.allowed and d.reason == "net_subject_blackhole_unreadable"
    c.close()


def test_an_empty_subject_is_refused(owned):
    assert not may_owner_write_about(owned, "").allowed


def test_the_decision_is_recorded_not_merely_implied(owned):
    """A consent decision that exists only as 'a fact was written' is not reviewable."""
    assert record_owner_decision(owned, "ent_unnamed", note="my landlord") is True
    assert subject_policy(owned, "ent_unnamed") == ALLOW


def test_recording_degrades_quietly_when_the_table_is_absent(bare):
    """The migration is not registered yet, and a promotion must not fail because the
    override table does not exist — but the caller has to be able to tell."""
    assert record_owner_decision(bare, "ent_nora") is False


# --- the opaque-id guard ---
#
# Found by a live re-measure the same day the nameability rule shipped: 257 of the 1,351
# subjects it authorised were bare UUIDs. Hex digits include a-f, so
# '187d819a-6f9f-4890-a380-099779a0ebef' satisfied "contains two consecutive letters" and
# sailed through — a dossier keyed to an opaque id, which is the exact outcome the rule
# exists to prevent. 1,351 -> 1,094 after the fix (72.7% of person entities).

@pytest.mark.parametrize("name", [
    "187d819a-6f9f-4890-a380-099779a0ebef",   # uuid
    "e73ff33ae330422b",                        # bare hex run
    "ent_e73ff33ae330422b",                    # schemed entity id
    "test-dataset:contact:8cee4315fdf697b61575",
    "deadbeefcafe",
])
def test_an_opaque_identifier_is_not_a_name(name):
    from topos.features.derivation.net_subject_policy import _looks_named

    assert not _looks_named(name)


@pytest.mark.parametrize("name", [
    "Tango Uniform", "Bob", "Anne-Marie O'Neill", "sarah@example.com",
    "Lima Mike", "田中太郎さん Tanaka",
])
def test_a_real_name_still_passes(name):
    """The guard must not buy precision by rejecting people. Hyphens, apostrophes,
    two-letter names and non-Latin scripts with a Latin transliteration all stay."""
    from topos.features.derivation.net_subject_policy import _looks_named

    assert _looks_named(name)


def test_an_opaque_subject_is_refused_end_to_end(conn):
    conn.execute("INSERT INTO entities VALUES ('ent_uuid',"
                 " '187d819a-6f9f-4890-a380-099779a0ebef', NULL)")
    conn.commit()
    assert not is_nameable_subject(conn, "ent_uuid")
    d = may_write_about(conn, "ent_uuid", pack_allows_net_subject=True)
    assert not d.allowed and d.reason == "net_subject_unnamed_subject"


def test_the_owner_can_still_override_an_opaque_subject(conn):
    """The rule is a default, not a wall — 'I know exactly which id this is' still works."""
    conn.execute("INSERT INTO entities VALUES ('ent_uuid2',"
                 " '61b5aa32-caca-4ba2-bba6-290e6c339960', NULL)")
    conn.commit()
    set_subject_policy(conn, "ent_uuid2", ALLOW, note="the shared calendar bot")
    assert may_write_about(conn, "ent_uuid2", pack_allows_net_subject=True).allowed
