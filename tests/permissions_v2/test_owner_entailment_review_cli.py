"""The owner's OD-38 confirmation CLI: it only runs where the owner is reading, and it only says what the
owner decided. Its decisions are the route's own; the end-to-end case drives the real route."""
import importlib
import io
import json
import sys
from pathlib import Path

import pytest

from tests.permissions_v2.test_entailment_grounding import (  # noqa: F401 -- fixtures
    FACT_MESSAGE, facts, paraphrase, route)
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- fixture
from topos.permissions_v2 import entailment_grounding as eg

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
cli = importlib.import_module("owner_entailment_review")

SECRET = "a claim only the owner may read"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, candidates, *, list_status=200, decision_status=200):
        self.candidates, self.list_status, self.decision_status, self.sent = candidates, list_status, decision_status, []

    def post(self, path, json):
        assert path == cli.ROUTE
        self.sent.append(json)
        if json["operation"] == "list":
            return FakeResponse(self.list_status, {"candidates": self.candidates})
        return FakeResponse(self.decision_status, {"status": "ok"})


def candidates():
    return [{"candidate_id": "a" * 64, "kind": "fact", "claim": SECRET, "message": "m1", "status": "pending"},
            {"candidate_id": "b" * 64, "kind": "goal", "claim": "c2", "message": "m2", "status": "pending"},
            {"candidate_id": "c" * 64, "kind": "fact", "claim": "done", "message": "m3", "status": "confirmed"}]


def run(client, answers, bind=None, top=None):
    shown, asked = [], iter(answers)
    outcome = cli.review(client, bind or {"node_id": "n"}, ask=lambda _prompt: next(asked), show=shown.append,
                         top=top)
    return outcome, shown


def test_each_pending_claim_gets_exactly_the_owners_answer():
    client = FakeClient(candidates())
    outcome, shown = run(client, ["c", "r"])
    assert [(b["operation"], b.get("candidate_id")) for b in client.sent] == [
        ("list", None), ("confirm", "a" * 64), ("reject", "b" * 64)]
    assert outcome == {"pending": 2, "offered": 2, "confirmed": 1, "rejected": 1, "skipped": 0, "stale": 0}
    assert any(SECRET in text for text in shown) and not any("done" in text for text in shown)


def test_skip_sends_nothing_and_quit_stops_everything_after_it():
    client = FakeClient(candidates())
    outcome, _ = run(client, ["s", "q"])
    assert [b["operation"] for b in client.sent] == ["list"]
    assert outcome["skipped"] == 2 and outcome["confirmed"] == outcome["rejected"] == 0


def test_an_unclear_answer_is_asked_again_never_guessed():
    client = FakeClient(candidates()[:1])
    outcome, _ = run(client, ["yes", "confirm all", "c"])
    assert [b["operation"] for b in client.sent] == ["list", "confirm"] and outcome["confirmed"] == 1


def test_a_stale_claim_is_reported_and_the_rest_continue():
    client = FakeClient(candidates(), decision_status=409)
    outcome, _ = run(client, ["c", "c"])
    assert outcome["stale"] == 2 and outcome["confirmed"] == 0


@pytest.mark.parametrize("status,words", [(404, "ENTAILMENT_GROUNDING"), (403, "not the owner"), (503, "HTTP 503")])
def test_a_refused_list_says_why_without_a_claim(status, words):
    with pytest.raises(cli.Refused, match=words) as caught:
        run(FakeClient(candidates(), list_status=status), [])
    assert SECRET not in str(caught.value)


def test_it_refuses_to_run_without_a_terminal_and_never_connects(monkeypatch, capsys):
    monkeypatch.setattr(cli, "open_client", lambda _path: pytest.fail("connected without a terminal"))
    no_tty = io.StringIO()
    assert cli.main([], stdin=no_tty, stdout=no_tty) == 2
    assert "terminal" in capsys.readouterr().err and no_tty.getvalue() == ""


def test_the_binding_is_the_nodes_own_config(tmp_path):
    config = tmp_path / "config.json"
    identity = {"environment_id": "e", "node_id": "n", "resource_id": "r", "owner_id": "o", "other": "x"}
    config.write_text(json.dumps({"identity": identity}))
    assert cli.binding(config) == {"environment_id": "e", "node_id": "n", "resource_id": "r", "owner_id": "o"}
    with pytest.raises(cli.Refused):
        cli.binding(tmp_path / "missing.json")


@pytest.mark.parametrize('paraphrase', [FACT_MESSAGE], indirect=True)
def test_end_to_end_the_owners_confirmation_releases_the_claim_through_the_real_route(route, monkeypatch):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, node, binding = route
    monkeypatch.setenv(eg.FLAG, "true")
    with TestClient(UDSChannelApp(app)) as client:
        outcome, shown = run(client, ["c"], binding)
    assert outcome["confirmed"] == 1 and len(shown) == 2
    assert len(facts(node)) == 1


@pytest.mark.parametrize('paraphrase', [FACT_MESSAGE], indirect=True)
def test_end_to_end_a_reject_is_sticky(route, monkeypatch):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, node, binding = route
    monkeypatch.setenv(eg.FLAG, "true")
    with TestClient(UDSChannelApp(app)) as client:
        run(client, ["r"], binding)
        outcome, shown = run(client, [], binding)
    assert outcome["pending"] == 0 and facts(node) == []


def ranked_candidates():
    """Three pending claims: one whose words are all in its cited text, one partly, one not at all."""
    return [
        {"candidate_id": "1" * 64, "kind": "goal", "claim": "I intend to plan the offsite agenda.",
         "message": "Thinking about lots of things today.", "status": "pending"},
        {"candidate_id": "2" * 64, "kind": "goal", "claim": "I intend to finish the quarterly draft.",
         "message": "Goal: finish the quarterly draft", "status": "pending", "source_family": "journal_entry"},
        {"candidate_id": "3" * 64, "kind": "fact", "claim": "I am working on Atlas.",
         "message": "A long day. Some of it on Atlas, mostly meetings about other things.", "status": "pending"},
    ]


def test_the_best_supported_claim_is_offered_first():
    client = FakeClient(ranked_candidates())
    _outcome, shown = run(client, ["s", "s", "s"])
    order = [c["candidate_id"][0] for c in cli.rank(ranked_candidates())]
    assert order == ["2", "3", "1"]
    assert shown[0].startswith("--- 1 of 3 (goal, cites a journal entry) ---")


def test_top_offers_only_the_best_n_and_says_how_many_wait():
    client = FakeClient(ranked_candidates())
    outcome, shown = run(client, ["c"], top=1)
    assert [(b["operation"], b.get("candidate_id")) for b in client.sent] == [("list", None), ("confirm", "2" * 64)]
    assert outcome["pending"] == 3 and outcome["offered"] == 1 and outcome["confirmed"] == 1
    assert len(shown) == 2   # one candidate, one confirmation line


def test_ties_keep_the_shorter_text_first():
    short = {"candidate_id": "s" * 64, "kind": "goal", "claim": "I intend to x y.", "message": "n", "status": "pending"}
    long_ = {**short, "candidate_id": "l" * 64, "message": "n" * 50}
    assert [c["candidate_id"][0] for c in cli.rank([long_, short])] == ["s", "l"]
