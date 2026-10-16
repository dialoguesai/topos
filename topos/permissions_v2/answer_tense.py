"""A plan stated as a deed: the deterministic tense guard of a grantee answer (lever 4b / G2, 16 Oct 2026).

The dense writer restates what a cited note intends as a thing done. A note says "I will design the shelf labels"
and the answer says "The owner designed the shelf labels [1]"; "she will organise the rota" comes back as
"organised the rota", "I agreed to help record the oral histories" as "helped record". Blind reviewers mark these
false, and they are the largest share of false sentences that are not about dates in the dense arms. A prompt line
alone does not hold (template v8's "A stated intention is not a completed act" is already there), so this is a check.

`plan_stated_as_deed(sentence, cited item texts)` is the whole rule, pure: no switch, no environment, no store, no
clock. The node calls it from `answer_generation.sieve_answer` while `ANSWER_TENSE_GUARD` is on; a lead can call it
offline to replay the rule over answers already written.

- An **intention** in an item is a verb right after one of `INTENTION_MARKERS` ("will", "'ll", "shall", "going to",
  "plan to", "agreed to", "need to", ...), past any of `ADVERBS` ("will also design"). A verb of `HELPERS` lends the
  intention to the verb after it ("agreed to help record": help and record; "will start painting": start and paint),
  "will be organising" is organise, and "design and print" is both. A marker with a negation just before it
  ("don't want to", "not going to") names no intention.
- A **deed** is that verb's past tense or past participle (`deed_forms`: regular -ed, -e+d, a doubled consonant,
  y to ied, -ise and -ize alike, and the closed `IRREGULAR` table), in a sentence or an item, unless it is framed as
  an intention or a possibility there (one of `FRAMING_MARKERS` before it, through only `CHAIN` words: "will have
  designed", "plans to set up"), negated ("has not designed", "never wrote"), or part of a framing marker itself
  ("planned to"). A participle spelled as the base ("run") is a deed only after "has", "have", "had" or "'ve".
- **Drop** when the sentence states a deed of a verb that at least one cited item gives as an intention, and no
  cited item states that verb as done.

Never drops a sentence with no cited item, a present or progressive sentence ("is designing"), a sentence whose own
words frame the act as a plan ("plans to design", "was going to design"), or a deed of a verb no cited item frames
as an intention. `SKIP` verbs (read, cut, let, hit, ...: the past is spelled as the present) are never read. Only
drops: never rewrites or repairs a sentence.

Closed lists, pinned by test (`tests/permissions_v2/test_answer_tense_guard.py`): adding an entry is an amendment.
"""
from __future__ import annotations

import re
import unicodedata

VERSION = "topos-answer-tense/v1"

#: An act named right after one of these, in an item, is an intention ("'ll" is read as "will").
INTENTION_MARKERS: tuple[tuple[str, ...], ...] = (
    ("will",), ("shall",),
    ("going", "to"), ("plan", "to"), ("plans", "to"), ("planning", "to"), ("intend", "to"), ("intends", "to"),
    ("want", "to"), ("wants", "to"), ("hope", "to"), ("aim", "to"), ("mean", "to"),
    ("agreed", "to"), ("offered", "to"), ("promised", "to"), ("need", "to"), ("have", "to"), ("has", "to"),
)
#: A deed after one of these (through `CHAIN` words) is framed as an intention or a possibility, not stated as done:
#: the intention markers, their other tenses and persons, and the modal verbs.
FRAMING_MARKERS: tuple[tuple[str, ...], ...] = INTENTION_MARKERS + (
    ("would",), ("could",), ("should",), ("might",), ("may",), ("must",), ("can",),
    ("planned", "to"), ("intended", "to"), ("intending", "to"), ("wanted", "to"), ("wanting", "to"),
    ("hopes", "to"), ("hoped", "to"), ("hoping", "to"), ("aims", "to"), ("aimed", "to"), ("aiming", "to"),
    ("means", "to"), ("meant", "to"), ("meaning", "to"), ("agree", "to"), ("agrees", "to"), ("offer", "to"),
    ("offers", "to"), ("promise", "to"), ("promises", "to"), ("needs", "to"), ("needed", "to"), ("had", "to"),
    ("decided", "to"), ("decides", "to"), ("expects", "to"), ("expected", "to"), ("arranged", "to"),
)
#: Words that may stand between a marker and its verb ("will also design", "is going to finally write").
ADVERBS = frozenset({"also", "still", "then", "probably", "definitely", "finally", "soon", "just", "really", "actually",
                     "first", "eventually", "hopefully", "maybe"})
