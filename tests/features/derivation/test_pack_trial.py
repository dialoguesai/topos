"""`pack_trial.run_trial`: a pack file's own examples on the local model, judged, with nothing written.

protects: a trial reads only the texts under the file's `eval` and sends each through the prompt a real run
builds (`template.build_prompt`) with the options a real run uses and no `keep_alive`; a gold example passes only
when every fact it expects came back (same predicate, about the owner, the named fields equal by the writer's own
equality), a negative control only when nothing came back; a trial writes no row to any table and no file to the
pack directory; it is refused, before any model call, for a file that is not a pack, for more examples than the
cap, and on a node with no local model picked; model calls that fail are reported and end the trial after two in
a row; the look for the model loads and calls nothing.

The model is a stub (`OllamaAdapter._generate` is replaced): no model host is contacted. The database is a
temporary one. Synthetic only.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.request
from typing import Any, Dict, List

import pytest
import yaml

from topos.features.derivation import custom_packs, pack_trial, processing, template
from topos.features.derivation.custom_packs import check_content
from topos.permissions_v2 import answer_gate
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

PACK = "custom.gardening"
MODEL = "stub-model"


def definition(**changes) -> Dict[str, Any]:
    data = {
        "pack": PACK, "version": "1.0.0", "title": "My gardening",
        "sensitivity_class": "personal", "role_policy": "authored_only", "net_subject": "deny",
        "routing": {"descriptors": ["Plants the owner grows"], "exemplars": ["I planted basil"]},
        "guidance": {"definitions": "Plants the owner says they grow."},
        "predicates": [{"name": "custom.plant", "value_type": "structured", "cardinality": "multi",
                        "temporal": "interval", "altitude": "stated",
                        "value_schema": {"name": "string", "place": "string"}}],
        "eval": {"gold": [{"text": "I grow tomatoes in my garden.", "role": "authored",
                           "expect": [{"predicate": "custom.plant", "value": {"name": "tomatoes"}}]}],
                 "negative_controls": [{"text": "The film had a beautiful garden.", "role": "authored",
                                        "expect": []}]},
        "consumers": ["assistant_context"],
    }
    data.update(changes)
    return data


def content(**changes) -> bytes:
    return yaml.safe_dump(definition(**changes)).encode("utf-8")


def says(*assertions: Dict[str, Any]) -> Dict[str, Any]:
    """What the stubbed model answers: the JSON a real extraction call returns."""
    return {"text": json.dumps({"assertions": [
        {"predicate": "custom.plant", "about": "owner", "occurrence_date": None, "confidence": 0.9,
         "quote": "I grow tomatoes", **a} for a in assertions]})}


class _Model:
    """The local model, stubbed. `answers` maps a phrase of the example's text to what comes back."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.answers: Dict[str, Any] = {}
        self.background: List[bool] = []

    def __call__(self, _adapter, model, prompt, **kwargs):
        self.calls.append({"model": model, "prompt": prompt, "kwargs": kwargs})
        self.background.append(answer_gate.is_background())
        for phrase, answer in self.answers.items():
            if phrase in prompt:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        return {"text": json.dumps({"assertions": []})}


@pytest.fixture
def model(monkeypatch):
    from topos.engine.backends import ollama

    stub = _Model()
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", lambda self, m, p, **kw: stub(self, m, p, **kw))
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c=None: MODEL)
    return stub


@pytest.fixture
def node(tmp_path, monkeypatch):
    home = tmp_path / "node" / "fact-packs"
    monkeypatch.setattr(custom_packs, "custom_pack_dir", lambda: home)
    custom_packs._SAID.clear()
    conn = sqlite3.connect(tmp_path / "node.db", check_same_thread=False)
    apply_all_migrations(conn)
    conn.commit()
    yield conn, home
    conn.close()


def _rows(conn: sqlite3.Connection) -> Dict[str, int]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    counts = {}
    for table in tables:
        try:
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except sqlite3.OperationalError:      # a virtual table whose module is not loaded here
            counts[table] = -1
    return counts


