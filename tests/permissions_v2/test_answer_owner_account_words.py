"""A2A-4 amendment 10: the control plane attests the share owner's account name words with each ask.

The shape found on a live node (12 Oct 2026, 1.5.6): the entity the owner attested as themselves does not carry
their first name, and several other entries and a contact card do (duplicates nobody merged). So the first name could
never be an owner word, "What has <first name> been working on this week?" bound it as another person's name, no
first-person item carries it, and every sentence dropped (`all_sentences_dropped`, `dropped_relevance`). The same
question worded "the owner" was answered.

Now the words the control plane signs for the ask count as the owner's even when other entries carry them. Nothing
else moves: another capitalised name still binds, an Off-limits word is never the owner's, and a field that is not
exactly the grammar refuses the ask. Invented names only.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.permissions_v2.message_search_harness import owner, recipient
from tests.permissions_v2.test_answer_release import _finished, _service
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from topos.permissions_v2 import answer_transport, owner_account_words as oaw
from topos.permissions_v2.answer_generation import (_name_terms, account_words_as_asked, build_prompt,
                                                    owner_party_words, people_words,
    post_check_answer)
from topos.permissions_v2.answer_protocol import ASK
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.knowledge_contract import MessageResult

MODES = ("only", "with_sources")
ACCOUNT = frozenset({"ivo", "keller"})
QUESTION = "What has Ivo been working on this week?"
SENTENCE = "Ivo spent the week on the lantern project [1]."


class Open:
    def mentions_protected(self, *texts):
        return False


class Protects:
    def __init__(self, word):
        self.word = word

    def mentions_protected(self, *texts):
        return any(self.word in text.lower() for text in texts)


def _message(letter, text):
    rid = "r." + letter * 64
    return MessageResult.parse({"kind": "message", "record_id": rid, "content": text,
        "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport", "content": text}]})


ITEM = _message("a", "I spent this week working on the lantern project for the studio.")
MARA = _message("b", "Mara and I walked the ridge path on Sunday.")


def _node(*, off_limits=None):
    """The live shape: the attested self entity has no first name; other entries and a contact card carry it."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT, canonical_name TEXT, aliases_json TEXT,
            is_self INTEGER, contact_id TEXT);
        CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT, known_usernames_json TEXT,
            is_self INTEGER);
        CREATE TABLE entity_blackholes (blackhole_id TEXT, entity_id TEXT, normalized_name TEXT, canonical_name TEXT,
            aliases_json TEXT);
    """)
    conn.execute("INSERT INTO entities VALUES ('ent-self','person','Keller Studio Notes','[]',1,NULL)")
    for number, name in enumerate(("Ivo", "Ivo Keller", "Ivo K", "Ivo (studio)"), 1):   # duplicates nobody merged
        conn.execute("INSERT INTO entities VALUES (?,?,?,?,0,NULL)", (f"ent-dup-{number}", "person", name, "[]"))
    conn.execute("INSERT INTO entities VALUES ('ent-mara','person','Mara Voss','[]',0,NULL)")
    conn.execute("INSERT INTO contacts VALUES ('c-ivo','Ivo Keller','[]',0)")
    if off_limits:
        conn.execute("INSERT INTO entity_blackholes VALUES ('b1','',?,?,'[]')", (off_limits.lower(), off_limits))
    return conn


@pytest.fixture
def attested(monkeypatch):
    from topos.permissions_v2 import identity
    monkeypatch.setattr(identity, "attested_subjects", lambda conn: {"ent-self"})


def _answer(conn, question, records, sentence, mode, *, account=frozenset(), boundary=None):
    boundary = boundary or Open()
    account = account_words_as_asked(question, account)     # as the service does: only where written as a name
    words = owner_party_words(conn, boundary, account_words=account) if account else owner_party_words(conn, boundary)
    people = people_words(conn)
    prompt = build_prompt(question, list(records), precision="none", owner_words=words, people=people,
                          shared_owner_words=account & words & people)
    return prompt, post_check_answer(sentence, list(records), prompt, mode=mode, boundary=boundary)


# --- the name rule -------------------------------------------------------------------------------------------------

def test_the_live_shape_drops_every_sentence_without_the_account_words(attested):
    conn = _node()
    assert "ivo" not in owner_party_words(conn, Open()) and "ivo" in people_words(conn)
    for mode in MODES:
        prompt, checked = _answer(conn, QUESTION, [ITEM], SENTENCE, mode)
        assert prompt.name_terms == {"ivo"}
        assert (checked.body.outcome, checked.reason, checked.dropped_relevance) == (
            "no_answer", "all_sentences_dropped", 1), mode
        assert "is the owner" not in prompt.user


def test_the_account_words_make_the_owners_first_name_the_owner(attested):
    conn = _node()
    words = owner_party_words(conn, Open(), account_words=ACCOUNT)
    assert {"ivo", "keller"} <= words
    for mode in MODES:
        prompt, checked = _answer(conn, QUESTION, [ITEM], SENTENCE, mode, account=ACCOUNT)
        assert prompt.name_terms == frozenset()
        assert (checked.body.outcome, checked.reason, checked.kept, checked.dropped_relevance) == (
            "answered", "answered", 1, 0), mode
        # The model is told, in the asker's own word, as for a confirmed name; the question stays as asked.
        assert "In this question, Ivo is the owner, who wrote every item." in prompt.user
        assert QUESTION in prompt.user and prompt.asked == QUESTION


@pytest.mark.parametrize("question, expected", [
    ("What has Ivo been working on this week?", {"ivo"}),
    ("what has ivo been working on this week?", set()),              # never written as a name
    ("Ivo: what did ivo finish?", set()),                            # written both ways: read the old way
    ("What did Keller and Ivo plan?", {"ivo", "keller"}),
    ("What is in the garden?", set()),
    ("IVO, what are you working on?", {"ivo"}),
])
def test_an_account_word_is_the_owner_only_where_the_asker_wrote_it_as_a_name(question, expected):
    assert account_words_as_asked(question, ACCOUNT | {"garden"}) == expected


def test_a_lower_case_account_word_is_a_plain_word_of_the_question(attested):
    """An address such as "garden@" attests "garden": a question about a garden is not a question about the owner."""
    conn = _node()
    account = ACCOUNT | {"lantern"}
    for mode in MODES:
        prompt, checked = _answer(conn, "What happened with the lantern project?", [ITEM],
                                  "The lantern project took the week [1].", mode, account=account)
        assert "is the owner" not in prompt.user and "lantern" in prompt.question.casefold()
        assert checked.body.outcome == "answered", mode
        # In lower case the owner's own first name is, as before, a known person's word and binds.
        prompt, checked = _answer(conn, "what has ivo been working on this week?", [ITEM], SENTENCE, mode,
                                  account=account)
        assert prompt.name_terms == {"ivo"} and checked.body.outcome == "no_answer", mode


NAMESAKE = _message("c", "Ivo Brandt told me on Monday that he is leaving the bakery.")


@pytest.mark.parametrize("sentence, outcome", [
    ("Ivo worked on the lantern project and is leaving the bakery [1, 2].", "no_answer"),   # the owner's and a namesake's
    ("Ivo spent the week on the lantern project [1].", "answered"),                        # the owner's own item
    ("Ivo is leaving the bakery [2].", "answered"),                                        # the item that names him
])
def test_a_sentence_never_mixes_the_owners_items_with_a_namesakes(attested, sentence, outcome):
    """Review R-157, finding 1: the node knows another Ivo. An item naming him never lends him the owner's facts."""
    conn = _node()
    for mode in MODES:
        _prompt, checked = _answer(conn, "What has Ivo been doing?", [ITEM, NAMESAKE], sentence, mode, account=ACCOUNT)
        assert checked.body.outcome == outcome, (mode, sentence)