#: A verb that lends the intention to the verb after it, past an optional "to" or "and" ("help record", "try to
#: finish", "go and buy", "start painting"). The helper itself is intended too.
HELPERS = frozenset({"help", "try", "start", "begin", "finish", "go", "come"})
#: Between a framing marker and a deed, only these (with the adverbs, the helpers and the intended verbs themselves)
#: keep the deed framed: "will have designed", "will be designed", "plans to design and set up", "wants to get it done".
CHAIN = frozenset({"to", "and", "or", "have", "be", "been", "get", "it", "them"})
#: A deed with one of these up to three words before it in its clause, or "not", "t" or "never" right after it ("did
#: not"), is not stated as done. "t" is the tokenizer's piece of "n't".
NEGATIONS = frozenset({"not", "never", "t", "no", "nor"})
NEGATION_WINDOW = 3
#: The most words a framing marker's reach runs back over.
CHAIN_REACH = 6
#: Before a participle spelled as its base ("has run"): only these make it a deed. "ve" is "'ve".
PERFECT = frozenset({"has", "have", "had", "having", "ve"})
#: Never an intended act: the auxiliaries and a negation after a marker ("will not", "will be").
NOT_ACTS = frozenset({"be", "have", "not", "never", "to", "and", "or", "the", "a", "an"})
#: The past is spelled as the present: never read, whatever a sentence says.
SKIP = frozenset({"read", "cut", "let", "hit", "cost", "hurt", "quit", "shut", "spread", "bet", "burst", "cast",
                  "split"})
#: Base -> (simple past, past participle). Every other verb is regular (`_regular`).
IRREGULAR: dict[str, tuple[str, str]] = {
    "write": ("wrote", "written"), "make": ("made", "made"), "build": ("built", "built"), "send": ("sent", "sent"),
    "buy": ("bought", "bought"), "take": ("took", "taken"), "give": ("gave", "given"), "run": ("ran", "run"),
    "meet": ("met", "met"), "bring": ("brought", "brought"), "get": ("got", "gotten"), "lead": ("led", "led"),
    "draw": ("drew", "drawn"), "choose": ("chose", "chosen"), "begin": ("began", "begun"), "sell": ("sold", "sold"),
    "pay": ("paid", "paid"), "find": ("found", "found"), "hold": ("held", "held"), "teach": ("taught", "taught"),
    "tell": ("told", "told"), "speak": ("spoke", "spoken"), "ride": ("rode", "ridden"), "go": ("went", "gone"),
    "do": ("did", "done"), "leave": ("left", "left"), "keep": ("kept", "kept"), "set": ("set", "set"),
    "put": ("put", "put"), "hang": ("hung", "hung"), "fit": ("fitted", "fitted"), "stick": ("stuck", "stuck"),
    "sweep": ("swept", "swept"), "feed": ("fed", "fed"),
}

_VOWELS = frozenset("aeiou")
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})
_WILL = re.compile(r"(?<=[^\W\d_])'ll\b")
# A word, or a mark that ends a clause (it breaks every reach below: negation, framing, a marker's verb).
_TOKEN = re.compile(r"[^\W\d_]+|[.,;:!?()\[\]\n\u2013\u2014]")


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_APOSTROPHES)
    return _TOKEN.findall(_WILL.sub(" will", folded))


def _word(token: str) -> bool:
    return token[:1].isalpha()


def _regular(base: str) -> set[str]:
    if base.endswith("e"):
        forms = {base + "d"}
    elif len(base) > 2 and base.endswith("y") and base[-2] not in _VOWELS:
        forms = {base[:-1] + "ied"}
    elif base.endswith("c"):
        forms = {base + "ked", base + "ed"}
    else:
        forms = {base + "ed"}
        # Consonant, vowel, consonant: doubled or not, as stress and spelling decide (planned, visited; labelled and
        # labeled). Both are read; a form no one writes matches nothing.
        if (len(base) >= 3 and base[-1] not in _VOWELS and base[-1] not in "wxy" and base[-2] in _VOWELS
                and base[-3] not in _VOWELS):
            forms.add(base + base[-1] + "ed")
    for form in list(forms):
        for one, other in (("ised", "ized"), ("ized", "ised"), ("ysed", "yzed"), ("yzed", "ysed")):
            if form.endswith(one):
                forms.add(form[:-len(one)] + other)
    return forms


def deed_forms(base: str) -> dict[str, bool]:
    """The spellings that state `base` as done, each with whether it needs "has", "have", "had" or "'ve" before it
    (only a participle spelled as the base: "run"). Nothing for a `SKIP` verb."""
    if base in SKIP or base in NOT_ACTS:
        return {}
    if base in IRREGULAR:
        past, participle = IRREGULAR[base]
        forms = {past: False}
        if participle not in forms:
            forms[participle] = participle == base
        return forms
    return {form: False for form in _regular(base)}


def _ing_bases(word: str) -> set[str]:
    """The base a "-ing" form may be read from: organising is organise, planning plan, designing design. A stem
    shorter than three letters is no verb ("bring" is not "br")."""
    stem = word[:-3]
    if not word.endswith("ing") or len(stem) < 3:
        return set()
    bases = {stem, stem + "e"}
    if stem[-1] == stem[-2]:
        bases.add(stem[:-1])
    return bases - NOT_ACTS


def _longest_first(markers) -> tuple[tuple[str, ...], ...]:
    return tuple(sorted(markers, key=len, reverse=True))


_INTENTIONS = _longest_first(INTENTION_MARKERS)
_FRAMINGS = _longest_first(FRAMING_MARKERS)


