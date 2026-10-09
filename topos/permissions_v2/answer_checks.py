"""Deterministic final checks for answers written by the owner's local model.

The model's text is untrusted. Each check removes a sentence or closes the
entire answer; none repairs, fills in, or fetches unrestricted source text.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TEMPLATE_VERSION = "topos-answer-template/v7"
_CITATION = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_SENTENCE_END = re.compile(r'''[.!?]+["'”’)]*(?:\s*\[\d+(?:\s*[,;]\s*\d+)*\])*(?=\s|$)''')
_BULLET = re.compile(r"^([-*•]|\d+[.)])\s+")
_ABBREVIATIONS = frozenset({"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "approx", "inc", "ltd", "co", "corp"})
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_EMAIL_RE = re.compile(r"[\w.-]+@[\w.-]+\.\w+")
_PHONE_RE = re.compile(r"\+?\d[\d\s()-]{7,}\d")
_ISO_DATE_PREFIX_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NSFW_TOKENS = ("nsfw", "xxx")
_QUESTION_SCAFFOLD = frozenset({"happened", "anything", "according", "available", "describe", "discussed",
    "explain", "information", "mentioned", "meaning", "reference", "references", "regarding", "remember",
    "specific", "something", "summarize", "summary", "updated", "updates", "yesterday", "tomorrow"})
# BL-146 (A2A-4 amendment 4): request words of the recipients' question shapes (the eval catalog's) that never name a
# subject an item could carry. Each was a subject before, so every such question abstained or dropped every sentence:
# - whose share it is: every item a share releases is its owner's own, written in the first person, so no item says
#   "owner" (the reason "shared" is a question word); nor a pronoun for the owner (round 2; the shorter pronouns,
#   "she", "him", "they", are never subjects at all, being under five letters). The owner's own names are per node:
#   answer_generation.owner_party_words;
# - the share's own record forms: a message, chat, record or entry is what every item is, not what it is about;
# - how to answer: "Cite the supporting evidence", "described", "mentioned", "discussed", "written", and
#   "question(s)" ("Quick question: ..."; round 4 add-on, WS0);
# - when: "lately", "recently"; the share's window already bounds every item's time.
# A subject word stays one: "trips", "glass", "Olympics", "holidays", "relationships", "projects", "career",
# "statements". Both rules read this list after the fold: the anchor rule (8 letters or more) and the subject check.
FORM_WORDS = frozenset({"owner", "owners", "herself", "himself", "theirs", "themself", "themselves",
    "message", "messages", "chat", "chats", "record", "records", "entry", "entries",
    "cite", "evidence", "support", "supporting", "supported", "describe", "described", "mention", "mentioned",
    "discuss", "discussed", "written", "question", "questions",
    # round 5 (1.5.3): the catalog's "What has the owner said about ..." and "What has the owner asked about ...":
    # the act of saying or asking is how to answer, never what an item is about. Four-letter words never bound before;
    # they matter now that every cited item must carry a content word of the question (per-item carriage).
    "said", "say", "says", "asked", "ask", "asks",
    "lately", "recent", "recently"})
# A2A-4 amendment 5 (AR152 class I, 1.5.2): the catalog templates' own instruction words, a second closed list read
# exactly like FORM_WORDS by the anchor rule (`_SCAFFOLD_FOLDS`) and the subject check
# (answer_generation._GENERIC_QUESTION_TERMS), after the fold. They say how to answer ("Separate stated facts from
# inference", "Only report what the messages state", "Do not connect unrelated messages or infer chronology", "Name
# topics only, never pages", "going by their browsing"), never what an item is about; a supported sentence for every
# fact, goal, relationship and interest template, and the goals, home and family questions, was dropped for want of
# one. It only stops requiring words no item can carry: names still bind, an unknown word still echoes, every other
# subject word still binds. Not in it, by WS0's ruling: "relationships", "people", "spend", "goal", "topic"
# (singular). Pinned by an exact-set test; adding a word is an amendment.
INSTRUCTION_WORDS = frozenset({"stated", "state", "states", "report", "guess", "inference", "infer", "separate",
    "statement", "statements", "explicit", "explicitly", "connect", "unrelated", "chronology", "establish",
    "establishes", "around", "pursue", "going", "browsing", "browse", "topics", "never", "pages", "page"})


def _redact_phone(match: re.Match[str]) -> str:
    candidate = match.group(0)
    return candidate if _ISO_DATE_PREFIX_RE.match(candidate) else "[REDACTED_PHONE]"


def _redact_pii(text: str) -> str:
    return _PHONE_RE.sub(_redact_phone, _EMAIL_RE.sub("[REDACTED_EMAIL]", text))


def _sanitize_nsfw(text: str) -> str:
    return "[SANITIZED]" if any(token in text.lower() for token in _NSFW_TOKENS) else text


def _initials(word: str) -> bool:
    if len(word) == 1 and word.isupper():
        return True
    parts = word.split(".")
    return len(parts) > 1 and all(len(part) == 1 and part.isalpha() for part in parts)


def _ends_sentence(line: str, match: re.Match[str]) -> bool:
    if "[" in match.group(0):
        return True
    tail = line[match.end():].lstrip()
    if tail and tail[0].islower():
        return False
    if not re.fullmatch(r'''\.["'”’)]*''', match.group(0)):
        return True
    head = line[:match.start()]
    found = re.search(r"[\w.]+$", head, re.UNICODE)
    word = found.group(0) if found else ""
    return word.lower() not in _ABBREVIATIONS and not _initials(word)


def split_sentences(line: str) -> list[str]:
    out, start = [], 0
    for match in _SENTENCE_END.finditer(line):
        if not _ends_sentence(line, match):
            continue
        out.append(line[start:match.end()].strip())
        start = match.end()
    out.append(line[start:].strip())
    return [part for part in out if part]


def citation_numbers(sentence: str) -> list[int]:
    return [int(part) for match in _CITATION.finditer(sentence)
            for part in re.split(r"\s*[,;]\s*", match.group(1))]


@dataclass(frozen=True)
class CheckedCitations:
    sentences: tuple[str, ...]
    cited: frozenset[int]
    dropped: int


def post_check_citations(answer: str, record_count: int) -> CheckedCitations:
    """Port the recipient app's sentence and citation rule; retain bullet prefixes."""
    kept, cited, dropped = [], set(), 0
    for raw_line in answer.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        bullet = _BULLET.match(line)
        prefix = bullet.group(0) if bullet else ""
        for index, sentence in enumerate(split_sentences(line[len(prefix):])):
            numbers = citation_numbers(sentence)
            if not numbers or any(number < 1 or number > record_count for number in numbers):
                dropped += 1
                continue
            kept.append((prefix if index == 0 else "") + sentence)
            cited.update(numbers)
    return CheckedCitations(tuple(kept), frozenset(cited), dropped)


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(unicodedata.normalize("NFKC", text).casefold())


