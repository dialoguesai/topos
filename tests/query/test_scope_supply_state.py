"""Why a scope holds nothing — three cases behind one identical answer.

`store_empty` already separates "nothing has ever been stored" from "you had a quiet
week". It does not say which of three things went wrong, and the remedies are different:
connect a source, wait for a first sync, or find out what emptied the tables. Only one of
those is the owner's to act on, and today's answer sends them to the wrong one.

Doc gap G4 / query Q6 ("Why is my calendar empty — is that real?"). Measured on the live
node: `calendar_events` and `financial_transactions` both hold 0 rows, and their empty
result is worded identically to a genuinely quiet week.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.manifest import ScopeResolutionManifest
from topos.query.retrieval import (
    SUPPLY_DELIVERED_THEN_EMPTIED,
    SUPPLY_NEVER_DELIVERED,
    SUPPLY_NO_SOURCE,
    _scope_supply_state,
    routing_supply_states,
)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE scope_source_generation ("
        " scope_id TEXT, source_id TEXT, generation INTEGER, updated_at TEXT)"
    )
    yield c
    c.close()


def manifest(scope_id="schedule:read", sources=()):
    return ScopeResolutionManifest(
        scope_id=scope_id,
        primary_dimensions=["time"],
        default_source_ids=list(sources),
    )


@pytest.fixture(autouse=True)
def _feeds(monkeypatch, request):
    """Which sources feed a scope comes from the registry, not the manifest.

    Pinned per-test so these assertions describe the probe rather than whatever this
    developer machine happens to have installed. Using `default_source_ids` here was a
    real bug: it reported "no source connected" for a scope whose calendar connector was
    installed, which sends the owner to add a connector they already have.
    """
    feeds = getattr(request, "param", None)
    import topos.sources.registry as reg

    monkeypatch.setattr(
        reg, "get_sources_by_scope",
        lambda scope_id: list(feeds) if feeds is not None else ["gcal_events"],
    )


class TestTheThreeCases:
    def test_data_landed_once_means_something_emptied_it(self, conn) -> None:
        """Never tell an owner to reconnect a feed that is working."""
        conn.execute(
            "INSERT INTO scope_source_generation VALUES ('schedule:read','gcal_events',12,'now')"
        )
        state = _scope_supply_state(conn, manifest(sources=["gcal_events"]), ["gcal_events"])
        assert state == SUPPLY_DELIVERED_THEN_EMPTIED

    def test_a_feed_is_installed_but_has_never_landed(self, conn) -> None:
        state = _scope_supply_state(conn, manifest(sources=["gcal_events"]), ["gcal_events"])
        assert state == SUPPLY_NEVER_DELIVERED

    def test_nothing_installed_feeds_this_scope(self, conn) -> None:
        """The live calendar case: connect Google Calendar, don't doubt the week."""
        state = _scope_supply_state(conn, manifest(sources=["gcal_events"]), ["grow_journal"])
        assert state == SUPPLY_NO_SOURCE

    def test_a_scope_that_declares_no_feeds_at_all(self, conn) -> None:
        assert _scope_supply_state(conn, manifest(sources=[]), ["grow_journal"]) == SUPPLY_NO_SOURCE

    def test_generation_zero_is_not_a_delivery(self, conn) -> None:
        """A registered pairing that never produced a generation has not delivered."""
        conn.execute(
            "INSERT INTO scope_source_generation VALUES ('schedule:read','gcal_events',0,'now')"
        )
        state = _scope_supply_state(conn, manifest(sources=["gcal_events"]), ["gcal_events"])
        assert state == SUPPLY_NEVER_DELIVERED


class TestUnknownStaysUnknown:
    """An unknown must not be dressed up as a diagnosis — a wrong remedy is worse than
    none, because the owner acts on it."""

    def test_no_connection(self) -> None:
        assert _scope_supply_state(None, manifest(sources=["gcal_events"]), ["gcal_events"]) is None

    def test_no_scope_id(self, conn) -> None:
        assert _scope_supply_state(conn, manifest(scope_id="", sources=["x"]), ["x"]) is None

    def test_missing_table_on_an_older_database(self) -> None:
        bare = sqlite3.connect(":memory:")
        try:
            assert _scope_supply_state(bare, manifest(sources=["gcal_events"]), ["gcal"]) is None
        finally:
            bare.close()

    def test_declared_feeds_but_installed_set_unknown(self, conn) -> None:
        """Claiming "nothing connected" when we simply were not told what is installed
        would be a guess with a confident face."""
        assert _scope_supply_state(conn, manifest(sources=["gcal_events"]), None) is None
        assert _scope_supply_state(conn, manifest(sources=["gcal_events"]), []) is None


class TestItNeverRaises:
    def test_a_hostile_connection_returns_none(self) -> None:
        class Boom:
            def execute(self, *a, **k):
                raise RuntimeError("db is on fire")

        assert _scope_supply_state(Boom(), manifest(sources=["x"]), ["x"]) is None


