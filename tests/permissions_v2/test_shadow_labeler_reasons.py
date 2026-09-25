"""The local labeler asks the node's own model host, and every way of not answering has its own name.

Measured on the beta stack (24 Sep 2026): the node carries `ENGINE_OLLAMA_BASE_URL=http://ingress:18094` and shares
the gateway's network namespace, where nothing listens on loopback 11434. `shadow_labeler_local` hard-coded that
loopback, so 28 of 30 fresh re-score samples answered "the local model did not answer" about 17 ms apart and were
filed `labeler_unresolved`, the reason a model that looked and abstained is filed under. The model was never asked.

Every text here is synthetic, written in this file. The one socket this suite opens is its own: a fake Ollama on a
random loopback port, started and stopped by the test, that answers what the test pinned. Nothing here probes a
model host on the machine, and nothing starts one.

  H1     the transport asks the configured host; every request arrives there and none goes to loopback 11434
  H2     the host is `ENGINE_OLLAMA_BASE_URL` as the engine's adapter reads it; a node that set nothing, set it
         blank, or whose settings will not load keeps the campaign's loopback
  R1-R6  one reason per way of not answering: unreachable, model_unreviewed, rubric_mismatch, empty_text,
         vocabulary -- and `labeler_unresolved` only for a genuine abstention
  S0-S5  the seam: every reason is a code in the control plane's grammar; the row carries the labeler's code; the
         log carries the code and nothing else; a reason that is not a code is dropped; a labeler that only scores
         is still `labeler_unresolved`; the real labeler's code reaches the reply end to end
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from topos.permissions_v2 import shadow_labeler_local as local
from topos.permissions_v2 import shadow_labelers, shadow_rescore
from topos.permissions_v2.contract import PolicyV2

pytestmark = [pytest.mark.p0]

WORK_NOTE = "Moving the deploy to Thursday so the release notes land first."
HOBBY_NOTE = "Finished the second volume last night; the middle third drags."
WORK_LABELS = json.dumps({"domains": ["work", "plans"], "sensitivity": "none"})
HOBBY_LABELS = json.dumps({"domains": ["hobbies"], "sensitivity": "none"})


def _policy():
    """A minimal p2a-v1 policy with one permit rule that releases anything."""
    true = {"kind": "all_of", "terms": []}
    return PolicyV2.parse({
        "version": "topos-policy/v2", "policy_version_id": "policy-1",
        "binding": {"environment_id": "beta", "node_id": "node-1", "resource_id": "resource-1",
                    "owner_id": "owner-1", "actor_id": "actor-1", "client_id": "client-1",
                    "grant_id": "grant-1", "assignment_id": "assignment-1"},
        "versions": {"vocabulary": "vocabulary-1", "capability": "permissions-beta/p2a-v1"},
        "validity": {"starts_at": 1000, "expires_at": 5000},
        "source_universe": {"universe_id": "sources-1", "revision": 1, "source_ids": ["source-A"]},
        "hard_constraints": {"owner_only": "deny", "unknown_classification": "withhold",
                             "unknown_lineage": "withhold", "cross_rule_derivation": "deny",
                             "capability_growth": "require_consent"},
        "rules": [{"rule_id": "rule-A", "effect": "permit",
                   "evidence_use": {"sources": {"kind": "only", "values": ["source-A"]}, "predicate": true,
                                    "purpose": "reading",
                                    "processors": {"kind": "only", "values": ["owner-engine-local"]},
                                    "new_records": "include_if_predicate"},
                   "release": {"predicate": true, "ceiling": "raw",
                               "forms": [{"family": "canonical_record", "operation": "read",
                                          "view_id": "canonical.message_disclosure.v1",
                                          "tables": ["conversation_messages"]}]}}],
        "evaluator": {"kind": "hard_rules", "version": "hard-rules/p2a-v1"}, "natural_language": None})


def _records(*texts):
    return [{"record_id": "r.%d" % index, "canonical_table": "conversation_messages", "source_id": "source-A",
             "content": text} for index, text in enumerate(texts)]


def _request(**changes):
    return {"version": shadow_rescore.VERSION, "request_id": "req-1", "grant_id": "grant-1",
            "capability": "permissions-beta/p2a-v1", "output_sha256": "a" * 64, "labeler_mode": "local", **changes}


class _Stub:
    """A transport that answers what the test pinned, per call, in order. Opens nothing."""

    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    async def label(self, text):
        self.seen.append(text)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Assessing:
    """A labeler that says why, through `assess`, the way the local one does."""

    def __init__(self, verdict, reason=None):
        self.id, self.family, self.outcome = "local-qwen", "qwen", local.Assessment(verdict, reason)

    def score(self, records, policy):
        return self.outcome.verdict

    def assess(self, records, policy):
        return self.outcome


class _ScoringOnly:
    """A labeler from before the seam could carry a reason."""

    id, family = "local-qwen", "qwen"

    def score(self, records, policy):
        return "unresolved"


class _FakeOllama:
    """A stand-in for the node's Ollama on a random loopback port.

    It lists the pinned tag at the digest the test chose, answers `/api/chat` as the test chose, and records every
    request it receives: method, path, the `Host` header the client sent, and the parsed body.
    """

    def __init__(self, *, answer=WORK_LABELS, digest=local.MODEL_REVISION, reply_model=local.MODEL, done=True,
                 status=200):
        self.answer, self.digest, self.reply_model, self.done, self.status = answer, digest, reply_model, done, status
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return None

            def _reply(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.requests.append(("GET", self.path, self.headers.get("Host"), None))
                if self.path != "/api/tags":
                    return self._reply(404, {})
                self._reply(fake.status, {"models": [{"name": local.MODEL, "digest": fake.digest}]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(("POST", self.path, self.headers.get("Host"), body))
                if self.path != "/api/chat":
                    return self._reply(404, {})
                self._reply(fake.status, {"model": fake.reply_model, "done": fake.done,
                                          "message": {"role": "assistant", "content": fake.answer}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    @property
    def paths(self):
        return [(method, path) for method, path, _, _ in self.requests]

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False


def _labeler_at(host, seen=None):
    """The real labeler on a transport bound to the fake host, opening and closing its own client per score."""
    async def record(request):
        if seen is not None:
            seen.append(request)

    def open_transport():
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False, event_hooks={"request": [record]})
        return local._PinnedTransport(client, base_url=host.url)

    return local.LocalRubricLabeler(open_transport=open_transport)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    shadow_labelers.clear()
    # The engine's runtime must never be asked to start a host on the labeler's behalf: a closed local port makes
    # `ensure_running` open the owner's Ollama app. These raise if anything here reaches them.
    from topos.engine import ollama_install, ollama_runtime

    def never(*args, **kwargs):
        raise AssertionError("the labeler asked the engine to start or open a model host")

    monkeypatch.setattr(ollama_runtime, "ensure_running", never)
    monkeypatch.setattr(ollama_runtime, "default_open_app", never)
    monkeypatch.setattr(ollama_runtime, "default_spawn_serve", never, raising=False)
    monkeypatch.setattr(ollama_install, "default_open_app", never)
    yield
    shadow_labelers.clear()


# --------------------------------------------------------------------------- H1


def test_H1_the_labeler_asks_the_configured_host_and_nothing_goes_to_loopback_11434(monkeypatch):
    """The beta stack's defect: a node whose Ollama is elsewhere has nothing on loopback 11434."""
    from topos.config import settings as config

    seen = []

    async def record(request):
        seen.append(request)

    def open_transport():
        # No `base_url`: the transport resolves the host itself, from the setting.
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False, event_hooks={"request": [record]})
        return local._PinnedTransport(client)

    with _FakeOllama() as host:
        # A trailing slash, as an operator types one; the transport normalises it the way the adapter does.
        monkeypatch.setattr(config.settings, local.BASE_URL_SETTING, host.url + "/")
        assert local.configured_base_url() == host.url
        opened = local.open_transport()
        assert opened.base_url == host.url
        asyncio.run(opened.client.aclose())
        labeler = local.LocalRubricLabeler(open_transport=open_transport)
        assert labeler.assess(_records(WORK_NOTE), _policy()) == ("agree", None)
        assert labeler.score(_records(WORK_NOTE), _policy()) == "agree"
    # Every request the labeler made arrived at the fake host, and the client sent nothing anywhere else.
    assert host.paths == [("GET", "/api/tags"), ("POST", "/api/chat")] * 2
    assert {hostname for _, _, hostname, _ in host.requests} == {"127.0.0.1:%d" % host.port}
    assert len(seen) == 4
    assert all(request.url.host == "127.0.0.1" and request.url.port == host.port for request in seen)
    assert not any(request.url.port == 11434 for request in seen)
    # The pinned request shape, at the pinned model, with the pinned prompt, and the record's text as the user turn.
    chat = host.requests[1][3]
    assert chat["model"] == local.MODEL and chat["stream"] is False and chat["think"] is False
    assert chat["format"] == "json"
    assert chat["messages"] == [{"role": "system", "content": local.system_prompt()},
                                {"role": "user", "content": WORK_NOTE}]


