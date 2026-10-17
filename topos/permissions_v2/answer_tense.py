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
- An act's **object** is the words after its verb up to the clause's end (`CLAUSE_ENDS`: "and", "while", a comma, a
  full stop, ...); a passive deed's ("the labels were designed") takes the words before it in its clause as well. Two
  objects **match** when they share a content word (`content_words`: four letters or more, folded as the answer checks
  fold, and none of `OBJECT_STOPWORDS`).
- **Drop** when the sentence states a deed of a verb that a cited item gives as an intention, the deed's object
  matches that intention's object, no cited item states that verb as done, and no cited item states as done, with any
  verb, an act whose object matches the intention's object ("finished the first batch of forty shelf labels" is the
  labels made, for "I will design the shelf labels").

Never drops a sentence with no cited item, a present or progressive sentence ("is designing"), a sentence whose own
words frame the act as a plan ("plans to design", "was going to design"), a deed of a verb no cited item frames as an
intention, or a deed whose object is not the intention's ("need to add a simple login" against "added a search box").
`SKIP` verbs (read, cut, let, hit, ...: the past is spelled as the present) are never read. Only drops: never rewrites
or repairs a sentence.

v2 (16 Oct 2026, after a replay over 1,245 blind-reviewed sentences: v1 caught 13 false sentences and dropped 12 the
reviewers called supported): the object match and the done-in-other-words keep.

Closed lists, pinned by test (`tests/permissions_v2/test_answer_tense_guard.py`): adding an entry is an amendment.
"""
from __future__ import annotations

import re
import unicodedata

from .answer_checks import _stem

VERSION = "topos-answer-tense/v2"

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
#: Never an intended act: the auxiliaries, a negation after a marker ("will not", "will be"), articles and pronouns
#: ("I will help and she will ...": "she" is no act).
NOT_ACTS = frozenset({"be", "have", "not", "never", "to", "and", "or", "the", "a", "an", "i", "you", "he", "she", "we",
                      "they", "it", "me", "him", "her", "us", "them", "my", "our", "his", "their", "its", "your",
                      "this", "that"})
#: An object runs from its verb to the first of these (or a mark that ends a clause), at most `OBJECT_REACH` words.
CLAUSE_ENDS = frozenset({"and", "or", "but", "while", "whereas", "because", "although", "though", "when", "so", "then",
                         "after", "before", "until"})
OBJECT_REACH = 12
#: Words of four letters or more that never make two objects match: function words, the writer's own word for the
#: owner, and time words.
OBJECT_STOPWORDS = frozenset({
    "this", "that", "these", "those", "some", "each", "every", "none", "both", "either", "neither", "much", "many",
    "more", "most", "less", "least", "such", "other", "another", "over", "under", "about", "into", "onto", "down",
    "back", "through", "between", "among", "after", "before", "during", "since", "until", "till", "near", "across",
    "along", "around", "against", "without", "within", "toward", "towards", "upon", "from", "with", "than", "then",
    "because", "although", "though", "while", "whether", "unless", "once", "were", "been", "being", "does", "done",
    "have", "having", "could", "would", "shall", "should", "might", "must", "will", "mine", "ours", "your", "yours",
    "hers", "their", "theirs", "them", "they", "whom", "whose", "what", "which", "when", "where", "there", "here",
    "ones", "something", "anything", "nothing", "everything", "someone", "anyone", "everyone", "just", "only", "also",
    "very", "ever", "never", "again", "still", "already", "lately", "recently", "today", "tonight", "yesterday",
    "tomorrow", "week", "weeks", "weekend", "month", "months", "year", "years", "last", "next", "soon", "later",
    "often", "always", "sometimes", "well", "even", "quite", "rather", "really", "almost", "away", "else", "please",
    "morning", "evening", "afternoon", "night", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "owner", "owners", "first", "together", "thing", "things"})
#: Before a word that looks past, one of these makes it an adjective ("the renovated hall"), not a deed.
DETERMINERS = frozenset({"the", "a", "an", "my", "our", "his", "her", "their", "its", "your", "this", "that", "these",
                         "those", "some", "every", "each"})
#: Before a deed, one of these makes it passive: its object is its clause's subject too ("the labels were designed").
PASSIVE = frozenset({"is", "are", "was", "were", "be", "been", "being", "get", "got", "gets"})
#: Words ending in "-ed" that are no past tense, for the done-in-other-words read of any verb.
NOT_PAST = frozenset({"need", "seed", "feed", "speed", "breed", "bleed", "weed", "deed", "greed", "steed", "reed",
                      "heed", "indeed", "proceed", "succeed", "exceed", "hundred", "kindred", "sacred", "naked",
                      "wicked", "rugged", "ragged", "beloved", "crooked", "jagged", "hatred", "shed", "embed", "seabed",
                      "flowerbed", "hotbed", "sled", "bed", "red"})
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


def content_words(words) -> frozenset[str]:
    """The words two objects are matched on: four letters or more, none of `OBJECT_STOPWORDS`, folded as the answer
    checks fold ("labels" is "label", "histories" "history")."""
    out = set()
    for word in words:
        if len(word) < 4 or not _word(word) or word in OBJECT_STOPWORDS:
            continue
        folded = _stem(word)
        if folded not in OBJECT_STOPWORDS:
            out.add(folded)
    return frozenset(out)


def _object(tokens: list[str], start: int) -> list[str]:
    """The words from `start` up to the clause's end (`CLAUSE_ENDS` or a mark), at most `OBJECT_REACH`."""
    words = []
    for token in tokens[start:start + OBJECT_REACH]:
        if not _word(token) or token in CLAUSE_ENDS:
            break
        words.append(token)
    return words


