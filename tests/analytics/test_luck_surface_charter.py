"""LSU-10 — the guardrails. Every test here is a defect that actually happened.

The Luck Surface screen makes claims about a person's life: what they have built, and who
never heard about it. Each of those claims has already been wrong once during development,
and each was wrong in a way that looked entirely plausible on screen. These tests are the
gates that keep the fixed versions fixed.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.analytics import luck_surface as L


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, is_self INTEGER);
      CREATE TABLE entity_mentions (mention_id TEXT PRIMARY KEY, entity_id TEXT,
        record_id TEXT, source_id TEXT, canonical_table TEXT, event_at TEXT,
        created_at TEXT, authored_by_owner INTEGER);
      CREATE TABLE user_goals (goal_id TEXT, goal_text TEXT);
      CREATE TABLE conversation_messages (dataset_id TEXT, message_id TEXT, content TEXT,
        is_from_self INTEGER, conversation_id TEXT, sender_id TEXT, event_at TEXT,
        source_id TEXT, reply_to_message_id TEXT);
    """)
    return conn


def _entity(conn, eid, etype, name, aliases="[]", is_self=0):
    conn.execute("INSERT INTO entities VALUES (?,?,?,?,?,?)",
                 (eid, etype, name, name.lower(), aliases, is_self))


def _mentions(conn, eid, source, n, when="2026-08-01T00:00:00Z"):
    for i in range(n):
        conn.execute("INSERT INTO entity_mentions VALUES (?,?,?,?,?,?,?,?)",
                     (f"m-{eid}-{source}-{i}", eid, f"rec-{eid}-{i}", source,
                      "activity_events", when, when, 1))


class TestWhatCountsAsWork:
    def test_place_and_person_are_never_work_items(self):
        """The owner journals FROM Northgate and ABOUT their friends, which gives both
        authored-work evidence. Listing a friend as a body of work with "told 1 person"
        is both wrong and unkind — it shipped once and must not again."""
        conn = _conn()
        _entity(conn, "e1", "place", "Northgate")
        _entity(conn, "e2", "person", "Mike Echo")
        _entity(conn, "e3", "project", "Topos")
        # github evidence for all three: the point is that TYPE excludes the place and the
        # person, not that they happened to lack authored-work evidence
        for eid in ("e1", "e2", "e3"):
            _mentions(conn, eid, "github_activity", 10)
        labels = {i["label"] for i in L.build_work_items(conn)}
        assert labels == {"Topos"}

    def test_reading_about_something_is_not_doing_it(self):
        """145 browser visits to the New York Times is not a body of work. The whole
        discriminator is that doing leaves a github/journal trace, not a visit."""
        conn = _conn()
        _entity(conn, "e1", "org", "The New York Times")
        _mentions(conn, "e1", "browser_visits", 145)
        _entity(conn, "e2", "org", "Dialogues")
        _mentions(conn, "e2", "github_activity", 20)
        assert [i["label"] for i in L.build_work_items(conn)] == ["Dialogues"]

    def test_one_body_of_work_split_across_entity_rows_merges(self):
        """Extraction emits Topos as BOTH a project and an org. Rendered unmerged, the
        screen showed the same work twice with contradictory telling counts."""
        conn = _conn()
        _entity(conn, "e1", "project", "Topos")
        _entity(conn, "e2", "org", "Topos")
        _mentions(conn, "e1", "github_activity", 30)
        _mentions(conn, "e2", "github_activity", 5)
        items = L.build_work_items(conn)
        assert len(items) == 1
        assert items[0]["doing_events"] == 35

    def test_label_is_the_most_mentioned_spelling(self):
        """"TOPOS" and "Topos" are separate rows; taking whichever sorted last SHOUTED
        the name on screen."""
        conn = _conn()
        _entity(conn, "e1", "project", "TOPOS")
        _entity(conn, "e2", "org", "Topos")
        _mentions(conn, "e1", "github_activity", 3)
        _mentions(conn, "e2", "github_activity", 40)
        assert L.build_work_items(conn)[0]["label"] == "Topos"