# --------------------------------------------------------------------------- H2


def test_H2_the_host_is_the_engines_own_setting_and_the_campaigns_loopback_when_the_node_set_nothing(monkeypatch):
    from topos.config import settings as config

    # The same env contract as the adapter: `ENGINE_OLLAMA_BASE_URL` reaches `settings.engine_ollama_base_url`.
    assert local.BASE_URL_SETTING == "engine_ollama_base_url"
    assert local.BASE_URL_SETTING in config.Settings.model_fields
    monkeypatch.setenv("ENGINE_OLLAMA_BASE_URL", "http://ingress:18094/")
    assert local.configured_base_url(config.Settings(_env_file=None)) == "http://ingress:18094"
    monkeypatch.delenv("ENGINE_OLLAMA_BASE_URL")
    assert local.configured_base_url(config.Settings(_env_file=None)) == local.ORIGIN

    def fake(value, *, configured=True):
        return types.SimpleNamespace(model_fields_set={local.BASE_URL_SETTING} if configured else set(),
                                     engine_ollama_base_url=value)

    # The setting's own default is the adapter's spelling of the loopback, not a configuration.
    assert local.configured_base_url(fake("http://localhost:11434", configured=False)) == local.ORIGIN
    # Blank is nothing; a value is used as given, less a trailing slash.
    for blank in ("", "   ", None):
        assert local.configured_base_url(fake(blank)) == local.ORIGIN
    assert local.configured_base_url(fake("http://ingress:18094/")) == "http://ingress:18094"
    assert local.configured_base_url(fake("http://192.0.2.10:11434")) == "http://192.0.2.10:11434"
    # Settings that will not load, or that will not answer, are a node that has set nothing.
    monkeypatch.setitem(sys.modules, "topos.config.settings", None)
    assert local.configured_base_url() == local.ORIGIN

    class _Broken:
        @property
        def model_fields_set(self):
            raise RuntimeError("no settings today")

    assert local.configured_base_url(_Broken()) == local.ORIGIN
    # The campaign's own binding is still the loopback, byte for byte.
    assert local.ORIGIN == "http://127.0.0.1:11434"