def _intentions(tokens: list[str]) -> list[tuple[frozenset[str], frozenset[str], tuple[int, int]]]:
    """Each intention in `tokens`: its verbs (the verb right after a marker of `INTENTION_MARKERS` with no negation
    before it, its helper chain, a verb coordinated with it), its object's content words (after the last of those
    verbs), and the span of words it covers, marker to object's end."""
    found = []
    for start, end in _marker_spans(tokens, _INTENTIONS):
        if _negated_before(tokens, start):
            continue
        at = _skip_adverbs(tokens, end)
        verbs: set[str] = set()
        object_start = None
        after_helper = False
        for _link in range(3):                                  # up to two helpers, then the act
            if at >= len(tokens) or not _word(tokens[at]) or tokens[at] in NOT_ACTS - {"be"}:
                break
            word = tokens[at]
            if word == "be":                                    # "will be organising"
                bases = _ing_bases(tokens[at + 1]) if at + 1 < len(tokens) else set()
                if bases:
                    verbs |= bases
                    object_start = at + 2
                break
            verbs.add(word)
            if after_helper:
                verbs |= _ing_bases(word)                       # "will start painting"
            object_start = at + 1
            if word not in HELPERS:
                if (at + 2 < len(tokens) and tokens[at + 1] in ("and", "or") and _word(tokens[at + 2])
                        and tokens[at + 2] not in NOT_ACTS):
                    verbs.add(tokens[at + 2])                   # "design and print": the object is the second's
                    object_start = at + 3
                break
            after_helper = True
            at += 1
            if at < len(tokens) and tokens[at] in ("to", "and"):
                at += 1
            at = _skip_adverbs(tokens, at)
        if verbs:
            words = _object(tokens, object_start) if object_start is not None else []
            stop = (object_start + len(words)) if object_start is not None else end
            found.append((frozenset(verbs), content_words(words), (start, stop)))
    return found


def intended_verbs(text: str) -> frozenset[str]:
    """The verbs `text` names as intentions (`_intentions`)."""
    return frozenset().union(*(verbs for verbs, _object_words, _span in _intentions(_tokens(text))))


def intention_objects(text: str) -> dict[str, frozenset[str]]:
    """Each intended verb of `text` with the content words of its objects, for a reader of the rule."""
    objects: dict[str, frozenset[str]] = {}
    for verbs, words, _span in _intentions(_tokens(text)):
        for verb in verbs:
            objects[verb] = objects.get(verb, frozenset()) | words
    return objects


def _framed(tokens: list[str], at: int, framing_ends: frozenset[int], chain: frozenset[str]) -> bool:
    """A framing marker ends right before `at`, or before a run of at most `CHAIN_REACH` chain words that ends there."""
    for steps in range(CHAIN_REACH + 1):
        if at in framing_ends:
            return True
        if steps == CHAIN_REACH or at == 0 or tokens[at - 1] not in chain:
            return False
        at -= 1
    return False


class _Reading:
    """The framing markers of one text, read once: which words are inside one, where each ends, and the chain words
    a marker's reach runs over."""

    def __init__(self, tokens: list[str], verbs: frozenset[str]):
        spans = _marker_spans(tokens, _FRAMINGS)
        self.tokens = tokens
        self.inside = frozenset(index for start, end in spans for index in range(start, end))
        self.ends = frozenset(end for _start, end in spans)
        self.chain = CHAIN | ADVERBS | HELPERS | verbs

    def stated(self, at: int) -> bool:
        """The word at `at` is said as a deed here: not inside a framing marker, not an auxiliary before a negation,
        not negated, not framed."""
        tokens = self.tokens
        if at in self.inside:
            return False
        if at + 1 < len(tokens) and tokens[at + 1] in ("not", "t", "never"):
            return False                                        # "did not", "had never": an auxiliary
        return not (_negated_before(tokens, at) or _framed(tokens, at, self.ends, self.chain))

    def before(self, at: int) -> int:
        """The index of the word before `at`, past any adverb (-1 when none)."""
        before = at - 1
        while before >= 0 and self.tokens[before] in ADVERBS:
            before -= 1
        return before