def _marker_spans(tokens: list[str], ordered) -> list[tuple[int, int]]:
    """(start, end) of every marker of `ordered` (longest first) in `tokens`, as whole words."""
    spans = []
    for start in range(len(tokens)):
        for marker in ordered:
            if tuple(tokens[start:start + len(marker)]) == marker:
                spans.append((start, start + len(marker)))
                break
    return spans


def _negated_before(tokens: list[str], at: int) -> bool:
    for back in range(at - 1, max(-1, at - 1 - NEGATION_WINDOW), -1):
        if not _word(tokens[back]):
            return False
        if tokens[back] in NEGATIONS:
            return True
    return False


def _skip_adverbs(tokens: list[str], at: int) -> int:
    while at < len(tokens) and tokens[at] in ADVERBS:
        at += 1
    return at


def intended_verbs(text: str) -> frozenset[str]:
    """The verbs `text` names as intentions: the verb right after each marker of `INTENTION_MARKERS` that has no
    negation before it, with its helper chain and a verb coordinated with it ("design and print")."""
    tokens = _tokens(text)
    verbs: set[str] = set()
    for start, end in _marker_spans(tokens, _INTENTIONS):
        if _negated_before(tokens, start):
            continue
        at = _skip_adverbs(tokens, end)
        after_helper = False
        for _link in range(3):                                  # up to two helpers, then the act
            if at >= len(tokens) or not _word(tokens[at]) or tokens[at] in NOT_ACTS - {"be"}:
                break
            word = tokens[at]
            if word == "be":                                    # "will be organising"
                if at + 1 < len(tokens):
                    verbs |= _ing_bases(tokens[at + 1])
                break
            verbs.add(word)
            if after_helper:
                verbs |= _ing_bases(word)                       # "will start painting"
            if word not in HELPERS:
                if (at + 2 < len(tokens) and tokens[at + 1] in ("and", "or") and _word(tokens[at + 2])
                        and tokens[at + 2] not in NOT_ACTS):
                    verbs.add(tokens[at + 2])                   # "design and print"
                break
            after_helper = True
            at += 1
            if at < len(tokens) and tokens[at] in ("to", "and"):
                at += 1
            at = _skip_adverbs(tokens, at)
    return frozenset(verbs)


def _framed(tokens: list[str], at: int, framing_ends: frozenset[int], chain: frozenset[str]) -> bool:
    """A framing marker ends right before `at`, or before a run of at most `CHAIN_REACH` chain words that ends there."""
    for steps in range(CHAIN_REACH + 1):
        if at in framing_ends:
            return True
        if steps == CHAIN_REACH or at == 0 or tokens[at - 1] not in chain:
            return False
        at -= 1
    return False


def stated_done(text: str, verbs) -> frozenset[str]:
    """Which of `verbs` (base forms) `text` states as done: a past tense or a participle (`deed_forms`) that is not
    framed as an intention or a possibility, not negated and not itself part of a framing marker. The verbs are
    chain words of a framing marker's reach as well ("plans to design and set up")."""
    verbs = frozenset(verbs)
    tokens = _tokens(text)
    forms: dict[str, list[tuple[str, bool]]] = {}
    for verb in verbs:
        for form, needs_perfect in deed_forms(verb).items():
            forms.setdefault(form, []).append((verb, needs_perfect))
    if not forms or not any(token in forms for token in tokens):
        return frozenset()
    spans = _marker_spans(tokens, _FRAMINGS)
    inside = {index for start, end in spans for index in range(start, end)}
    framing_ends = frozenset(end for _start, end in spans)
    chain = CHAIN | ADVERBS | HELPERS | verbs
    done: set[str] = set()
    for at, token in enumerate(tokens):
        if token not in forms or at in inside:
            continue
        if at + 1 < len(tokens) and tokens[at + 1] in ("not", "t", "never"):
            continue                                            # "did not", "had never": an auxiliary
        if _negated_before(tokens, at) or _framed(tokens, at, framing_ends, chain):
            continue
        before = at - 1
        while before >= 0 and tokens[before] in ADVERBS:
            before -= 1
        perfect = before >= 0 and tokens[before] in PERFECT
        for verb, needs_perfect in forms[token]:
            if perfect or not needs_perfect:
                done.add(verb)
    return frozenset(done)


def plan_stated_as_deed(sentence_text: str, cited_item_texts) -> bool:
    """True when the guard would drop the sentence: it states as done a verb that at least one of the cited items
    names as an intention, and none of the cited items states that verb as done. False with no cited item text.

    Pure: no switch, no environment, no store. `cited_item_texts` are the texts of the items the sentence cites, as
    the writer saw them (the node passes each item's clipped body and supporting text)."""
    if not isinstance(sentence_text, str):
        return False
    items = [text for text in (cited_item_texts or ()) if isinstance(text, str) and text.strip()]
    if not items:
        return False
    intended = frozenset().union(*(intended_verbs(text) for text in items))
    if not intended:
        return False
    stated = stated_done(sentence_text, intended)
    if not stated:
        return False
    done = frozenset().union(*(stated_done(text, intended) for text in items))
    return bool(stated - done)