# ----------------------------------------------------------------------------- what is judged, and how
def test_a_gold_example_passes_when_the_expected_fact_comes_back_and_a_negative_control_when_nothing_does(node, model):
    conn, _home = node
    model.answers["I grow tomatoes"] = says({"value": {"name": "Tomatoes ", "place": "garden"}})
    report = pack_trial.run_trial(content(), conn)
    gold, negative = report["examples"]
    assert (gold["kind"], gold["index"], gold["outcome"], gold["reason"]) == ("gold", 1, "pass", None)
    assert gold["text"] == "I grow tomatoes in my garden." and gold["routed"] is True
    assert gold["expected"] == [{"predicate": "custom.plant", "value": {"name": "tomatoes"}}]
    assert gold["returned"] == [{"predicate": "custom.plant", "value": {"name": "Tomatoes ", "place": "garden"},
                                 "about": "owner", "confidence": 0.9, "quote": "I grow tomatoes"}]
    assert (negative["kind"], negative["outcome"], negative["returned"]) == ("negative_control", "pass", [])
    assert report["totals"] == {"examples": 2, "passed": 2, "failed": 0, "unjudged": 0, "errors": 0, "skipped": 0,
                                "gold": {"examples": 1, "passed": 1, "failed": 0},
                                "negative_controls": {"examples": 1, "passed": 1, "failed": 0}}
    assert (report["pack_id"], report["version"], report["model"]) == (PACK, "1.0.0", MODEL)
    assert report["template"] == template.TEMPLATE_VERSION and report["fact_check"] == "not_run"
    assert report["stopped"] is None


@pytest.mark.parametrize("answer,reason", [
    (says(), "expected_fact_missing"),                                   # nothing came back
    (says({"value": {"name": "basil"}}), "expected_fact_missing"),       # the field differs
    (says({"value": {"place": "garden"}}), "expected_fact_missing"),     # the named field is absent
    (says({"value": {"name": "tomatoes"}, "about": "other:Sam"}), "expected_fact_missing"),   # not about the owner
    (says({"predicate": "custom.other", "value": {"name": "tomatoes"}}), "expected_fact_missing"),   # the parser drops it
], ids=["nothing", "other-value", "field-absent", "about-someone-else", "unknown-predicate"])
def test_a_gold_example_fails_when_the_expected_fact_does_not_come_back(node, model, answer, reason):
    conn, _home = node
    model.answers["I grow tomatoes"] = answer
    gold = pack_trial.run_trial(content(), conn)["examples"][0]
    assert (gold["outcome"], gold["reason"], gold["missing"]) == ("fail", reason, 1)


def test_a_negative_control_fails_when_any_assertion_comes_back(node, model):
    conn, _home = node
    model.answers["I grow tomatoes"] = says({"value": {"name": "tomatoes"}})
    model.answers["The film had"] = says({"value": {"name": "roses"}, "quote": "a beautiful garden"})
    report = pack_trial.run_trial(content(), conn)
    negative = report["examples"][1]
    assert (negative["outcome"], negative["reason"], negative["extra"]) == ("fail", "assertion_returned", 1)
    assert report["totals"]["failed"] == 1 and report["totals"]["negative_controls"]["failed"] == 1


def test_a_gold_example_that_names_no_fact_is_not_judged_and_a_role_the_pack_does_not_read_is_not_called(node, model):
    conn, _home = node
    gold = [{"text": "I grow tomatoes in my garden."},                                # no `expect` at all
            {"text": "Your tomato seeds have shipped.", "role": "addressed",          # a role authored_only skips
             "expect": [{"predicate": "custom.plant", "value": {"name": "tomato"}}]}]
    report = pack_trial.run_trial(content(eval={"gold": gold, "negative_controls": [{"text": "A film."}]}), conn)
    first, second, _negative = report["examples"]
    assert (first["outcome"], first["reason"]) == ("unjudged", "no_expected_fact")
    assert (second["outcome"], second["reason"]) == ("fail", "role_not_read")
    assert not any("tomato seeds" in call["prompt"] for call in model.calls), "a real run would never read it"
    assert report["totals"]["unjudged"] == 1 and report["totals"]["failed"] == 1