def test_the_mixing_rule_needs_another_carrier_on_this_node(attested):
    """An account word nobody else here carries is only the owner's: nothing to mix."""
    conn = _node()
    conn.execute("DELETE FROM entities WHERE entity_id LIKE 'ent-dup-%'")
    conn.execute("DELETE FROM contacts")
    assert "ivo" not in people_words(conn)
    prompt, checked = _answer(conn, "What has Ivo been doing?", [ITEM, NAMESAKE],
                              "Ivo worked on the lantern project [1].", "only", account=ACCOUNT)
    assert prompt.shared_owner_words == frozenset() and checked.body.outcome == "answered"


def test_no_account_words_is_exactly_the_rule_of_before(attested):
    conn = _node()
    assert owner_party_words(conn, Open(), account_words=frozenset()) == owner_party_words(conn, Open())
    assert owner_party_words(conn, account_words=frozenset()) == owner_party_words(conn)


def test_another_capitalised_name_in_the_same_question_still_binds(attested):
    conn = _node()
    question = "What have Ivo and Mara been working on this week?"
    words = owner_party_words(conn, Open(), account_words=ACCOUNT)
    assert _name_terms(question, words, people_words(conn)) == {"mara"}
    for mode in MODES:
        # The owner's own item does not name Mara: it cannot answer a question about her.
        _prompt, checked = _answer(conn, question, [ITEM], "Ivo and Mara built the lantern project [1].", mode,
                                   account=ACCOUNT)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode
        # An item that names her can.
        _prompt, checked = _answer(conn, "What did Ivo do with Mara on Sunday?", [MARA],
                                   "They walked the ridge path together [1].", mode, account=ACCOUNT)
        assert checked.body.outcome == "answered", mode
    # A name nobody on the node carries binds too, as before.
    assert _name_terms("What has Ivo told Quillon?", words, people_words(conn)) == {"quillon"}


