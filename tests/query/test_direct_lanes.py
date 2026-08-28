"""The direct-answer lanes: what fires, what refuses, and who is allowed a name.

These lanes short-circuit the LLM with deterministic answers. Two properties matter more
than any individual answer:

  * a lane NAMES PEOPLE, and the only thing standing between a non-owner and those names
    is the `packet_resolution` gate — scope is not checked in the dispatch at all, so
    every lane carrying names must be gated the same way;
  * lanes are tried in order and the first non-None wins, so two lanes that both claim a
    phrasing make the answer depend on dispatch order rather than on the question.
"""

from __future__ import annotations

import inspect
import re
import sqlite3

import pytest

from topos.query.closeness import matches_closeness
from topos.query.collaborators import (
    compose_collaborators_answer,
    matches_collaborators,
    try_collaborators,
)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


class TestEveryNamingLaneIsGatedTheSameWay:
    """The caveat this suite exists for: adding lanes concentrates weight on one gate."""

    def test_the_dispatch_checks_no_scope(self):
        """Recorded, not asserted as good: names are protected by packet_resolution and
        NOT by scope, so a reader of this file learns where to look."""
        from topos.query import pipeline

        source = inspect.getsource(pipeline)
        block = source[source.index("try_facts_direct"):source.index("if direct is not None")]
        assert "scope_id" not in block

    @pytest.mark.parametrize("module_name", ["closeness", "collaborators", "facts_direct"])
    def test_each_lane_refuses_a_non_owner(self, module_name):
        """A non-owner floors to scores_only upstream; every lane must honour it."""
        import importlib

        module = importlib.import_module(f"topos.query.{module_name}")
        entry = next(fn for name, fn in vars(module).items()
                     if name.startswith("try_") and callable(fn))
        assert "packet_resolution" in inspect.signature(entry).parameters, \
            f"{module_name}'s entry point cannot see who is asking"
        # Read the MODULE, not the entry function: `facts_direct` gates one call deeper,
        # and the property that matters is that the refusal is on the path at all.
        assert re.search(r'packet_resolution not in \("facts", "facts_all"\)',
                         inspect.getsource(module)), \
            f"{module_name} does not floor a non-owner"


class TestTheLanesDoNotCompeteForAPhrasing:
    @pytest.mark.parametrize("query", [
        "who is in my close circle", "who are my closest friends",
        "who do i talk to most", "who's in my inner circle",
    ])
    def test_closeness_phrasings_stay_with_closeness(self, query):
        assert matches_closeness(query)
        assert not matches_collaborators(query), \
            "the work lane must not claim an interaction question"

    @pytest.mark.parametrize("query", [
        "who works on this with me", "who do I collaborate with",
        "who are my coworkers", "who are my teammates",
        "who should I ask about the engine relay",
        "who else is working on the signal graph",
    ])
    def test_work_phrasings_stay_with_the_work_lane(self, query):
        assert matches_collaborators(query)
        assert not matches_closeness(query), \
            "closeness must not claim a work question"

    @pytest.mark.parametrize("query", ["what am I working on", "how did I sleep"])
    def test_neither_claims_an_unrelated_question(self, query):
        assert not matches_collaborators(query)
        assert not matches_closeness(query)


class TestTheCollaboratorLaneSaysTheWeakerThingItMeans:
    def test_a_non_owner_gets_nothing(self, conn):
        assert try_collaborators(conn, "who works on this with me",
                                 packet_resolution="scores_only") is None

    def test_an_unrunnable_lane_falls_through_rather_than_asserting_nobody(self, conn):
        """An empty database is not evidence that nobody works with the owner."""
        assert try_collaborators(conn, "who works on this with me",
                                 packet_resolution="facts") is None

    def test_the_basis_string_refuses_the_capability_claim(self):
        from topos.query.collaborators import try_collaborators as fn

        source = inspect.getsource(fn)
        assert "not people evidenced to be able to do it" in source, \
            "engagement and capability are different claims and the payload must say so"

    def test_an_unmeasured_closeness_is_not_printed_as_zero(self):
        answer = compose_collaborators_answer([
            {"name": "Ada", "closeness": None, "messages_considered": 12},
        ])
        assert "closeness unmeasured" in answer
        assert "0.00" not in answer