# --------------------------------------------------------------------------- R1


def test_R1_a_host_that_cannot_be_asked_or_does_not_complete_is_labeler_unreachable():
    records, policy = _records(WORK_NOTE), _policy()
    # No connection, and whatever else a transport can raise: the model was not heard from.
    for failure in (httpx.ConnectError("[Errno 61] Connection refused"), httpx.ReadTimeout("read timed out"),
                    RuntimeError("the host is not there")):
        outcome = local.LocalRubricLabeler(_Stub(failure)).assess(records, policy)
        assert outcome == ("unresolved", "labeler_unreachable"), failure
    # A host that answers with an error status.
    with _FakeOllama(status=503) as host:
        assert _labeler_at(host).assess(records, policy) == ("unresolved", "labeler_unreachable")
    assert host.paths == [("GET", "/api/tags")]
    # A host that answers, at the reviewed digest, with a generation that did not complete.
    with _FakeOllama(done=False) as host:
        assert _labeler_at(host).assess(records, policy) == ("unresolved", "labeler_unreachable")
    assert host.paths == [("GET", "/api/tags"), ("POST", "/api/chat")]


# --------------------------------------------------------------------------- R2


def test_R2_a_model_that_is_not_the_reviewed_revision_is_labeler_model_unreviewed():
    records, policy = _records(WORK_NOTE), _policy()
    # The installed tag is at another digest: refused before the model is asked anything.
    with _FakeOllama(digest="0" * 64) as host:
        assert _labeler_at(host).assess(records, policy) == ("unresolved", "labeler_model_unreviewed")
    assert host.paths == [("GET", "/api/tags")]
    # The tag is right and the answer came from some other model.
    with _FakeOllama(reply_model="qwen3.5:latest") as host:
        assert _labeler_at(host).assess(records, policy) == ("unresolved", "labeler_model_unreviewed")
    assert host.paths == [("GET", "/api/tags"), ("POST", "/api/chat")]
    # The digest check is the one `test_L4c` pins, and it still raises a ValueError.
    assert issubclass(local.ModelUnreviewed, ValueError)