def test_a_word_the_off_limits_boundary_protects_is_never_an_owner_word(attested):
    conn = _node()
    for boundary in (Protects("ivo"), Protects("keller")):
        words = owner_party_words(conn, boundary, account_words=ACCOUNT)
        assert boundary.word not in words
        assert (ACCOUNT - {boundary.word}) <= words


def test_a_word_of_an_off_limits_entry_is_never_an_owner_word_even_when_the_boundary_is_silent(attested):
    conn = _node(off_limits="Ivo Brandt")
    words = owner_party_words(conn, Open(), account_words=ACCOUNT)
    assert "ivo" not in words and "keller" in words
    for mode in MODES:
        _prompt, checked = _answer(conn, QUESTION, [ITEM], SENTENCE, mode, account=ACCOUNT)
        assert checked.body.outcome == "no_answer", mode


@pytest.mark.parametrize("word", ["will", "may", "hello", "owner", "work", "home", "plans", "journal", "chatgpt",
                                  "message", "share"])
def test_an_account_word_that_is_a_word_of_the_question_rules_is_never_an_owner_word(attested, word):
    """An address like "hello@" or "work@" names nobody: the starters, the request words, the category words and the
    source families keep the meaning the rules give them."""
    assert word not in owner_party_words(_node(), Open(), account_words=frozenset({word, "ivo"}))
    assert "ivo" in owner_party_words(_node(), Open(), account_words=frozenset({word, "ivo"}))


def test_account_words_never_widen_the_confirmed_names_rule(attested):
    """Only the attested words are excepted from "a word another party carries": the node's own words still are not."""
    conn = _node()
    conn.execute("INSERT INTO entities VALUES ('ent-other','person','Studio Brandt','[]',0,NULL)")
    words = owner_party_words(conn, Open(), account_words=ACCOUNT)
    assert "studio" not in words and "notes" in words and "ivo" in words