def test_the_writers_equality_decides_a_match():
    """Case and spacing do not count; a field the example does not name is not compared; a list is compared whole."""
    assert pack_trial._same({"name": "Tomatoes"}, {"name": "  tomatoes", "place": "garden"})
    assert pack_trial._same("Daily", "daily ") and not pack_trial._same("daily", "weekly")
    assert not pack_trial._same({"name": "tomatoes"}, "tomatoes")
    assert pack_trial._same(["a", "B"], ["b", "a"]) and not pack_trial._same(["a"], ["a", "b"])


# ----------------------------------------------------------------------------- the call it makes
def test_each_example_is_the_prompt_and_the_call_a_real_run_makes(node, model):
    conn, _home = node
    pack_trial.run_trial(content(eval={"gold": [{"text": "I grow tomatoes in my garden.", "date": "2026-05-01",
                                                 "expect": [{"predicate": "custom.plant"}]}],
                                       "negative_controls": [{"text": "A film."}]}), conn)
    pack = check_content(content())
    first = model.calls[0]
    assert first["model"] == MODEL
    assert first["prompt"] == template.build_prompt(pack, "I grow tomatoes in my garden.", "2026-05-01", "authored")
    assert first["kwargs"] == {"num_predict": processing.EXTRACTION_TOKENS, **processing.GENERATE_OPTIONS}
    assert "keep_alive" not in first["kwargs"], "a trial never says how long a model stays loaded"
    assert len(model.calls) == 2 and model.background == [True, True], "it runs as background work"


def test_a_real_derivation_call_asks_the_model_for_the_same_thing(node, model):
    """`measured_generate` (ingest, backfill) and the trial share one call, so they cannot drift apart."""
    conn, _home = node
    from topos.engine.backends.ollama import OllamaAdapter

    processing.measured_generate(conn, OllamaAdapter(), MODEL, "prompt", pack_id=PACK, phase="extraction",
                                 mode="backfill")
    assert model.calls[0]["kwargs"] == {"num_predict": 900, "think": False, "temperature": 0.0, "num_ctx": 8192,
                                        "timeout": 180}


# ----------------------------------------------------------------------------- it writes nothing
def test_a_trial_writes_no_row_and_no_file(node, model):
    conn, home = node
    model.answers["I grow tomatoes"] = says({"value": {"name": "tomatoes"}})
    before = _rows(conn)
    changes = conn.total_changes
    report = pack_trial.run_trial(content(), conn)
    assert report["totals"]["passed"] == 2 and len(model.calls) == 2
    assert _rows(conn) == before and conn.total_changes == changes and not conn.in_transaction
    assert before["derivation_processing"] == 0, "not even the measurement row a real call leaves"
    assert not home.exists(), "nothing was put in the pack directory"
    assert custom_packs.load_custom_packs() == {}


# ----------------------------------------------------------------------------- what it refuses
def test_a_file_with_more_examples_than_the_cap_is_refused_before_any_model_call(node, model):
    conn, _home = node
    many = [{"text": f"I grow plant number {n}."} for n in range(pack_trial.MAX_EXAMPLES)]
    with pytest.raises(pack_trial.TrialRefused) as refused:
        pack_trial.run_trial(content(eval={"gold": many, "negative_controls": [{"text": "A film."}]}), conn)
    assert refused.value.code == "too_many_examples" and "at most 20" in str(refused.value)
    assert model.calls == []
    exactly = pack_trial.run_trial(content(eval={"gold": many[:-1], "negative_controls": [{"text": "A film."}]}), conn)
    assert exactly["totals"]["examples"] == pack_trial.MAX_EXAMPLES == 20