# --------------------------------------------------------------------------- R3


def test_R3_a_rubric_that_is_not_the_reviewed_bytes_is_labeler_rubric_mismatch(monkeypatch, tmp_path):
    records, policy = _records(WORK_NOTE), _policy()
    edited = tmp_path / "rubric.md"
    edited.write_text("## A rubric somebody edited\n")
    stub = _Stub(WORK_LABELS)
    monkeypatch.setattr(local, "RUBRIC_PATH", edited)
    assert local.LocalRubricLabeler(stub).assess(records, policy) == ("unresolved", "labeler_rubric_mismatch")
    # Checked before the model is asked, so the transport never saw a record.
    assert stub.seen == []
    # A rubric that is not there at all is the same reason, not a crash.
    monkeypatch.setattr(local, "RUBRIC_PATH", tmp_path / "missing.md")
    assert local.LocalRubricLabeler(stub).assess(records, policy) == ("unresolved", "labeler_rubric_mismatch")
    # And the pin itself is the one `test_L1` checks: `rubric()` still raises a ValueError.
    with pytest.raises(ValueError):
        local.rubric()
    assert issubclass(local.RubricMismatch, ValueError)


# --------------------------------------------------------------------------- R4


def test_R4_a_release_with_nothing_to_read_is_labeler_empty_text():
    policy = _policy()
    for records in ([{"content": "   "}], [{"content": None}], [{"record_id": "r.1"}], [], [object()]):
        stub = _Stub()
        assert local.LocalRubricLabeler(stub).assess(records, policy) == ("unresolved", "labeler_empty_text"), records
        assert stub.seen == []
    # A blank record after a good one: the whole release is scored whole, and the reason is the blank's.
    stub = _Stub(WORK_LABELS)
    assert local.LocalRubricLabeler(stub).assess(
        _records(WORK_NOTE, "  "), policy) == ("unresolved", "labeler_empty_text")
    assert stub.seen == [WORK_NOTE]


# --------------------------------------------------------------------------- R5


@pytest.mark.parametrize("answer", [
    '{"domains": ["astrology"], "sensitivity": "none"}',
    '{"domains": ["work"], "sensitivity": "sensitive"}',
    '{"domains": ["work"], "sensitivity": "none", "why": "it is about the job"}',
    "I would say this one is about work.",
    "",
    None,
])
def test_R5_an_answer_outside_the_rubrics_vocabulary_is_labeler_vocabulary(answer):
    policy = _policy()
    assert local.LocalRubricLabeler(_Stub(answer)).assess(_records(WORK_NOTE), policy) == (
        "unresolved", "labeler_vocabulary")
    # One record the model fumbled after one it labelled: still the whole release, still this reason.
    stub = _Stub(WORK_LABELS, answer)
    assert local.LocalRubricLabeler(stub).assess(_records(WORK_NOTE, HOBBY_NOTE), policy) == (
        "unresolved", "labeler_vocabulary")
    assert stub.seen == [WORK_NOTE, HOBBY_NOTE]


# --------------------------------------------------------------------------- R6