class TestAJournalIsARecordOfALifeNotOfWork:
    """The growth journal made "The Lantern Cafe" and "the Greenmart" into bodies of work
    the owner had supposedly built and told nobody about. Writing something down is not
    working on it."""

    def _goal(self, conn, text):
        conn.execute("INSERT INTO user_goals VALUES (?,?)", (text, text))

    def test_journal_only_entity_needs_the_owners_goals_to_qualify(self):
        conn = _conn()
        _entity(conn, "e1", "org", "The Lantern Cafe")
        _mentions(conn, "e1", "grow_journal", 6)
        assert L.build_work_items(conn) == []

    def test_journal_only_entity_qualifies_once_goals_keep_naming_it(self):
        """Learning Russian leaves no commits and is still real work."""
        conn = _conn()
        _entity(conn, "e1", "topic", "Russian")
        _mentions(conn, "e1", "grow_journal", 4)
        for i in range(L.MIN_GOALS_FOR_JOURNAL_WORK):
            self._goal(conn, f"Practise Russian conversation, session {i}")
        assert [i["label"] for i in L.build_work_items(conn)] == ["Russian"]

    def test_github_evidence_needs_no_corroboration(self):
        conn = _conn()
        _entity(conn, "e1", "project", "Topos")
        _mentions(conn, "e1", "github_activity", 5)
        assert [i["label"] for i in L.build_work_items(conn)] == ["Topos"]

    def test_goal_matching_is_word_bounded(self):
        """Substring matching let "first" collect 22 hits from "first draft" and "first
        pass", promoting an extraction truncation into a body of work."""
        conn = _conn()
        _entity(conn, "e1", "org", "Ryde")
        _mentions(conn, "e1", "grow_journal", 5)
        for i in range(6):
            self._goal(conn, f"Rydell High reunion planning step {i}")
        assert L.build_work_items(conn) == []

    def test_generic_words_are_never_work_items(self):
        conn = _conn()
        _entity(conn, "e1", "org", "First")
        _mentions(conn, "e1", "grow_journal", 5)
        for i in range(9):
            self._goal(conn, f"Ship the first pass of milestone {i}")
        assert L.build_work_items(conn) == []


class TestNeverLibelUnmeasuredWork:
    def test_unspeakable_names_are_not_reported_as_untold(self):
        """`dialoguesai/topos-react-app` carried 287 authored-work events and exactly one
        name — a repo slug nobody types into a message. Its telling count is structurally
        zero, and rendering that as "built for three months, told nobody" states a naming
        artifact as a fact about the owner's life."""
        conn = _conn()
        _entity(conn, "e1", "project", "dialoguesai/topos-react-app")
        _mentions(conn, "e1", "github_activity", 287)
        items = L.compile_surfaces(conn, "ds", L.build_work_items(conn))
        assert items[0]["matchable"] is False

    @pytest.mark.parametrize("surface,ok", [
        ("Topos", True), ("Topos Personal Node", True), ("Dialogues Research Institute", True),
        ("dialoguesai/topos-react-app", False), ("dev-ry", False), ("AI", False),
        ("Topos\n\nAccomplished", False),
    ])
    def test_speakability(self, surface, ok):
        assert L.is_speakable(surface) is ok

    def test_common_word_surfaces_are_dropped(self):
        """If extraction ever canonicalises a common word into an entity name, matching it
        would manufacture telling events out of ordinary chat."""
        conn = _conn()
        # not one of the GENERIC_ENTITY_NAMES — this guard is the separate one that fires on
        # how often a surface actually occurs in THIS owner's messages
        _entity(conn, "e1", "project", "Signal")
        _mentions(conn, "e1", "github_activity", 10)
        for i in range(10):
            conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
                         ("ds", f"m{i}", "lost signal on the train", 1, "c1", "self",
                          "2026-08-01T00:00:00Z", "imessage", None))
        items = L.compile_surfaces(conn, "ds", L.build_work_items(conn))
        assert items[0]["matchable"] is False


