"""Shared context as a second opinion about who belongs with whom.

The clustering ran on one layer — who messages whom — so two people with everything
in common and no messages between them were never grouped. This adds a second layer
that is never drawn, never counted as a tie, and never allowed near centrality.

The three things measured on the live node that this pins:
  · the layers are NOT comparable as stored (rank normalisation)
  · a shared subject must not make anyone a broker (centrality stays on layer 1)
  · a project the owner DECLARED is worth more than a subject a model inferred
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.analytics import person_graph as PG


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "ctx.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _nodes(*ids):
    return [
        {"node_id": i, "entity_id": f"e-{i}", "label": i.title(), "is_owner": False,
         "band": PG.BAND_CORE, "evidence": {"messaged": True, "mentioned": False},
         "messenger_keys": [], "message_count": 5, "mention_count": 0}
        for i in ids
    ]


def _edge(conn, a, b, w):
    conn.execute(
        "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type,"
        " weight, evidence_count) VALUES (?,?,?,?,?,?)",
        (f"x-{a}-{b}", f"e-{a}", f"e-{b}", "communicates_with", float(w), int(w)),
    )


class TestRankNormalisation:
    def test_it_replaces_weights_with_their_order(self):
        out = PG._rank_normalised([("a", "b", 2772.0), ("c", "d", 3.0), ("e", "f", 1.0)])
        assert out[("e", "f")] == pytest.approx(1 / 3)
        assert out[("c", "d")] == pytest.approx(2 / 3)
        assert out[("a", "b")] == pytest.approx(1.0)

    def test_it_is_what_makes_the_two_layers_addable(self):
        """THE BUG this exists for. Message counts reach 2772 and a cosine reaches 1, so
        adding them raw is the message count with rounding error — sweeping the layer
        weight from 1.0 to 0.1 on the live node produced byte-identical clusters."""
        heavy = PG._rank_normalised([("a", "b", 2772.0), ("c", "d", 3.0)])
        light = PG._rank_normalised([("a", "b", 0.9), ("c", "d", 0.03)])
        assert set(heavy.values()) == set(light.values())


class TestTheSecondLayerIsNotATie:
    def test_a_shared_subject_does_not_make_someone_a_broker(self, conn):
        """Centrality answers "who connects parts of my world". A shared subject connects
        nobody, and counting it would invent a broker out of common interests."""
        for a, b in (("x", "y"), ("p", "q")):
            _edge(conn, a, b, 10)
        conn.commit()
        nodes = _nodes("x", "y", "p", "q")

        joined = PG.structural_metrics(
            conn, "d", nodes,
            context_pairs=[{"source": "y", "target": "p", "weight": 0.9}],
        )
        alone = PG.structural_metrics(conn, "d", nodes, context_pairs=[])

        assert joined["betweenness"] == alone["betweenness"], (
            "layer 2 must not move betweenness"
        )
        assert joined["degree"] == alone["degree"], "layer 2 must not move degree"

    def test_it_places_someone_the_messaging_record_could_not(self, conn):
        """The measured win. On the live node this took the number of people with a
        community from 92 to 115: someone the owner knows offline has no messaging edge,
        so the one-layer clustering had nowhere to put them at all."""
        for a, b in (("x", "y"), ("y", "z"), ("x", "z")):
            _edge(conn, a, b, 10)
        conn.commit()
        nodes = _nodes("x", "y", "z", "offline")

        alone = PG.structural_metrics(conn, "d", nodes, context_pairs=[])
        joined = PG.structural_metrics(
            conn, "d", nodes,
            context_pairs=[{"source": "offline", "target": "x", "weight": 0.9},
                           {"source": "offline", "target": "y", "weight": 0.8}],
        )

        assert "offline" not in alone["communities"], "no edge, nowhere to put them"
        assert joined["communities"].get("offline") == joined["communities"].get("x")
        assert joined["coverage"]["context_pairs_joined"] == 2

    def test_one_weak_subject_does_not_dissolve_two_real_groups(self, conn):
        """The other direction, and the reason the weight is a weight and not an override:
        two well-connected groups sharing one subject stay two groups."""
        for a, b in (("x", "y"), ("y", "z"), ("x", "z"),
                     ("p", "q"), ("q", "r"), ("p", "r")):
            _edge(conn, a, b, 10)
        conn.commit()
        nodes = _nodes("x", "y", "z", "p", "q", "r")

        joined = PG.structural_metrics(
            conn, "d", nodes,
            context_pairs=[{"source": "z", "target": "p", "weight": 0.9}],
        )

        assert joined["communities"]["z"] != joined["communities"]["p"]

    def test_layer_two_may_only_ADD_never_subtract(self, conn):
        """A second signal can pull a person IN; its absence is silence, not evidence of
        distance — the same rule `attach_fact_closeness` states for a stated fact.

        THE REGRESSION this closes: measured on the live node, one person sat in a
        ten-strong group on the messaging graph alone, and adding their shared subjects
        moved them into a sub-threshold group that was then dropped. They lost their
        placement entirely while the group they belonged to kept theirs — one of 115, and
        that group's highest-volume member.
        """
        # A five-person group, plus a far-away pair the loner also shares a subject with.
        for a, b in (("m1", "m2"), ("m2", "m3"), ("m3", "m4"), ("m4", "m5"),
                     ("m5", "m1"), ("m1", "m3")):
            _edge(conn, a, b, 20)
        conn.commit()
        nodes = _nodes("m1", "m2", "m3", "m4", "m5")

        alone = PG.structural_metrics(conn, "d", nodes, context_pairs=[])
        assert alone["communities"].get("m1"), "fixture must place them without layer 2"

        # A subject pulling m1 toward nobody in particular must not orphan them.
        joined = PG.structural_metrics(
            conn, "d", nodes,
            context_pairs=[{"source": "m1", "target": "m4", "weight": 0.95},
                           {"source": "m1", "target": "m5", "weight": 0.95}],
        )

        assert joined["communities"].get("m1"), (
            "a person the tie graph alone would place must never LOSE their placement "
            "because a shared subject was added"
        )
        assert set(joined["communities"]) >= set(alone["communities"]), (
            "layer 2 must not shrink the set of placed people"
        )

    def test_a_pair_naming_someone_off_the_graph_is_ignored(self, conn):
        _edge(conn, "x", "y", 10)
        conn.commit()
        out = PG.structural_metrics(
            conn, "d", _nodes("x", "y"),
            context_pairs=[{"source": "x", "target": "stranger", "weight": 0.9}],
        )
        assert out["coverage"]["context_pairs_joined"] == 0

    def test_the_read_says_which_graph_each_number_came_from(self, conn):
        _edge(conn, "x", "y", 10)
        conn.commit()
        out = PG.structural_metrics(conn, "d", _nodes("x", "y"), context_pairs=[])
        assert out["coverage"]["centrality_from"] == "who you know only"
        assert "never drawn" in out["coverage"]["communities_from"]


class TestCommunityIdsStayPut:
    """The social graph paints `community_id % palette`. If the rank twitches
    on the next load of the same edges, people change colour."""

    def test_equal_size_groups_keep_the_same_id_when_listed_backwards(self):
        a = [{"ann", "ben", "cam"}, {"dot", "ed", "fay"}]
        b = [{"fay", "ed", "dot"}, {"cam", "ben", "ann"}]
        first, kept_a = PG._community_ids_by_size(a)
        second, kept_b = PG._community_ids_by_size(b)
        assert kept_a == kept_b == 2
        assert first == second
        assert first["ann"] == 1  # same size; "ann" < "dot"
        assert first["dot"] == 2

    def test_edge_insert_order_does_not_reassign_anyone(self, conn):
        people = ("ann", "ben", "cam", "dot", "ed", "fay")
        cliques = (("ann", "ben", "cam"), ("dot", "ed", "fay"))

        def clique_edges(clique):
            return [(clique[i], clique[j])
                    for i in range(len(clique))
                    for j in range(i + 1, len(clique))]

        forward = [edge for clique in cliques for edge in clique_edges(clique)]
        reverse = list(reversed(forward))

        def run(order):
            conn.execute("DELETE FROM entity_edges")
            for src, dst in order:
                _edge(conn, src, dst, 5)
            conn.commit()
            return PG.structural_metrics(
                conn, "d", _nodes(*people), context_pairs=[]
            )["communities"]

        assert run(forward) == run(reverse)


class TestDeclaredWorkOutranksAnInferredSubject:
    def _seed(self, conn, rows):
        for i, (person, subject, kind, conf) in enumerate(rows):
            for eid, name, etype in ((f"p-{person}", person, "person"),
                                     (f"s-{subject}", subject, kind)):
                conn.execute(
                    "INSERT OR IGNORE INTO entities (entity_id, entity_type, canonical_name,"
                    " normalized_name) VALUES (?,?,?,?)", (eid, etype, name, name.lower()))
            rec = f"r-{person}-{subject}"
            for eid, c in ((f"p-{person}", conf), (f"s-{subject}", 1.0)):
                conn.execute(
                    "INSERT INTO entity_mentions (mention_id, entity_id, record_id,"
                    " source_id, canonical_table, confidence) VALUES (?,?,?,?,?,?)",
                    (f"m{i}-{eid}", eid, rec, "grow_journal", "journal_entries", c))
        conn.commit()

    def test_a_declared_project_reaches_the_substrate(self, conn):
        self._seed(conn, [("Ada", "Helios", "project", 1.0),
                          ("Bo", "Helios", "project", 1.0)])
        people = {n["node_id"]: n for n in _nodes("ada", "bo")}
        people["ada"]["entity_id"] = "p-Ada"
        people["bo"]["entity_id"] = "p-Bo"

        members, declared_work = PG._journal_context_members(conn, people)

        assert "helio" in members or "helios" in members
        key = next(k for k in members if k.startswith("helio"))
        assert members[key] == {"ada", "bo"}
        assert key in declared_work, "a project is declared WORK, and may stand alone"

    def test_a_name_the_model_found_in_prose_is_not_a_participant(self, conn):
        """THE LOOSENESS this closes. Taking any co-mention put 29 people in the owner's
        own project — an AI assistant, a novelist, a grandparent, and the project entity
        itself — because prose naming a project while somebody was in the room counted."""
        self._seed(conn, [("Ada", "Helios", "project", 1.0),
                          ("Guessed", "Helios", "project", 0.87)])
        people = {n["node_id"]: n for n in _nodes("ada", "guessed")}
        people["ada"]["entity_id"] = "p-Ada"
        people["guessed"]["entity_id"] = "p-Guessed"

        members, _ = PG._journal_context_members(conn, people)
        key = next(k for k in members if k.startswith("helio"))
        assert members[key] == {"ada"}, "only the declared participant counts"

    def test_a_place_is_not_declared_work(self, conn):
        self._seed(conn, [("Ada", "Bruges", "place", 1.0),
                          ("Bo", "Bruges", "place", 1.0)])
        people = {n["node_id"]: n for n in _nodes("ada", "bo")}
        people["ada"]["entity_id"] = "p-Ada"
        people["bo"]["entity_id"] = "p-Bo"

        members, declared_work = PG._journal_context_members(conn, people)
        key = next(k for k in members if k.startswith("bruge"))
        assert members[key] == {"ada", "bo"}
        assert key not in declared_work, (
            "two people at the same venue is not two people working together"
        )