def test_a_store_that_cannot_be_read_gives_no_words():
    conn = sqlite3.connect(":memory:")
    conn.close()
    assert owner_party_words(conn, Open(), account_words=ACCOUNT) == frozenset()


# --- the grammar ---------------------------------------------------------------------------------------------------

KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
PUBLIC = KEY.public_key().public_bytes_raw()


def _stamp(words=("ivo", "keller"), msg_id="req-1", msg_type=oaw.MESSAGE_TYPE):
    return oaw.sign_stamp(list(words), msg_id=msg_id, msg_type=msg_type, signer=KEY)


def test_the_stamp_verifies_for_its_own_message_only():
    stamp = _stamp()
    assert set(stamp) == {"v", "words", "sig"} and stamp["v"] == 1
    assert oaw.verify_stamp(stamp, msg_id="req-1", msg_type=oaw.MESSAGE_TYPE, key=PUBLIC) == ACCOUNT
    for kwargs in ({"msg_id": "req-2", "msg_type": oaw.MESSAGE_TYPE, "key": PUBLIC},
                   {"msg_id": "req-1", "msg_type": "permissions_v2_answer_fetch", "key": PUBLIC},
                   {"msg_id": "req-1", "msg_type": oaw.MESSAGE_TYPE,
                    "key": Ed25519PrivateKey.from_private_bytes(bytes(32)).public_key().public_bytes_raw()},
                   {"msg_id": "req-1", "msg_type": oaw.MESSAGE_TYPE, "key": None},
                   {"msg_id": "req-1", "msg_type": oaw.MESSAGE_TYPE, "key": b"short"}):
        with pytest.raises(PolicyError) as refused:
            oaw.verify_stamp(stamp, **kwargs)
        assert refused.value.code == oaw.INVALID


def test_the_signed_bytes_are_pinned():
    assert oaw.signing_bytes(["ivo", "keller"], msg_id="req-1", msg_type=oaw.MESSAGE_TYPE) == (
        b'topos-owner-account-words/v1\n'
        b'{"msg_id":"req-1","msg_type":"permissions_v2_answer_submit","v":1,"words":["ivo","keller"]}')
    assert _stamp()["sig"] == base64.urlsafe_b64encode(KEY.sign(oaw.signing_bytes(
        ["ivo", "keller"], msg_id="req-1", msg_type=oaw.MESSAGE_TYPE))).decode().rstrip("=")
    assert (oaw.FIELD, oaw.VERSION, oaw.MAX_WORDS, oaw.MIN_LETTERS, oaw.MAX_LETTERS) == (
        "owner_account_stamp", 1, 8, 2, 32)


MALFORMED_WORDS = [
    [], "ivo", None, {"ivo": 1}, ["Ivo"], ["ivo", "ivo"], ["keller", "ivo"], ["i"], ["x" * 33], ["ivo2"], ["ivo keller"],
    ["ivo-k"], ["ivo_k"], ["ivo."], [""], [7], [None], ["ivo", 7], ["ｉｖｏ"], ["ab", "cd", "ef", "gh", "ij",
    "kl", "mn", "op", "qr"], ["ivo\n"], ["iv́o"],
]


@pytest.mark.parametrize("words", MALFORMED_WORDS, ids=repr)
def test_malformed_or_oversized_words_are_refused(words):
    with pytest.raises(PolicyError) as refused:
        oaw.parse_words(words)
    assert refused.value.code == oaw.INVALID
    with pytest.raises(PolicyError):
        oaw.signing_bytes(words, msg_id="req-1", msg_type=oaw.MESSAGE_TYPE)
    stamp = {**_stamp(), "words": words}
    with pytest.raises(PolicyError):
        oaw.verify_stamp(stamp, msg_id="req-1", msg_type=oaw.MESSAGE_TYPE, key=PUBLIC)