# The word fold the answer checks compare through, the same for the question, the items and the model's sentences.
_UNDOUBLED = frozenset("bdgmnprt")


def _vowel(word: str, i: int) -> bool:
    """A vowel letter: "y" after a consonant is one ("story"), "u" after "q" is not ("quite").

    A run of "y" alternates (consonant, vowel, consonant…) from the letter before it, counted without recursion, so
    a long run in a question folds like any word instead of ending the answer as `model_error` (BL-144)."""
    letter = word[i]
    if letter in "aeio":
        return True
    if letter == "u":
        return i == 0 or word[i - 1] != "q"
    if letter != "y":
        return False
    start = i
    while start > 0 and word[start - 1] == "y":
        start -= 1
    # word[start] is the run's first "y": a vowel when a consonant comes before it, a consonant at the start or after
    # a vowel. Each later "y" in the run is the opposite of the one before it.
    first = start > 0 and not _vowel(word, start - 1)
    return first if (i - start) % 2 == 0 else not first


def _measure(stem: str) -> int:
    """The stem's vowel-then-consonant runs: 1 for "shar", "hous", "not"; 2 for "updat"."""
    return sum(1 for i in range(1, len(stem)) if _vowel(stem, i - 1) and not _vowel(stem, i))


def _short_syllable(stem: str) -> bool:
    """One syllable ending consonant, vowel, consonant (not w, x or y): "shar", "not", "plan", "quit"."""
    return (len(stem) >= 3 and _measure(stem) == 1 and not _vowel(stem, len(stem) - 3)
            and _vowel(stem, len(stem) - 2) and not _vowel(stem, len(stem) - 1) and stem[-1] not in "wxy")