@pytest.mark.parametrize("data,says_", [
    (yaml.safe_dump(definition(pack="work.career")).encode(), "must be an id that starts with `custom.`"),
    (yaml.safe_dump(definition(net_subject="allow")).encode(), "A pack you add can only describe you."),
    (b"pack: [unclosed\n", "The file is not valid YAML"),
    (b"\xff\xfe not text", "The file is not UTF-8 text."),
], ids=["shipped-id", "outward", "not-yaml", "not-utf8"])
def test_a_file_that_is_not_a_pack_is_refused_in_one_sentence_before_any_model_call(node, model, data, says_):
    conn, _home = node
    with pytest.raises(pack_trial.TrialRefused) as refused:
        pack_trial.run_trial(data, conn)
    assert refused.value.code == "pack_invalid" and says_ in str(refused.value)
    assert model.calls == []


def test_a_node_with_no_local_model_picked_is_refused(node, model, monkeypatch):
    conn, _home = node
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c=None: "")
    with pytest.raises(pack_trial.TrialRefused) as refused:
        pack_trial.run_trial(content(), conn)
    assert refused.value.code == "no_local_model" and model.calls == []


def test_one_trial_at_a_time(node, model):
    conn, _home = node
    assert pack_trial._one_at_a_time.acquire(blocking=False)
    try:
        with pytest.raises(pack_trial.TrialRefused) as refused:
            pack_trial.run_trial(content(), conn)
    finally:
        pack_trial._one_at_a_time.release()
    assert refused.value.code == "trial_already_running" and model.calls == []
    assert pack_trial.run_trial(content(), conn)["totals"]["examples"] == 2, "the slot is free again afterwards"


def test_model_calls_that_fail_are_reported_and_two_in_a_row_end_the_trial(node, model):
    conn, _home = node
    gold = [{"text": f"I grow plant number {n}.", "expect": [{"predicate": "custom.plant"}]} for n in range(4)]
    model.answers["plant number 0"] = RuntimeError("connection refused")
    model.answers["plant number 1"] = {"status": "deferred", "text": ""}
    report = pack_trial.run_trial(content(eval={"gold": gold, "negative_controls": [{"text": "A film."}]}), conn)
    outcomes = [(row["outcome"], row["reason"]) for row in report["examples"]]
    assert outcomes == [("error", "model_call_failed"), ("error", "model_call_failed"),
                        ("skipped", "model_unavailable"), ("skipped", "model_unavailable"),
                        ("skipped", "model_unavailable")]
    assert report["stopped"] == "model_unavailable" and len(model.calls) == 2
    assert report["totals"]["errors"] == 2 and report["totals"]["skipped"] == 3


def test_the_time_limit_leaves_the_rest_not_run(node, model):
    conn, _home = node
    ticks = iter([0.0, 0.0, 1.0, 2.0, pack_trial.TIME_LIMIT_S + 5.0, pack_trial.TIME_LIMIT_S + 6.0])
    report = pack_trial.run_trial(content(), conn, clock=lambda: next(ticks))
    assert [row["outcome"] for row in report["examples"]] == ["fail", "skipped"]
    assert report["examples"][1]["reason"] == "time_limit" and report["stopped"] == "time_limit"
    assert len(model.calls) == 1


# ----------------------------------------------------------------------------- the look for the model
def test_the_look_for_the_model_loads_and_calls_nothing(node, model, monkeypatch):
    conn, _home = node
    asked: List[str] = []

    class _Listed:
        def __init__(self, names):
            self.body = json.dumps({"models": [{"name": name} for name in names]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self.body

    def host(names):
        def urlopen(request, timeout=None):
            asked.append(f"{request.get_method()} {request.full_url}")
            if isinstance(names, BaseException):
                raise names
            return _Listed(names)
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    host([f"{MODEL}:latest", "other:7b"])
    assert pack_trial.model_status(conn) == {"model": MODEL, "available": "yes"}
    host(["other:7b"])
    assert pack_trial.model_status(conn) == {"model": MODEL, "available": "no"}
    host(OSError("connection refused"))
    assert pack_trial.model_status(conn) == {"model": MODEL, "available": "unknown"}
    assert asked and all(line.startswith("GET ") and line.endswith("/api/tags") for line in asked)
    assert model.calls == [], "no generation call: the host's list of what is on disk is the only request"

    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c=None: "")
    asked.clear()
    assert pack_trial.model_status(conn) == {"model": None, "available": "no"} and asked == []