class TestTheControlStaysHonest:
    def test_control_is_deterministic(self):
        """A sampled control makes a stored row differ from a recomputed one, so the same
        screen disagrees with itself between loads."""
        comms = {f"p{i}": f"c{i % 7}" for i in range(40)}
        a = L.shuffle_control_breadth(["p1", "p2", "p3"], comms)
        b = L.shuffle_control_breadth(["p1", "p2", "p3"], comms)
        assert a == b

    def test_control_accounts_for_unequal_communities(self):
        """Drawing uniformly over COMMUNITIES rather than PEOPLE put the control above
        observed breadth for every item on the live corpus, declaring every real spread
        "chance". With one dominant community, three random people should usually land
        inside it — an expectation near 1, not near the community count."""
        comms = {f"p{i}": ("big" if i < 50 else f"tiny{i}") for i in range(55)}
        assert L.shuffle_control_breadth(["a", "b", "c"], comms) < 2.0

    def test_control_never_exceeds_the_number_told(self):
        n = 4
        comms = {f"p{i}": f"c{i}" for i in range(100)}
        assert L.shuffle_control_breadth([f"r{i}" for i in range(n)], comms) <= n

    def test_empty_inputs_do_not_raise(self):
        assert L.shuffle_control_breadth([], {}) == 0.0
        assert L.shuffle_control_breadth(["a"], {}) == 0.0


class TestNoScore:
    def test_rollup_returns_components_never_a_product(self):
        """The whole point of the design: the screen says "you built X for three months and
        told two people". It never says "your luck score is 74", because nobody can falsify
        a 74 and everyone would optimise it."""
        conn = _conn()
        _entity(conn, "e1", "project", "Topos")
        _mentions(conn, "e1", "github_activity", 10)
        out = L.rollup(conn, "ds")
        banned = {"score", "luck_score", "index", "rating", "grade"}
        for item in out["work_items"]:
            assert not (banned & set(item)), f"a score appeared in {sorted(item)}"

    def test_coverage_states_its_own_basis(self):
        """Every number on this screen is contestable, so the basis ships with it."""
        conn = _conn()
        _entity(conn, "e1", "project", "Topos")
        _mentions(conn, "e1", "github_activity", 10)
        cov = L.rollup(conn, "ds")["coverage"]
        for key in ("work_item_basis", "telling_basis", "resolver", "breadth_basis",
                    "control_basis"):
            assert cov.get(key), f"{key} missing from coverage"


class TestReadsNeverWrite:
    def test_rollup_runs_on_a_read_only_connection(self, tmp_path):
        """Reads that run DDL take SQLite's WRITE LOCK on every page load. This caught it
        once on the relationship reads; the same mistake here would be silent."""
        src = _conn()
        _entity(src, "e1", "project", "Topos")
        _mentions(src, "e1", "github_activity", 10)
        src.commit()  # an uncommitted write txn makes backup() block forever
        path = tmp_path / "ro.db"
        dest = sqlite3.connect(path)
        src.backup(dest)
        dest.commit()
        dest.close()
        src.close()
        ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        L.rollup(ro, "ds")  # must not raise "attempt to write a readonly database"
        ro.close()