def test_a_stamp_that_is_not_exactly_the_grammar_is_refused():
    good = _stamp()
    for bad in (None, [], "stamp", {}, {**good, "extra": 1}, {k: v for k, v in good.items() if k != "v"},
                {**good, "v": 2}, {**good, "v": True}, {**good, "v": "1"}, {**good, "sig": good["sig"] + "="},
                {**good, "sig": good["sig"][:-1]}, {**good, "sig": 7}, {**good, "sig": "A" * 86},
                {**good, "words": ["ivo"]}, {**good, "words": ["ivo", "keller", "mara"]}):
        with pytest.raises(PolicyError) as refused:
            oaw.verify_stamp(bad, msg_id="req-1", msg_type=oaw.MESSAGE_TYPE, key=PUBLIC)
        assert refused.value.code == oaw.INVALID, bad


def test_the_longest_and_the_fullest_lists_are_accepted():
    words = sorted({"ab", "cd", "ef", "gh", "ij", "kl", "mn", "x" * 32})
    assert oaw.parse_words(words) == tuple(words)
    assert oaw.parse_words(["zoë"]) == ("zoë",)


@pytest.mark.parametrize("address,names,expected", [
    ("ivo.keller@example.test", (), ("ivo", "keller")),
    ("Ivo_Keller42+shares@example.test", (), ("ivo", "keller")),
    ("i.keller@example.test", ("Ivo",), ("ivo",)),                      # a name: the address is not read
    ("mara.ivo@example.test", ("Ivo Keller",), ("ivo", "keller")),      # nor a second person's name in it
    ("ik@example.test", ("Ivo  Keller-Voss",), ("ivo", "keller", "voss")),
    (None, ("Ivo", "Keller"), ("ivo", "keller")),
    ("1234@example.test", (), ()),
    ("not an address", (), ()),
    (None, (), ()),
    ("a.b@example.test", ("X",), ()),
    ("zz@example.test", ("Aa Bb Cc Dd Ee Ff Gg Hh Ii",), ("aa", "bb", "cc", "dd", "ee", "ff", "gg", "hh")),
])
def test_words_from_an_account(address, names, expected):
    words = oaw.words_from_account(address, names)
    assert words == expected
    if words:
        assert oaw.parse_words(list(words)) == words


# --- the transport and the service ---------------------------------------------------------------------------------

class Socket:
    def __init__(self):
        self.frames = []

    async def send(self, frame):
        self.frames.append(json.loads(frame))


def _dispatch(monkeypatch, message, *, key=PUBLIC):
    seen = []

    class Service:
        def submit(self, **kwargs):
            seen.append(("submit", kwargs))
            return {"signed": True}, {"state": "pending"}

        def fetch(self, **kwargs):
            seen.append(("fetch", kwargs))
            raise PolicyError("permission_denied")

    monkeypatch.setattr(answer_transport, "verify_relay_stamp", lambda _message: SimpleNamespace(
        cls="third_party", channel="cp_relay", acting_user="person", client_id="topos-app"))
    monkeypatch.setattr(answer_transport, "get_runtime", lambda: SimpleNamespace(answers=lambda: Service()))
    if key is None:
        monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY", raising=False)
        monkeypatch.setattr("topos.relay_stamp._load_public_key_bytes", lambda: None)
    else:
        monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(key).decode())
    socket = Socket()
    asyncio.run(answer_transport.dispatch_answer(socket, message))
    return socket.frames[0], seen


def _submit(**extra):
    return {"id": "req-1", "type": answer_transport.SUBMIT_TYPE, "payload": {"envelope": {}, "intent": {}}, **extra}


def test_an_ask_without_the_field_reaches_the_service_with_no_words(monkeypatch):
    frame, seen = _dispatch(monkeypatch, _submit())
    assert frame["status"] == "ok"
    assert seen[0][1]["owner_account_words"] == frozenset()