def test_R6_labeler_unresolved_is_only_the_genuine_abstention(monkeypatch):
    """The model answered in the vocabulary and the policy could not decide. Nothing else carries this code."""
    records, policy = _records(WORK_NOTE), _policy()
    monkeypatch.setattr(local, "policy_verdict", lambda policy, per_record: "indeterminate")
    stub = _Stub(WORK_LABELS)
    assert local.LocalRubricLabeler(stub).assess(records, policy) == ("unresolved", "labeler_unresolved")
    assert stub.seen == [WORK_NOTE]
    monkeypatch.undo()
    # A verdict carries no reason; a missing policy is the seam's own code for it.
    assert local.LocalRubricLabeler(_Stub(WORK_LABELS)).assess(records, policy) == ("agree", None)
    assert local.LocalRubricLabeler(_Stub(WORK_LABELS)).assess(records, None) == ("unresolved", "policy_unavailable")
    # `score` is `assess` without the reason, on every path.
    for stub, records_ in ((_Stub(WORK_LABELS), records), (_Stub(RuntimeError("down")), records),
                           (_Stub(), []), (_Stub("not json"), records)):
        labeler = local.LocalRubricLabeler(stub)
        expected = local.LocalRubricLabeler(_Stub(*stub.answers)).assess(records_, policy).verdict
        assert labeler.score(records_, policy) == expected


# --------------------------------------------------------------------------- S0


def test_S0_every_reason_is_a_code_in_the_control_planes_reason_grammar():
    assert shadow_rescore.REASON.pattern == r"^[a-z][a-z0-9_]{0,63}$"
    assert set(local.REASONS) == {"labeler_unreachable", "labeler_model_unreviewed", "labeler_rubric_mismatch",
                                  "labeler_empty_text", "labeler_vocabulary", "labeler_unresolved"}
    for code in local.REASONS + ("policy_unavailable",):
        assert shadow_rescore.REASON.fullmatch(code), code
    for failure in (local.Unreachable, local.ModelUnreviewed, local.RubricMismatch, local.EmptyText,
                    local.OutsideVocabulary):
        assert issubclass(failure, local.Unresolved) and failure.reason in local.REASONS
        assert failure.reason != "labeler_unresolved"


# --------------------------------------------------------------------------- S1


@pytest.mark.parametrize("reason", local.REASONS + ("policy_unavailable",))
def test_S1_the_row_carries_the_labelers_own_code(monkeypatch, reason):
    monkeypatch.setattr(shadow_rescore, "resolve_records", lambda runtime, request: _records(WORK_NOTE))
    monkeypatch.setattr(shadow_rescore, "resolve_policy", lambda runtime, request: _policy())
    shadow_labelers.register("local", _Assessing("unresolved", reason))
    result = shadow_rescore.rescore(object(), _request())
    assert result.verdict == "unresolved" and result.reason == reason
    assert result.labeler == "local-qwen" and result.family == "qwen" and result.output_sha256 == "a" * 64


# --------------------------------------------------------------------------- S2


def test_S2_the_log_carries_the_code_and_nothing_else(monkeypatch, caplog):
    records = [{"record_id": "r.secret-row", "canonical_table": "conversation_messages", "source_id": "source-A",
                "content": "PRIVATE CONTENT nobody may log"}]
    monkeypatch.setattr(shadow_rescore, "resolve_records", lambda runtime, request: records)
    monkeypatch.setattr(shadow_rescore, "resolve_policy", lambda runtime, request: _policy())
    request = _request(request_id="req-secret-77", grant_id="grant-secret-77")
    caplog.set_level(logging.INFO, logger="topos.permissions_v2.shadow_rescore")
    caplog.set_level(logging.INFO, logger="topos.permissions_v2.shadow_labeler_local")

    shadow_labelers.register("local", _Assessing("unresolved", "labeler_unreachable"))
    assert shadow_rescore.rescore(object(), request).reason == "labeler_unreachable"
    failures = [record for record in caplog.records if record.name == "topos.permissions_v2.shadow_rescore"]
    assert [(record.levelname, record.getMessage()) for record in failures] == [
        ("WARNING", "permissions v2 shadow re-score: unresolved, labeler_unreachable")]

    caplog.clear()
    shadow_labelers.register("local", _Assessing("unresolved", "labeler_unresolved"))
    assert shadow_rescore.rescore(object(), request).reason == "labeler_unresolved"
    abstentions = [record for record in caplog.records if record.name == "topos.permissions_v2.shadow_rescore"]
    assert [(record.levelname, record.getMessage()) for record in abstentions] == [
        ("INFO", "permissions v2 shadow re-score: unresolved, labeler_unresolved")]

    caplog.clear()
    # The real labeler, unreachable: its own line and the seam's, both the code and nothing else.
    local.register(_Stub(httpx.ConnectError("[Errno 61] Connection refused")))
    assert shadow_rescore.rescore(object(), request).reason == "labeler_unreachable"
    assert "permissions v2 shadow labeler: unresolved, labeler_unreachable" in caplog.text
    assert "permissions v2 shadow re-score: unresolved, labeler_unreachable" in caplog.text

    for secret in ("req-secret-77", "grant-secret-77", "r.secret-row", "PRIVATE CONTENT", "nobody may log",
                   "Connection refused"):
        assert secret not in caplog.text, secret
    # A verdict logs nothing here.
    caplog.clear()
    shadow_labelers.register("local", _Assessing("agree"))
    assert shadow_rescore.rescore(object(), request).reason is None
    assert not [record for record in caplog.records if record.name == "topos.permissions_v2.shadow_rescore"]