class TestTheMoveRanker:
    """LSU-7/8. A suggestion about a person's relationships is only trustworthy if it can
    say where it came from and why it is ordered where it is."""

    def _corpus(self):
        conn = _conn()
        _entity(conn, "e1", "project", "Topos")
        _mentions(conn, "e1", "github_activity", 40)
        return conn

    def test_the_same_slider_position_gives_the_same_order(self):
        """LSU-8 requires determinism: a list that reshuffles between identical loads
        teaches the owner to distrust all of it."""
        conn = self._corpus()
        a = L.build_moves(conn, "ds", explore=0.7)
        b = L.build_moves(conn, "ds", explore=0.7)
        assert [m["title"] for m in a] == [m["title"] for m in b]

    def test_every_move_states_its_marginal_benefit_in_words(self):
        """Section 2: a move is never presented as valuable without saying what makes it so."""
        conn = self._corpus()
        for move in L.build_moves(conn, "ds", explore=0.5):
            assert move.get("marginal_benefit_words"), move["title"]
            assert move.get("why"), move["title"]
            assert move.get("evidence") is not None, move["title"]

    def test_the_slider_moves_the_two_kinds_in_opposite_directions(self):
        """Tested on the weights themselves rather than on a synthetic corpus: producing a
        reach move needs communities, directed edges and messages, and a fixture elaborate
        enough to make one would be testing the fixture."""
        assert L.reach_score(0.8, 1.0) > L.reach_score(0.8, 0.0)
        assert L.deepen_score(0.55, 0.0) > L.deepen_score(0.55, 1.0)
        # at full reach a deepening move must not outrank an equally-weighted reach move
        assert L.reach_score(0.8, 1.0) > L.deepen_score(0.8, 1.0)

    def test_reaching_a_new_circle_never_scores_zero(self):
        """Someone who mostly wants to deepen existing ties should still see reach moves,
        just not at the top."""
        assert L.reach_score(0.8, 0.0) > 0

    def test_it_never_puts_a_bare_phone_number_in_a_reconnect_suggestion(self):
        """"Reach out to +15125551234" reads as a machine talking about a stranger, and the
        naming move exists precisely so this one does not have to guess."""
        conn = self._corpus()
        for move in L.build_moves(conn, "ds", explore=0.5):
            if move["kind"] in ("reconnect_cooling_tie", "repay_imbalance"):
                assert any(ch.isalpha() for ch in move["title"].split("to ")[-1])

    def test_the_panel_is_capped(self):
        conn = self._corpus()
        assert len(L.build_moves(conn, "ds", explore=0.5)) <= L.MAX_MOVES

    def test_a_broken_rail_costs_its_moves_not_the_panel(self, monkeypatch):
        """The chart must survive a failing signals read; a suggestion panel is not worth
        taking the screen down for."""
        conn = self._corpus()
        import topos.analytics.relationship_reads as reads

        monkeypatch.setattr(reads, "read_relationship_signals",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        moves = L.build_moves(conn, "ds", explore=0.5)
        assert isinstance(moves, list)


class TestADatasetThatCannotAnswer:
    """Doing is read from entity_mentions, which is NOT dataset-scoped. Telling is read from
    this dataset's messages. Point the read at a dataset with no messaging substrate and
    every work item keeps its true doing count while telling collapses to zero — which the
    screen renders as "you built all this and told nobody". Measured on the live node
    2026-08-27: 1,609 doing / 0 telling from a one-message device stub."""

    def _corpus(self):
        conn = _conn()
        _entity(conn, "e1", "project", "Topos")
        _mentions(conn, "e1", "github_activity", 40)
        return conn

    def test_a_dataset_without_messages_reports_telling_as_unmeasurable(self):
        conn = self._corpus()
        out = L.rollup(conn, "dataset-with-no-messages")
        assert out["coverage"]["telling_measurable"] is False
        for w in out["work_items"]:
            assert w["matchable"] is False, "an unmeasurable dataset must not read as untold"
            assert w["below_telling_floor"] is False

    def test_doing_still_reports_truthfully(self):
        """The work is real even when telling cannot be read; blanking it would be its own lie."""
        conn = self._corpus()
        out = L.rollup(conn, "dataset-with-no-messages")
        assert sum(w["doing_events"] for w in out["work_items"]) == 40

    def test_a_dataset_with_messages_is_measurable(self):
        conn = self._corpus()
        conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
                     ("ds", "m1", "shipping Topos today", 1, "c1", "self",
                      "2026-08-01T00:00:00Z", "imessage", None))
        assert L.rollup(conn, "ds")["coverage"]["telling_measurable"] is True


