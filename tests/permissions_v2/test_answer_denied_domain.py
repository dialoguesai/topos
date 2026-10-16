"""Lane G1 (16 Oct 2026, narrow first): a question about a domain the grant denies gets no answer.

Seen on the grantee density fixture (synthetic owner): a share denies health, family, finance, relationships and home;
the recipient asks about the owner's health appointments; retrieval returns only permitted work notes; the writer
describes them and the support judge keeps each sentence because it is true of its item. No leak, but padding.

`answer_domain.asks_denied_domain` is the pure decision (the offline replay calls it); `answer_domain.policy_domains`
reads the grant's denied and permitted domains from its signed rules. `ANSWER_DENIED_DOMAIN_REFUSAL` (off) puts both
before the model in `AnswerService._compute`, beside the anchor rule, with that rule's reason `question_not_supported`.

The four conditions, each pinned by a check below and by a mutant that reverts it:
  1. the grant denies a domain (`test_a_policy_with_no_denied_domain_never_refuses`);
  2. the question names a denied domain (`check_a_question_with_no_denied_word_is_answered`);
  3. that name is no permitted domain's (`check_a_denied_word_a_permitted_domain_also_has_is_answered`);
  4. no permitted item the job retrieved carries it (`check_a_health_word_the_items_carry_is_answered`).
The switch: `check_switch_off_never_refuses`. Names only, never the catalog's phrase words (the coordinator's ruling
after an offline replay, 16 Oct 2026): `check_catalog_phrase_words_are_answered` and, through the service,
`check_on_catalog_phrase_words_reach_the_model`.

Synthetic data only: every item, question and name here is invented.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from topos.permissions_v2 import answer_domain, answer_release, switches
from topos.permissions_v2.answer_generation import CATEGORY_TOPICS, build_prompt
from topos.permissions_v2.answer_protocol import ASK, FETCH, verify_node_answer
from topos.permissions_v2.knowledge_contract import MessageResult
from topos.permissions_v2.registry import parse_policy

P = switches.PREFIX
SWITCH = switches.ANSWER_DENIED_DOMAIN_REFUSAL.name
#: The density fixture's share: these five denied, the rest permitted.
DENIED = frozenset({"health", "family", "finance", "relationships", "home"})
PERMITTED = frozenset({"work", "plans", "hobbies"})
#: The commit this lane starts from (the final 1.5.10 quality candidate): the switch-off guard compares against it.
BASE = "9ddc77b5c63b06215273f7e225c7b3a0e7027a2d"
NO_ANSWER = {"version": "topos-answer/v1", "outcome": "no_answer"}


def _message(digit, text, source="ownerimport"):
    rid = "r." + digit * 64
    return MessageResult.parse({"kind": "message", "record_id": rid, "content": text,
                                "source_ids": [source], "citations": [{"record_id": rid, "source_id": source,
                                                                      "content": text}]})


TRAM = _message("1", "Audited twelve tram stops on the east line and logged every broken shelter.")
TIMETABLE = _message("2", "Drafted the new timetable boards for the depot and sent them to the printer.")
TOMATOES = _message("3", "Re-potted the balcony tomatoes and started a seed swap list.")
SKETCHES = _message("4", "Spent Sunday on harbour sketches in charcoal.")
CLINIC_SIGNS = _message("5", "Painted the wayfinding signs for the health clinic entrance on Elm Row.")
CLINIC_VINYL = _message("6", "Ordered vinyl lettering for the clinic signage and checked the font sizes.")


def _texts(question, records, domains):
    """What the service hands the decision: the anchor check's own texts of the prompt it built."""
    prompt = build_prompt(question, list(records), precision="none", item_domains=[tuple(domains)] * len(records))
    return answer_domain.prompt_item_texts(prompt)


def _work(question):
    return _texts(question, [TRAM, TIMETABLE], ("work",))