# --------------------------------------------------------------------------- S3


@pytest.mark.parametrize("reason", [
    "Not A Code", "x" * 65, "the record says PRIVATE CONTENT", "", "req-secret-77", "labeler_unreachable ", 42,
    b"labeler_unreachable",
])
def test_S3_a_reason_that_is_not_a_code_is_dropped_and_never_logged(monkeypatch, caplog, reason):
    monkeypatch.setattr(shadow_rescore, "resolve_records", lambda runtime, request: _records(WORK_NOTE))
    monkeypatch.setattr(shadow_rescore, "resolve_policy", lambda runtime, request: _policy())
    caplog.set_level(logging.INFO, logger="topos.permissions_v2.shadow_rescore")
    shadow_labelers.register("local", _Assessing("unresolved", reason))
    result = shadow_rescore.rescore(object(), _request(request_id="req-secret-77"))
    assert result.verdict == "unresolved" and result.reason == "labeler_unresolved"
    assert "not a code" in caplog.text and "unresolved, labeler_unresolved" in caplog.text
    if isinstance(reason, str) and reason.strip():
        assert reason not in caplog.text
    assert "PRIVATE CONTENT" not in caplog.text and "req-secret-77" not in caplog.text


# --------------------------------------------------------------------------- S4


def test_S4_a_labeler_that_only_scores_is_still_labeler_unresolved_and_a_verdict_carries_no_reason(monkeypatch):
    monkeypatch.setattr(shadow_rescore, "resolve_records", lambda runtime, request: _records(WORK_NOTE))
    monkeypatch.setattr(shadow_rescore, "resolve_policy", lambda runtime, request: _policy())
    shadow_labelers.register("local", _ScoringOnly())
    result = shadow_rescore.rescore(object(), _request())
    assert result.verdict == "unresolved" and result.reason == "labeler_unresolved"
    for verdict in ("agree", "candidate_miss"):
        shadow_labelers.register("local", _Assessing(verdict, "labeler_unreachable"))
        result = shadow_rescore.rescore(object(), _request())
        assert result.verdict == verdict and result.reason is None
    # An assessment outside the vocabulary is still the seam's own refusal.
    shadow_labelers.register("local", _Assessing("miss", None))
    assert shadow_rescore.rescore(object(), _request()).reason == "labeler_verdict_invalid"
    shadow_labelers.register("local", _Assessing(None, "labeler_unreachable"))
    assert shadow_rescore.rescore(object(), _request()).reason == "labeler_verdict_invalid"


# --------------------------------------------------------------------------- S5


def test_S5_the_real_labelers_code_reaches_the_reply_end_to_end(monkeypatch):
    monkeypatch.setattr(shadow_rescore, "resolve_records", lambda runtime, request: _records(WORK_NOTE))
    monkeypatch.setattr(shadow_rescore, "resolve_policy", lambda runtime, request: _policy())
    with _FakeOllama(digest="0" * 64) as host:
        shadow_labelers.register("local", _labeler_at(host))
        result = shadow_rescore.rescore(object(), _request())
    assert result.verdict == "unresolved" and result.reason == "labeler_model_unreviewed"
    assert result.labeler == local.LABELER_ID and result.family == "qwen"
    with _FakeOllama() as host:
        shadow_labelers.register("local", _labeler_at(host))
        result = shadow_rescore.rescore(object(), _request())
    assert result.verdict == "agree" and result.reason is None
    assert host.paths == [("GET", "/api/tags"), ("POST", "/api/chat")]