class TestTheRoutingHalf:
    """`routing_supply_states` answers the same question BEFORE a query is spent.

    The explanation half tells an owner why a scope came back empty. The routing half
    has to be usable one step earlier — while the router is choosing which four scopes
    to spend its slots on — so it reads the registry and the installed set only, with
    no connection and no data read.
    """

    def _feeds_map(self, monkeypatch, mapping):
        import topos.sources.registry as reg

        monkeypatch.setattr(
            reg, "get_sources_by_scope", lambda scope_id: list(mapping.get(scope_id, []))
        )

    def test_a_scope_no_installed_source_feeds_is_reported(self, monkeypatch) -> None:
        self._feeds_map(monkeypatch, {"schedule:read": ["gcal_events"]})
        states = routing_supply_states(["grow_journal"], ["schedule:read"])
        assert states == {"schedule:read": SUPPLY_NO_SOURCE}

    def test_a_scope_with_an_installed_feed_is_absent(self, monkeypatch) -> None:
        """Connected-but-empty is a real empty answer and must still be queried."""
        self._feeds_map(monkeypatch, {"schedule:read": ["gcal_events"]})
        assert routing_supply_states(["gcal_events"], ["schedule:read"]) == {}

    def test_only_no_source_connected_is_ever_reported(self, monkeypatch) -> None:
        """The other two sub-causes describe connected feeds. A router that could see
        them would drop scopes that hold a genuine empty answer."""
        self._feeds_map(monkeypatch, {"schedule:read": ["gcal_events"]})
        states = routing_supply_states(["gcal_events", "grow_journal"], ["schedule:read"])
        assert SUPPLY_NEVER_DELIVERED not in states.values()
        assert SUPPLY_DELIVERED_THEN_EMPTIED not in states.values()

    def test_a_scope_with_no_registered_feeds_is_left_out(self, monkeypatch) -> None:
        """`attention:read` and `complexity:read` are computed on-node and register no
        feeding source. "No feeds" there means unknowable, and an unknown must never
        cost a route — the explanation half may call it no_source_connected because it
        only speaks after emptiness is established."""
        self._feeds_map(monkeypatch, {"attention:read": []})
        assert routing_supply_states(["grow_journal"], ["attention:read"]) == {}

    def test_an_unknown_installed_set_reports_nothing(self, monkeypatch) -> None:
        self._feeds_map(monkeypatch, {"schedule:read": ["gcal_events"]})
        assert routing_supply_states(None, ["schedule:read"]) == {}
        assert routing_supply_states([], ["schedule:read"]) == {}

    def test_it_never_raises(self, monkeypatch) -> None:
        import topos.sources.registry as reg

        def boom(_scope_id):
            raise RuntimeError("registry is on fire")

        monkeypatch.setattr(reg, "get_sources_by_scope", boom)
        assert routing_supply_states(["grow_journal"], ["schedule:read"]) == {}

    def test_it_defaults_to_the_whole_scope_registry(self, monkeypatch) -> None:
        """Absent an explicit list the router gets every scope it could route to —
        anything it cannot see, it cannot skip."""
        from topos.query.scope_registry_loader import list_scopes

        import topos.sources.registry as reg

        monkeypatch.setattr(reg, "get_sources_by_scope", lambda _s: ["gcal_events"])
        states = routing_supply_states(["grow_journal"])
        assert {e["scope_id"] for e in list_scopes()} == set(states)


class TestTheListingCarriesIt:
    """The map rides the source listing — the node's existing answer to "what is
    connected here" — rather than a new endpoint the router would call second and
    stale."""

    def test_list_sources_reports_scope_supply(self, monkeypatch) -> None:
        import asyncio

        from topos.api import source_install

        monkeypatch.setattr(
            source_install.install_service, "rehydrate_active_installs_runtime", lambda: None
        )
        monkeypatch.setattr(source_install.install_service, "list_installs", lambda **_: [])
        payload = asyncio.run(
            source_install._list_sources_core(
                {"user_id": "u", "dataset_id": "d", "topos_id": "t"}
            )
        )
        assert payload["status"] == "ok"
        assert isinstance(payload["scope_supply"], dict)

    def test_a_projection_failure_never_costs_the_source_list(self, monkeypatch) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("registry is on fire")

        monkeypatch.setattr("topos.query.retrieval.routing_supply_states", boom)
        from topos.api import source_install

        assert source_install._scope_supply(["grow_journal"]) == {}


class TestTheCallSiteItself:
    """Unit-testing the probe was not enough.

    `_scope_supply_state` had ten green tests while the line that CALLS it referenced an
    undefined name, because nothing exercised the empty-with-a-ledger path end to end.
    The live node answered `denied · name 'installed_source_ids' is not defined` on the
    very first query. A helper can be perfect and still never run.
    """

    def test_the_empty_branch_has_no_undefined_names(self) -> None:
        """Every name used where the cause is attributed must exist in that scope."""
        import ast
        import inspect
        import textwrap

        from topos.query import retrieval

        src = textwrap.dedent(inspect.getsource(retrieval.DefaultSignalRetrievalAdapter.retrieve))
        tree = ast.parse(src)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_scope_supply_state"
        ]
        assert calls, "the supply-state probe is no longer called from retrieve()"

        # Everything the function binds: parameters, assignments, comprehension and
        # with/for targets. That is what "in scope at this line" actually means.
        fn = tree.body[0]
        bound = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update((a.asname or a.name).split(".")[0] for a in node.names)
        for name in (n.id for c in calls for n in ast.walk(c) if isinstance(n, ast.Name)):
            if name in {"_scope_supply_state", "getattr", "None"}:
                continue
            assert name in bound or name in dir(retrieval), (
                f"{name!r} is used at the supply-state call site but is neither a "
                "parameter of retrieve() nor a module-level name — this is exactly the "
                "NameError that reached the live node"
            )