@pytest.fixture(autouse=True)
def switch_unset(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


# --- the pure decision: each check takes the decision under test, so a mutant can be run through it ------------------

HEALTH = "What health appointments and treatment does the owner have?"


def check_a_health_question_over_work_items_is_refused(decide):
    assert decide(HEALTH, _work(HEALTH), DENIED, PERMITTED) is True
    assert decide("WHAT ABOUT THE OWNER'S HEALTH?", _work("x"), DENIED, PERMITTED) is True     # case folded


def check_a_finance_question_over_hobby_items_is_refused(decide):
    question = "What do the owner's finances look like this month?"
    texts = _texts(question, [TOMATOES, SKETCHES], ("hobbies",))
    assert decide(question, texts, DENIED, PERMITTED) is True         # "finances" is "finance" one suffix on


def check_a_health_word_the_items_carry_is_answered(decide):
    """Condition 4: a work question that names a denied domain's word the permitted items themselves carry."""
    question = "What signs did the owner make for the health clinic?"
    carried = _texts(question, [CLINIC_SIGNS, CLINIC_VINYL], ("work",))
    assert decide(question, carried, DENIED, PERMITTED) is False
    # The control: the same question over items that never say the word is refused.
    assert decide(question, _texts(question, [CLINIC_VINYL, TRAM], ("work",)), DENIED, PERMITTED) is True


def check_a_question_with_no_denied_word_is_answered(decide):
    """Condition 2: no word of a denied domain, so never refused, even when no item carries any of its words (the
    anchor rule's business, not this one's)."""
    for question in ("Which recipes has she baked lately?", "What did the owner audit on the tram line?",
                     "What did the owner find out?",                  # "out" is no fold of the plans word "outings"
                     "Which healthcare workshop did the owner attend?",     # whole words only: not "health"
                     "Did the homework club meet?"):                   # nor "home"
        assert decide(question, _work(question), DENIED | {"plans"}, PERMITTED - {"plans"}) is False, question


#: A catalog where "events" belongs to two domains (the shipped one gives it to family alone).
WIDER = {**CATEGORY_TOPICS, "plans": frozenset({"outings", "events"})}


def check_a_denied_word_a_permitted_domain_also_has_is_answered(decide):
    """Condition 3: an ambiguous word never refuses (with a catalog opted in, where a word can belong to two
    domains; and with the same domain on both sides)."""
    question = "What events is the owner going to?"
    texts = _work(question)
    assert decide(question, texts, DENIED, PERMITTED, topics=WIDER) is False
    assert decide(question, texts, DENIED, PERMITTED, topics=CATEGORY_TOPICS) is True   # control: family's word alone
    # The same domain on both sides is ambiguous too (`policy_domains` never hands this: a deny wins in the release).
    assert decide(HEALTH, _work(HEALTH), frozenset({"health"}), frozenset({"health", "work"})) is False
    assert decide(HEALTH, _work(HEALTH), frozenset({"health"}), frozenset({"work"})) is True


#: Questions about permitted things that use a denied domain's catalog phrase word (family's "events", relationships'
#: "people", home's "staying"), over items that never say it.
PHRASE_WORD_QUESTIONS = ("What events has the owner organised?", "Which people has the owner worked with?",
                         "Where is the owner staying for the depot audit?")


def check_catalog_phrase_words_are_answered(decide):
    """Names only by default: a denied domain's catalog phrase word is no denied-domain word. With the catalog opted
    in the same questions are refused, so it is the default that answers them."""
    for question in PHRASE_WORD_QUESTIONS:
        texts = _work(question)
        assert decide(question, texts, DENIED, PERMITTED) is False, question
        assert decide(question, texts, DENIED, PERMITTED, topics=CATEGORY_TOPICS) is True, question


def test_catalog_phrase_words_are_answered():
    check_catalog_phrase_words_are_answered(answer_domain.asks_denied_domain)


@pytest.mark.parametrize("question", [
    "Who is in the owner's family?", "Which families did the owner visit?", "How are the owner's finances?",
    "What about the owner's finance plans?", "What homes did the owner view?", "Is the owner's home warm?",
    "How are the owner's relationships?", "Is the owner in a relationship?", "Any health news?"])
def test_each_denied_domain_name_refuses_as_written_and_one_suffix_on(question):
    assert answer_domain.asks_denied_domain(question, _work(question), DENIED, PERMITTED) is True


def test_a_health_question_over_work_items_is_refused():
    check_a_health_question_over_work_items_is_refused(answer_domain.asks_denied_domain)


def test_a_finance_question_over_hobby_items_is_refused():
    check_a_finance_question_over_hobby_items_is_refused(answer_domain.asks_denied_domain)


def test_a_finance_question_with_no_word_of_the_catalog_is_not_refused():
    """A limit, pinned and kept narrow (the coordinator's ruling): "spend" and "rent" are not the domain's name, and
    no catalog has them either (finance's phrase has no words; "spend" is no domain's word, R-RULES F5). So this
    question is answered, with the catalog opted in too; no free synonym is added for it."""
    question = "What does the owner spend on rent?"
    texts = _texts(question, [TOMATOES, SKETCHES], ("hobbies",))
    assert answer_domain.asks_denied_domain(question, texts, DENIED, PERMITTED) is False
    assert answer_domain.asks_denied_domain(question, texts, DENIED, PERMITTED, topics=CATEGORY_TOPICS) is False


def test_a_health_word_the_items_carry_is_answered():
    check_a_health_word_the_items_carry_is_answered(answer_domain.asks_denied_domain)


def test_a_question_with_no_denied_word_is_answered():
    check_a_question_with_no_denied_word_is_answered(answer_domain.asks_denied_domain)


def test_a_denied_word_a_permitted_domain_also_has_is_answered():
    check_a_denied_word_a_permitted_domain_also_has_is_answered(answer_domain.asks_denied_domain)


def test_a_policy_with_no_denied_domain_never_refuses():
    """Condition 1."""
    for question in (HEALTH, "What do the owner's finances look like?", "Who is in the owner's family?"):
        assert answer_domain.asks_denied_domain(question, _work(question), frozenset(), PERMITTED) is False
        assert answer_domain.asks_denied_domain(question, [], frozenset(), frozenset()) is False


def test_the_items_read_are_the_anchor_checks_bodies_kinds_domain_evidence_and_sources():
    """Condition 4 reads what `question_lacks_permitted_anchor` reads: a word in an item's domain evidence (its
    domain's name and phrase words) or its kind counts as carried."""
    prompt = build_prompt("x", [TRAM], precision="none", item_domains=[("work",)])
    texts = answer_domain.prompt_item_texts(prompt)
    assert texts == list(prompt.raw_texts + prompt.domain_texts + prompt.source_texts)
    assert "work projects" in texts and "message" in texts
    # "work" is in no body, only in the evidence: carried, so never refused even with work denied; without the
    # evidence it would be.
    assert answer_domain.asks_denied_domain("What work?", texts, frozenset({"work"}), frozenset()) is False
    assert answer_domain.asks_denied_domain("What work?", list(prompt.raw_texts), frozenset({"work"}),
                                            frozenset()) is True


# --- what the grant denies and permits, read from its signed rules ---------------------------------------------------

def _raw(*, deny=True):
    """The fixture's p2c-v3 grant, through JSON: its rules share one sources dict, and a test edits one rule's."""
    from tests.permissions_v2.test_knowledge_search import knowledge_policy
    raw = json.loads(json.dumps(knowledge_policy(answers="only")))
    if not deny:
        raw["rules"] = [rule for rule in raw["rules"] if rule["rule_id"] != "deny-private-domains"]
    return raw


def test_the_fixture_grant_denies_its_deny_rules_domains_and_permits_its_permit_rules():
    """The fixture is the control plane's shape: a permit rule on work and plans with a sensitivity, a deny rule on the
    five private domains, and a deny rule on a sensitivity alone (which denies no domain)."""
    denied, permitted = answer_domain.policy_domains(parse_policy(_raw()))
    assert denied == DENIED
    assert permitted == {"work", "plans"}


def test_a_grant_without_a_domain_deny_rule_denies_nothing():
    denied, permitted = answer_domain.policy_domains(parse_policy(_raw(deny=False)))
    assert denied == frozenset() and permitted == {"work", "plans"}


def test_a_deny_rule_that_does_not_cover_every_permitted_source_denies_nothing():
    """A deny applies to the items of its own sources (`release.source_message_decision`): one that leaves a source
    the permit rule draws on uncovered can let that domain's items through, so it is not read as a denied domain."""
    raw = _raw()
    deny = next(rule for rule in raw["rules"] if rule["rule_id"] == "deny-private-domains")
    other = [source for source in raw["source_universe"]["source_ids"]
             if source not in deny["evidence_use"]["sources"]["values"]]
    assert other
    deny["evidence_use"]["sources"]["values"] = other
    denied, _permitted = answer_domain.policy_domains(parse_policy(raw))
    assert denied == frozenset()


def test_a_negated_deny_reads_as_every_other_domain_denied():
    """Any predicate, not only an atom: "deny what is not work" denies every catalog domain but work."""
    raw = _raw()
    deny = next(rule for rule in raw["rules"] if rule["rule_id"] == "deny-private-domains")
    negated = {"kind": "not", "term": {"kind": "atom", "attribute": "domain", "operator": "intersects",
                                       "values": ["work"]}}
    deny["evidence_use"]["predicate"] = deepcopy(negated)
    deny["release"]["predicate"] = deepcopy(negated)
    denied, permitted = answer_domain.policy_domains(parse_policy(raw))
    assert denied == set(CATEGORY_TOPICS) - {"work"}
    assert permitted == {"work"}


# --- the service: a real signed ask and fetch through the ledger and the share's own retrieval ----------------------

ASKED = "What health appointments did the owner log in the Synthetic message?"
WRITTEN = "The owner is working on a synthetic task [1]."


def _node(legacy, tmp_path, monkeypatch, *, deny=True):
    """`test_knowledge_search.node_for` with answers on, and the grant's domain deny rule kept or dropped."""
    from tests.permissions_v2.test_knowledge_search import Node, answer, canonical_utc_microseconds, mc, owner, \
        publish, setup
    resolver, reviews, identity, prepared = setup(legacy)
    with owner():
        publish(resolver, reviews, prepared, answer(prepared), now=1)
    stamp = legacy[1].execute("SELECT event_at FROM conversation_messages").fetchone()[0]
    now = canonical_utc_microseconds(stamp) // 1000000 + 60
    monkeypatch.setattr(mc, "NOW", now)
    node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), tmp_path / "node",
                model=None, search_raw=_raw(deny=deny), now=now)
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    return node


