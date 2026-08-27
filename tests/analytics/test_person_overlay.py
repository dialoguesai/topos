"""Curation is an OVERLAY. Nothing here may edit `entities` or `entity_mentions`.

Three reasons, and the third decides it: re-derivation would wipe the owner's work; undo is
free when nothing was destroyed; and `merge_entities` on this codebase is not reliably
reversible. The owner WILL merge two people wrongly — `Bravo Yankee` and `Charlie Yankee` are
both real on this node — and that must not be a one-way door.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.analytics import person_overlay as OV


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    OV.create_overlay_table(c)
    return c


def _node(node_id, label="Someone", **kw):
    base = {"node_id": node_id, "label": label, "entity_id": None, "messenger_keys": [],
            "contact_id": None, "evidence": {"messaged": False, "mentioned": True},
            "is_owner": False, "message_count": 0, "mention_count": 1, "needs_name": False,
            "band": "ambient", "band_reason": "seen once in passing"}
    base.update(kw)
    return base


class TestNothingIsDestroyed:
    def test_undo_revokes_rather_than_deletes(self, conn):
        """'What did I already decide about this person, and when' is worth keeping, and a
        deleted decision cannot be re-examined."""
        row = OV.record(conn, "ds", "n1", OV.ACTION_DISMISS)
        assert OV.revoke(conn, row["overlay_id"]) is True
        assert OV.load(conn, "ds") == []
        assert len(OV.load(conn, "ds", include_revoked=True)) == 1

    def test_revoking_twice_is_not_an_error_but_changes_nothing(self, conn):
        row = OV.record(conn, "ds", "n1", OV.ACTION_DISMISS)
        assert OV.revoke(conn, row["overlay_id"]) is True
        assert OV.revoke(conn, row["overlay_id"]) is False

    def test_a_second_band_replaces_the_first_but_keeps_its_history(self, conn):
        OV.record(conn, "ds", "n1", OV.ACTION_BAND, "named")
        OV.record(conn, "ds", "n1", OV.ACTION_BAND, "core")
        live = OV.load(conn, "ds")
        assert [r["value"] for r in live] == ["core"]
        assert len(OV.load(conn, "ds", include_revoked=True)) == 2

    def test_notes_accumulate_rather_than_replace(self, conn):
        OV.record(conn, "ds", "n1", OV.ACTION_NOTE, "met at YC")
        OV.record(conn, "ds", "n1", OV.ACTION_NOTE, "intro'd me to Dana")
        assert len(OV.load(conn, "ds")) == 2

    def test_an_unknown_action_is_refused(self, conn):
        with pytest.raises(ValueError):
            OV.record(conn, "ds", "n1", "delete_forever")


class TestDismissIsNotDelete:
    def test_a_dismissed_person_is_flagged_not_removed(self):
        nodes = [_node("n1")]
        out = OV.apply_overlay(nodes, [{"overlay_id": "o1", "subject_id": "n1",
                                        "action": OV.ACTION_DISMISS, "value": None,
                                        "created_at": "t"}])
        assert len(out) == 1 and out[0]["dismissed"] is True

    def test_the_dismissal_carries_its_undo_handle(self):
        out = OV.apply_overlay([_node("n1")], [{"overlay_id": "o9", "subject_id": "n1",
                                                "action": OV.ACTION_DISMISS, "value": None,
                                                "created_at": "t"}])
        assert out[0]["dismissed_by"] == "o9"


class TestMerge:
    def _merge(self, a, b, **kw):
        return OV.apply_overlay([a, b], [{"overlay_id": "o1", "subject_id": b["node_id"],
                                          "action": OV.ACTION_MERGE, "value": a["node_id"],
                                          "created_at": "t"}])

    def test_the_survivor_absorbs_identities_and_traffic(self):
        a = _node("keep", "Dasha", messenger_keys=["+1555"], message_count=606,
                  mention_count=0, evidence={"messaged": True, "mentioned": False})
        b = _node("gone", "Dasha", entity_id="ent_x", mention_count=4)
        out = self._merge(a, b)
        assert len(out) == 1
        assert out[0]["message_count"] == 606 and out[0]["mention_count"] == 4
        assert out[0]["entity_id"] == "ent_x"
        assert out[0]["evidence"] == {"messaged": True, "mentioned": True}

    def test_the_merge_is_recorded_on_the_survivor(self):
        out = self._merge(_node("keep", "Dasha"), _node("gone", "Dasha"))
        assert out[0]["merged_from"][0]["node_id"] == "gone"

    def test_a_name_survives_over_a_phone_number(self):
        a = _node("keep", "+15551234567", needs_name=True)
        b = _node("gone", "Dasha")
        out = self._merge(a, b)
        assert out[0]["label"] == "Dasha" and out[0]["needs_name"] is False

    def test_chains_resolve_to_the_final_survivor(self):
        assert OV.resolve_merges({"a": "b", "b": "c"}) == {"a": "c", "b": "c"}

    def test_a_cycle_does_not_hang(self):
        """The owner can make one in two clicks; a page load must not spin on it."""
        out = OV.resolve_merges({"a": "b", "b": "a"})
        assert set(out) == {"a", "b"}


class TestBulk:
    def test_one_decision_over_many_people_returns_one_undo_set(self, conn):
        """152 ambient one-offs is not a per-item job, and a bulk action the owner cannot
        take back in one move is a trap."""
        rows = OV.record_many(conn, "ds", [f"n{i}" for i in range(5)], OV.ACTION_DISMISS)
        assert len(rows) == 5
        assert OV.revoke_many(conn, [r["overlay_id"] for r in rows]) == 5
        assert OV.load(conn, "ds") == []

    def test_history_shows_live_and_revoked_newest_first(self, conn):
        a = OV.record(conn, "ds", "n1", OV.ACTION_NOTE, "one")
        OV.revoke(conn, a["overlay_id"])
        OV.record(conn, "ds", "n1", OV.ACTION_NOTE, "two")
        hist = OV.history(conn, "ds")
        assert len(hist) == 2 and any(h["revoked_at"] for h in hist)


class TestReadsNeverWrite:
    def test_load_on_a_missing_table_returns_empty_rather_than_raising(self):
        assert OV.load(sqlite3.connect(":memory:"), "ds") == []

    def test_load_never_creates_the_table(self):
        c = sqlite3.connect(":memory:")
        OV.load(c, "ds")
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name=?",
                             (OV.OVERLAY_TABLE,)).fetchone()