def _after_suffix(stem: str) -> str:
    """What "-ing" or "-ed" leave: "plann" to "plan" (never "ll", "ss", "zz", "ff"); "shar" to "share", "not" to
    "note" (a short syllable whose last consonant was not doubled had an "e"), "hous" to "house" (so did "-se")."""
    if stem[-1] == stem[-2] and stem[-1] in _UNDOUBLED:
        return stem[:-1]
    return stem + "e" if _short_syllable(stem) or (stem[-1] == "s" and stem[-2] != "s") else stem


def _fold_once(word: str) -> str:
    """One step of the fold. "-ss" is not a plural ("class", "access"), and a step that would leave fewer than 3
    letters is not taken. A word of 4 letters folds only as a plural onto a short syllable ("maps" to "map"), so it
    never becomes a different common word ("note", "news", "does", "this" stay as they are)."""
    if len(word) <= 3:
        return word
    if len(word) == 4:
        return word[:-1] if word.endswith("s") and not word.endswith("ss") and _short_syllable(word[:-1]) else word
    if word.endswith(("ies", "ied")):
        stem = word[:-3] + "y"                   # stories, tried
    elif word.endswith("ie"):
        stem = word[:-2] + "y"                   # movie, as movies
    elif word.endswith("xes"):
        stem = word[:-2]                         # boxes, taxes
    elif word.endswith("s") and not word.endswith("ss"):
        stem = word[:-1]                         # plans, notes; lunches, then lunche to lunch
    elif word.endswith("eed"):
        stem = word[:-1] if _measure(word[:-3]) else word      # agreed to agree; never speed, freed
    elif word.endswith("ing") and any(_vowel(word, i) for i in range(len(word) - 3)):
        stem = _after_suffix(word[:-3])          # planning, sharing; never string, thing
    elif word.endswith("ed") and any(_vowel(word, i) for i in range(len(word) - 2)):
        stem = _after_suffix(word[:-2])          # planned, shared, noted
    elif (word.endswith("e") and _measure(word[:-1]) and not _short_syllable(word[:-1])
          and _fold_once(word[:-1]) == word[:-1]):
        stem = word[:-1]                         # update, house, lunche; never share, plane, quite, tense
    else:
        return word
    return stem if len(stem) >= 3 else word


def _stem(word: str) -> str:
    """The fold, applied until a step changes nothing, so it is idempotent and a word meets its own forms:
    "meetings", "meeting", "meet"; "update", "updates", "updated"; "stories", "story"; "shared", "share"."""
    while (folded := _fold_once(word)) != word:
        word = folded
    return word


# A word is scaffold when its fold is the fold of a scaffold word ("explained" is "explain", "happening" "happen").
_SCAFFOLD_FOLDS = frozenset(_stem(word) for word in _QUESTION_SCAFFOLD | FORM_WORDS | INSTRUCTION_WORDS)


def question_anchors(question: str) -> frozenset[str]:
    return frozenset(word for word in _tokens(question) if len(word) >= 8 and _stem(word) not in _SCAFFOLD_FOLDS)


def _suffix_step(word: str) -> str:
    """One suffix off: the fold's first step, with "-es" after a hiss taken as one suffix ("lunches" to "lunch",
    "classes" to "class"), never two ("cannings" to "canning", never "can")."""
    step = _fold_once(word)
    if word.endswith("es") and step == word[:-1] and (after := _fold_once(step)) == step[:-1]:
        return after
    return step


def _forms_within_one_suffix(words) -> set[str]:
    """Each word as written and with one suffix off."""
    return {form for word in words for form in (word, _suffix_step(word))}