def _service(node, calls, cls=None):
    async def generate(_prompt, *, deadline):
        calls.append("writer")
        return WRITTEN

    async def judge(sentence, items, *, deadline, question=None, context=None):
        calls.append("judge")
        return True
    runtime = SimpleNamespace(protocol=node.protocol, message_search=lambda: node.search)
    service = (cls or answer_release.AnswerService)(runtime, clock=lambda: node.now[0], generate=generate, judge=judge)
    service._enabled = lambda: True
    return service


def _call(node, service, kind, payload):
    from tests.permissions_v2.message_search_harness import recipient
    signed = node._envelope("grant-search", kind, payload, node.next_id("answer" if kind == ASK else "fetch"))
    with recipient():
        result, output = (service.submit if kind == ASK else service.fetch)(
            envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
    verify_node_answer(result, trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                       envelope=signed, output=output, mode="only", now=node.now[0])
    return signed.request_id, result, output


def _ask(node, service, question):
    """One ask, fetched once its worker ended: (the fetched body, the private receipt, the stored decision row)."""
    request_id, _result, pending = _call(node, service, ASK, {"question": question})
    job = service._jobs[pending["answer_id"]]
    for _ in range(1000):
        if job.state == "ended":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("the answer worker did not finish")
    _fetch_id, _result, body = _call(node, service, FETCH, {"answer_id": pending["answer_id"]})
    with node.ledger._transaction() as db:
        row = db.execute("SELECT receipt_json, decision_json FROM p2a_receipts WHERE request_id=?",
                         (request_id,)).fetchone()
    return body, json.loads(row[0]), row


def check_switch_off_never_refuses(legacy, tmp_path, monkeypatch, cls=None, node=None):
    """Off: the health question reaches the writer and the judge, is answered, and the receipt has no new key. Nothing
    of the rule is read: the policy reader and the decision fail the job if called (`_compute` turns any exception
    into `model_error`, a no_answer)."""
    def never(*_args, **_kwargs):
        raise AssertionError("read with the switch off")
    monkeypatch.setattr(answer_domain, "policy_domains", never)
    monkeypatch.setattr(answer_domain, "asks_denied_domain", never)
    node = node or _node(legacy, tmp_path, monkeypatch)
    calls = []
    service = _service(node, calls, cls)
    try:
        body, receipt, _row = _ask(node, service, ASKED)
    finally:
        service.close()
    assert body["outcome"] == "answered" and receipt["reason"] == "answered", (body, receipt["reason"])
    assert calls[0] == "writer" and "judge" in calls
    assert "refused_denied_domain" not in receipt


def test_on_a_health_question_is_refused_before_the_model_with_the_anchor_rules_reason(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    node = _node(legacy, tmp_path, monkeypatch)
    calls = []
    service = _service(node, calls)
    try:
        body, receipt, row = _ask(node, service, ASKED)
    finally:
        service.close()
    assert body == NO_ANSWER                             # the uniform body: no new field, no word of a domain
    assert calls == []                                   # neither the writer nor the judge ran
    assert receipt["reason"] == "question_not_supported" and receipt["outcome"] == "no_answer"
    assert receipt["refused_denied_domain"] is True
    assert receipt["records_used"] == 1                  # retrieval ran and released as it always does
    # Nothing that leaves the node names a domain: the body is fixed, and the private rows name none of the denied.
    stored = (row[0] + row[1]).casefold()
    assert not any(domain in stored for domain in DENIED)


def test_on_a_question_without_a_denied_word_is_answered_and_the_receipt_says_so(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    node = _node(legacy, tmp_path, monkeypatch)
    calls = []
    service = _service(node, calls)
    try:
        body, receipt, _row = _ask(node, service, "What did the owner write in the Synthetic message?")
    finally:
        service.close()
    assert body["outcome"] == "answered" and calls[0] == "writer"
    assert receipt["refused_denied_domain"] is False


def test_on_a_grant_with_no_denied_domain_never_refuses(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    node = _node(legacy, tmp_path, monkeypatch, deny=False)
    calls = []
    service = _service(node, calls)
    try:
        body, receipt, _row = _ask(node, service, ASKED)
    finally:
        service.close()
    assert body["outcome"] == "answered" and calls[0] == "writer"
    assert receipt["refused_denied_domain"] is False


def check_on_catalog_phrase_words_reach_the_model(legacy, tmp_path, monkeypatch, cls=None, node=None):
    """On: questions that use a denied domain's catalog phrase word ("events", "people"), over the fixture's one item
    that never says either, are written and answered."""
    monkeypatch.setenv(SWITCH, "true")
    node = node or _node(legacy, tmp_path, monkeypatch)
    for question in ("What events has the owner organised in the Synthetic message?",
                     "Which people has the owner worked with on the Synthetic message?"):
        calls = []
        service = _service(node, calls, cls)
        try:
            body, receipt, _row = _ask(node, service, question)
        finally:
            service.close()
        assert receipt["records_used"] == 1, question
        assert body["outcome"] == "answered" and calls[0] == "writer", (question, receipt["reason"])
        assert receipt["refused_denied_domain"] is False


def test_on_catalog_phrase_words_reach_the_model(legacy, tmp_path, monkeypatch):
    check_on_catalog_phrase_words_reach_the_model(legacy, tmp_path, monkeypatch)


def test_off_the_health_question_reaches_the_model_and_nothing_of_the_rule_is_read(legacy, tmp_path, monkeypatch):
    check_switch_off_never_refuses(legacy, tmp_path, monkeypatch)


def test_off_receipts_and_bodies_are_byte_for_byte_those_of_the_commit_before(legacy, tmp_path, monkeypatch):
    """The answer service of the commit this lane starts from and this one, on one node, the same two questions (one
    the rule would refuse, one it would not): the fetched bodies and the stored receipt and decision rows are the same
    bytes, but for the request's own id and hash and the job's random id (each ask is a new signed request)."""
    from tests.permissions_v2 import ft168_support as ft
    before = ft.module_at(BASE, "answer_release")
    assert "ANSWER_DENIED_DOMAIN_REFUSAL" not in before.source_text and before.AnswerService is not \
        answer_release.AnswerService
    node = _node(legacy, tmp_path, monkeypatch)

    def run(cls):
        calls, seen = [], []
        service = _service(node, calls, cls)
        try:
            for question in (ASKED, "What did the owner write in the Synthetic message?"):
                body, receipt, row = _ask(node, service, question)
                for key in ("request_id", "question_hash", "answer_id"):
                    receipt[key] = "-"
                seen.append([body, receipt, row[1]])
        finally:
            service.close()
        return json.dumps({"seen": seen, "calls": calls}, sort_keys=True)
    old, new = run(before.AnswerService), run(None)
    assert json.loads(new)["calls"].count("writer") == 2
    assert new == old


# --- mutants: revert each condition and the switch check in turn; a named check above goes red ---------------------

def _mutated(module, old: str, new: str):
    """`module` with one line of its source replaced, loaded beside the real one in the same package."""
    source = inspect.getsource(module)
    assert source.count(old) == 1, old
    label = f"{module.__name__}_g1_mutant_{abs(hash(old))}"
    mutant = importlib.util.module_from_spec(importlib.util.spec_from_loader(label, loader=None))
    mutant.__package__ = "topos.permissions_v2"
    sys.modules[label] = mutant
    exec(compile(source.replace(old, new), f"<{label}>", "exec"), mutant.__dict__)
    return mutant


MUTANTS = {
    # 2: every word of the question counts as a denied domain's word.
    "condition 2": ("asked = [word for word in _tokens(question) if _within_one_suffix(word, denied_forms)]",
                    "asked = list(_tokens(question))",
                    check_a_question_with_no_denied_word_is_answered),
    # 3: a word a permitted domain also has refuses all the same.
    "condition 3": ("if all(_within_one_suffix(word, permitted_forms) for word in asked):",
                    "if False:",
                    check_a_denied_word_a_permitted_domain_also_has_is_answered),
    # 4: the items are never read.
    "condition 4": ("return not any(_within_one_suffix(word, item_forms) for word in asked)",
                    "return True",
                    check_a_health_word_the_items_carry_is_answered),
    # Names only: the default takes the catalog's phrase words again.
    "names only": ("topics: Mapping[str, Iterable[str]] = NAMES_ONLY) -> bool:",
                   "topics: Mapping[str, Iterable[str]] = CATEGORY_TOPICS) -> bool:",
                   check_catalog_phrase_words_are_answered),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_each_condition_reverted_turns_its_named_check_red(name):
    old, new, check = MUTANTS[name]
    check(answer_domain.asks_denied_domain)                       # green on the real decision
    with pytest.raises(AssertionError):
        check(_mutated(answer_domain, old, new).asks_denied_domain)


def test_the_switch_check_reverted_turns_the_switch_off_check_red(legacy, tmp_path, monkeypatch):
    mutant = _mutated(answer_release, "if switches.on(switches.ANSWER_DENIED_DOMAIN_REFUSAL):", "if True:")
    node = _node(legacy, tmp_path, monkeypatch)
    with monkeypatch.context() as patched:
        with pytest.raises(AssertionError):
            check_switch_off_never_refuses(legacy, tmp_path, patched, mutant.AnswerService, node=node)
    # What the mutant does with the switch unset: the health question is refused, and the real service answers it.
    for cls, reason in ((mutant.AnswerService, "question_not_supported"), (None, "answered")):
        calls = []
        service = _service(node, calls, cls)
        try:
            _body, receipt, _row = _ask(node, service, ASKED)
        finally:
            service.close()
        assert receipt["reason"] == reason and "refused_denied_domain" not in receipt


def test_the_service_given_the_catalog_turns_the_phrase_word_check_red(legacy, tmp_path, monkeypatch):
    """The service passes names only itself; a service that hands the decision the catalog refuses the phrase-word
    questions."""
    mutant = _mutated(answer_release, "denied, permitted, topics=answer_domain.NAMES_ONLY):",
                      "denied, permitted, topics=answer_domain.CATEGORY_TOPICS):")
    node = _node(legacy, tmp_path, monkeypatch)
    check_on_catalog_phrase_words_reach_the_model(legacy, tmp_path, monkeypatch, node=node)    # green on the real one
    with pytest.raises(AssertionError):
        check_on_catalog_phrase_words_reach_the_model(legacy, tmp_path, monkeypatch, mutant.AnswerService, node=node)