def _deeds(tokens: list[str], verbs: frozenset[str]) -> list[tuple[str, int]]:
    """(verb, index) of each word of `tokens` that states one of `verbs` as done (`deed_forms`)."""
    forms: dict[str, list[tuple[str, bool]]] = {}
    for verb in verbs:
        for form, needs_perfect in deed_forms(verb).items():
            forms.setdefault(form, []).append((verb, needs_perfect))
    if not forms or not any(token in forms for token in tokens):
        return []
    reading = _Reading(tokens, verbs)
    deeds = []
    for at, token in enumerate(tokens):
        if token not in forms or not reading.stated(at):
            continue
        before = reading.before(at)
        perfect = before >= 0 and tokens[before] in PERFECT
        for verb, needs_perfect in forms[token]:
            if perfect or not needs_perfect:
                deeds.append((verb, at))
    return deeds


def stated_done(text: str, verbs) -> frozenset[str]:
    """Which of `verbs` (base forms) `text` states as done: a past tense or a participle (`deed_forms`) that is not
    framed as an intention or a possibility, not negated and not itself part of a framing marker. The verbs are
    chain words of a framing marker's reach as well ("plans to design and set up")."""
    return frozenset(verb for verb, _at in _deeds(_tokens(text), frozenset(verbs)))


def _deed_object(tokens: list[str], at: int, before: int) -> frozenset[str]:
    """The content words of the object of the deed at `at`: the words after it to the clause's end ("designed and
    printed the labels": after the second verb when nothing stands between), and, when it is passive (`before` is the
    index of the word before it, past adverbs), the words before it in its clause as well."""
    words = _object(tokens, at + 1)
    if (not content_words(words) and at + 2 < len(tokens) and tokens[at + 1] in ("and", "or")
            and _word(tokens[at + 2])):
        words = _object(tokens, at + 3)
    if before >= 0 and tokens[before] in PASSIVE:
        back = before - 1
        while back >= 0 and before - back <= OBJECT_REACH and _word(tokens[back]) and tokens[back] not in CLAUSE_ENDS:
            words.append(tokens[back])
            back -= 1
    return content_words(words)


#: The irregular past forms the done-in-other-words read knows for any verb: every simple past and participle of
#: `IRREGULAR` that is not spelled as its base ("set", "put" and "run" are left out: "a set of labels").
_IRREGULAR_PAST = frozenset(form for base, pair in IRREGULAR.items() for form in pair if form != base)


def _past_looking(word: str) -> bool:
    if word in _IRREGULAR_PAST:
        return True
    return word.endswith("ed") and (len(word) >= 5 or word == "used") and word not in NOT_PAST


def _acts_done(tokens: list[str], intention_spans) -> list[frozenset[str]]:
    """The objects (content words) of every act `tokens` states as done with any verb: a past tense or participle
    (an irregular form, or a word ending in "-ed" that is none of `NOT_PAST`), not after a determiner ("the renovated
    hall"), not inside the text's own intentions, and said as a deed (`_Reading.stated`)."""
    inside_intentions = {index for start, stop in intention_spans for index in range(start, stop)}
    reading = _Reading(tokens, frozenset())
    acts = []
    for at, token in enumerate(tokens):
        if at in inside_intentions or not _word(token) or not _past_looking(token):
            continue
        if at > 0 and tokens[at - 1] in DETERMINERS:
            continue
        if not reading.stated(at):
            continue
        acts.append(_deed_object(tokens, at, reading.before(at)))
    return acts


def plan_stated_as_deed(sentence_text: str, cited_item_texts) -> bool:
    """True when the guard would drop the sentence: it states as done a verb that a cited item names as an
    intention, the deed's object shares a content word with that intention's object, no cited item states that verb
    as done, and no cited item states as done (with any verb) an act whose object shares a content word with that
    intention's object. False with no cited item text.

    Pure: no switch, no environment, no store. `cited_item_texts` are the texts of the items the sentence cites, as
    the writer saw them (the node passes each item's clipped body and supporting text)."""
    if not isinstance(sentence_text, str):
        return False
    items = [_tokens(text) for text in (cited_item_texts or ()) if isinstance(text, str) and text.strip()]
    if not items:
        return False
    read = [_intentions(tokens) for tokens in items]
    intentions = [(verbs, words) for found in read for verbs, words, _span in found]
    intended = frozenset().union(*(verbs for verbs, _words in intentions))
    if not intended:
        return False
    said = _tokens(sentence_text)
    deeds = _deeds(said, intended)
    if not deeds:
        return False
    done = frozenset(verb for tokens in items for verb, _at in _deeds(tokens, intended))
    acts = [act for tokens, found in zip(items, read) for act in _acts_done(tokens, [span for *_, span in found])]
    reading = _Reading(said, intended)
    for verb, at in deeds:
        if verb in done:
            continue                                            # a cited item says this verb was done
        deed_object = _deed_object(said, at, reading.before(at))
        matching = [words for verbs, words in intentions if verb in verbs and words & deed_object]
        if not matching:
            continue                                            # the intention is about another object
        if any(act & words for words in matching for act in acts):
            continue                                            # a cited item says it was done in other words
        return True
    return False