class TestANodeThatCannotNameItsDataset:
    """`/v1/ingestion/datasets` returns ZERO rows on a node whose messages arrived by sync
    rather than upload — measured on the live node 2026-08-27. The client had nothing to
    resolve from, so a database holding 7,668 messages rendered as an empty screen. The
    engine is one GROUP BY away from the answer."""

    def _corpus(self, dataset_id="ds-real", n=3):
        conn = _conn()
        _entity(conn, "e1", "project", "Topos")
        _mentions(conn, "e1", "github_activity", 40)
        for i in range(n):
            conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
                         (dataset_id, f"m{i}", "shipping Topos today", 1, "c1", "self",
                          "2026-08-01T00:00:00Z", "imessage", None))
        return conn

    def test_an_empty_dataset_id_resolves_to_the_busiest_one(self):
        conn = self._corpus()
        out = L.rollup(conn, "")
        assert out["dataset_id"] == "ds-real"
        assert out["coverage"]["telling_measurable"] is True

    def test_the_substitution_is_never_silent(self):
        """The screen has to be able to say which dataset it read."""
        conn = self._corpus()
        assert L.rollup(conn, "")["coverage"]["dataset_resolved_by_engine"] is True
        assert L.rollup(conn, "ds-real")["coverage"]["dataset_resolved_by_engine"] is False

    def test_it_picks_the_busiest_not_the_first(self):
        conn = self._corpus(dataset_id="ds-real", n=5)
        conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
                     ("user:default:device", "stub", "hi", 1, "c9", "self",
                      "2026-08-01T00:00:00Z", "imessage", None))
        assert L.rollup(conn, "")["dataset_id"] == "ds-real"

    def test_no_messages_at_all_still_answers(self):
        conn = _conn()
        _entity(conn, "e1", "project", "Topos")
        _mentions(conn, "e1", "github_activity", 40)
        out = L.rollup(conn, "")
        assert out["coverage"]["telling_measurable"] is False
        assert sum(w["doing_events"] for w in out["work_items"]) == 40


class TestTheJournalIsAFirstClassDoingSource:
    """The growth journal is in WORK_SOURCES and counts toward doing. The goals gate exists
    ONLY to keep places of daily life out — measured on the live corpus, a threshold of 3
    also dropped Mursion, TinyCloud and Yale, which are real."""

    def _journal_entity(self, conn, name, events, goal_hits):
        _entity(conn, f"e-{name}", "org", name)
        _mentions(conn, f"e-{name}", "grow_journal", events)
        for i in range(goal_hits):
            conn.execute("INSERT INTO user_goals VALUES (?,?)",
                         (f"g{name}{i}", f"Follow up with {name} about the pilot {i}"))

    def test_journal_events_count_toward_doing(self):
        conn = _conn()
        _entity(conn, "e1", "project", "Topos")
        _mentions(conn, "e1", "github_activity", 10)
        _mentions(conn, "e1", "grow_journal", 7)
        item = L.build_work_items(conn)[0]
        assert item["doing_events"] == 17
        assert item["doing_by_source"]["grow_journal"] == 7

    def test_a_journal_only_body_of_work_qualifies_on_two_goal_mentions(self):
        conn = _conn()
        self._journal_entity(conn, "Mursion", 4, L.MIN_GOALS_FOR_JOURNAL_WORK)
        assert [i["label"] for i in L.build_work_items(conn)] == ["Mursion"]

    def test_places_of_daily_life_still_do_not_qualify(self):
        conn = _conn()
        self._journal_entity(conn, "the Greenmart", 6, 0)
        self._journal_entity(conn, "Metro Fitness", 3, 0)
        assert L.build_work_items(conn) == []


class TestAWrongDatasetIsNotAnAnswer:
    """A caller naming a dataset with no messages got every body of work back with
    "who has heard is unknown" — true of the id, useless to the person, and the node held
    the messages under another dataset the whole time."""

    def _corpus(self):
        conn = _conn()
        _entity(conn, "e1", "project", "Topos")
        _mentions(conn, "e1", "github_activity", 40)
        for i in range(4):
            conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
                         ("ds-real", f"m{i}", "shipping Topos today", 1, "c1", "self",
                          "2026-08-01T00:00:00Z", "imessage", None))
        return conn

    def test_a_dataset_without_messages_falls_back_to_the_one_with_them(self):
        conn = self._corpus()
        out = L.rollup(conn, "user:default:device")
        assert out["dataset_id"] == "ds-real"
        assert out["coverage"]["telling_measurable"] is True

    def test_the_fallback_reports_both_ids(self):
        conn = self._corpus()
        cov = L.rollup(conn, "user:default:device")["coverage"]
        assert cov["dataset_resolved_by_engine"] is True
        assert cov["dataset_requested"] == "user:default:device"

    def test_a_good_dataset_is_left_alone(self):
        conn = self._corpus()
        out = L.rollup(conn, "ds-real")
        assert out["dataset_id"] == "ds-real"
        assert out["coverage"]["dataset_resolved_by_engine"] is False