def _within_one_suffix(word: str, forms: set[str]) -> bool:
    """`word` and some word of `forms` (built by `_forms_within_one_suffix`) are at most one suffix apart each:
    "meetings" and "meeting", "compilers" and "compiler", "stories" and "story", "updating" and "update"."""
    return word in forms or _suffix_step(word) in forms


def question_only_anchors(question: str, raw_texts: tuple[str, ...]) -> frozenset[str]:
    """Distinctive question words absent, in every form, from every item the model may see.

    An invented or protected word supplied by the asker is not evidence that
    the share contains it. A short common question word is not an anchor. A
    word is present when it and an item's word are at most one suffix apart
    ("meetings" and "meeting"). The whole fold is not enough (BL-144): it takes
    two suffixes off a name shaped as a word plus "-ings" ("cannings" to "can",
    "herrings" to "her"), so a supplied name no item carries would count as
    present wherever an item says the short word, the model would run on it,
    and its echo of the name would be kept.
    """
    item_forms = _forms_within_one_suffix(word for raw in raw_texts for word in _tokens(raw))
    return frozenset(word for word in question_anchors(question) if not _within_one_suffix(word, item_forms))


def echoes_question_only_word(sentence: str, echoes: frozenset[str]) -> bool:
    """The sentence uses a question-only anchor in any of its forms (the whole fold: the safe side drops more)."""
    folds = {_stem(word) for word in echoes}
    return any(_stem(word) in folds for word in _tokens(sentence))


def protected_question_words(question: str, raw_texts: tuple[str, ...], boundary) -> frozenset[str]:
    """The forms of the asker's own words that name someone Off-limits where the boundary does not read them.

    The answer pass refuses a question the boundary reads as protected before the model runs. A name written in lower
    case, or as an "-ies" plural of a "-y" name, is not read there (a part that is a word in lower case is a word),
    and when no item carries it as written it can come back in the model's sentence first, in lower case, or in the
    same plural, where the boundary does not read it either (BL-144). For each question word no item carries as
    written, this returns the word as written and with one suffix off ("cherries", "cherry") wherever the boundary
    reads that form, written as a name, as protected."""
    item_words = {word for raw in raw_texts for word in _tokens(raw)}
    forms = {form for word in _tokens(question) if len(word) >= 3 and word not in item_words
             for form in (word, _suffix_step(word))}
    if not forms or not boundary.mentions_protected("\n".join(sorted(form.capitalize() for form in forms))):
        return frozenset()
    return frozenset(form for form in forms if boundary.mentions_protected(form.capitalize()))


def echoes_protected_word(sentence: str, forms: frozenset[str]) -> bool:
    """The sentence uses one of `forms` (from `protected_question_words`) as written or with one suffix more, in any
    case: "cherries" or "Cherry" for "cherry"; "fielding" or "Fieldings" for "fieldings"."""
    return bool(forms) and any(word in forms or _suffix_step(word) in forms for word in _tokens(sentence))


def _runs(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i+n]) for i in range(len(tokens) - n + 1)}


def copied_sentence(sentence: str, raw_texts: list[str] | tuple[str, ...]) -> bool:
    """Three conservative overlapping-copy rules from A2A-4 §6.2 check 5."""
    plain = _CITATION.sub("", sentence)
    words = _tokens(plain)
    if not words:
        return True
    for raw in raw_texts:
        source = _tokens(raw)
        if len(words) >= 8 and _runs(words, 8) & _runs(source, 8):
            return True
        if len(source) >= 4 and tuple(source) in _runs(words, len(source)):
            return True
        if len(words) >= 5:
            own, theirs = _runs(words, 5), _runs(source, 5)
            if own and len(own & theirs) * 2 >= len(own):
                return True
    return False


def scrub_sentences(sentences: list[str]) -> tuple[list[str], int]:
    kept, dropped = [], 0
    for sentence in sentences:
        if _sanitize_nsfw(sentence) != sentence:
            dropped += 1
        else:
            kept.append(_redact_pii(sentence))
    return kept, dropped