def test_an_ask_with_a_verified_field_reaches_the_service_with_its_words(monkeypatch):
    frame, seen = _dispatch(monkeypatch, _submit(owner_account_stamp=_stamp()))
    assert frame["status"] == "ok"
    assert seen[0][1]["owner_account_words"] == ACCOUNT


@pytest.mark.parametrize("stamp", [
    None, {}, "ivo", {"v": 1, "words": ["ivo"], "sig": "A" * 86}, {**_stamp(), "words": ["mara"]},
    _stamp(msg_id="another-request"), {**_stamp(), "words": ["Ivo", "keller"]}, {**_stamp(), "more": True},
], ids=["null", "empty", "string", "unsigned", "other-words", "other-message", "upper-case", "extra-key"])
def test_an_ask_whose_field_is_malformed_or_does_not_verify_is_refused(monkeypatch, stamp):
    frame, seen = _dispatch(monkeypatch, _submit(owner_account_stamp=stamp))
    assert frame == {"id": "req-1", "type": answer_transport.SUBMIT_TYPE, "status": "error", "code": 403,
                     "error": "permission_denied"}
    assert seen == []


def test_an_ask_with_the_field_is_refused_on_a_node_that_holds_no_stamp_key(monkeypatch):
    frame, seen = _dispatch(monkeypatch, _submit(owner_account_stamp=_stamp()), key=None)
    assert frame["status"] == "error" and seen == []


def test_a_fetch_never_carries_the_field(monkeypatch):
    message = {"id": "req-1", "type": answer_transport.FETCH_TYPE, "payload": {"envelope": {}, "intent": {}},
               "owner_account_stamp": _stamp()}
    frame, seen = _dispatch(monkeypatch, message)
    assert frame["status"] == "error" and seen == []


def test_the_field_is_no_part_of_the_closed_payload(monkeypatch):
    """The relay payload stays exactly {envelope, intent}: the asker's intent has no room for words."""
    frame, seen = _dispatch(monkeypatch, {"id": "req-1", "type": answer_transport.SUBMIT_TYPE, "payload": {
        "envelope": {}, "intent": {}, "owner_account_stamp": _stamp()}})
    assert frame["status"] == "error" and seen == []
    from topos.permissions_v2.answer_protocol import AskIntent
    with pytest.raises(PolicyError):
        AskIntent.parse({"question": QUESTION, "owner_account_words": ["ivo"]})


def _ask(node, service, question, **kwargs):
    payload = {"question": question}
    signed = node._envelope("grant-search", ASK, payload, node.next_id("answer"))
    with recipient():
        _result, output = service.submit(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id,
                                         **kwargs)
    return output["answer_id"]


def test_the_service_carries_the_words_from_the_ask_to_the_name_rule(legacy, tmp_path, monkeypatch):  # noqa: F811
    """Through the real ledger, retrieval and checks: the same ask, without the words and with them."""
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "Ivo is working on a synthetic task [1].")
    question = "What synthetic message has Ivo written?"
    try:
        assert _finished(node, service, _ask(node, service, question))["outcome"] == "no_answer"
        body = _finished(node, service, _ask(node, service, question, owner_account_words=ACCOUNT))
        assert body["outcome"] == "answered" and set(body) == {"version", "outcome", "answer"}
        # Review R-157, finding 3: the service itself reads an account word as the owner only where the asker wrote it
        # as a name at every place. Written both ways, the words change nothing: the rule of before, no answer.
        both_ways = "Ivo: what synthetic message has ivo written?"
        assert _finished(node, service, _ask(node, service, both_ways))["outcome"] == "no_answer"
        assert _finished(node, service, _ask(node, service, both_ways, owner_account_words=ACCOUNT))["outcome"] == "no_answer"
        with pytest.raises(PolicyError):
            _ask(node, service, question, owner_account_words=frozenset({"Ivo"}))
        with pytest.raises(PolicyError):
            _ask(node, service, question, owner_account_words=["ivo"])
    finally:
        service.close()
