"""Deterministic final checks for answers written by the owner's local model.

The model's text is untrusted. Each check removes a sentence or closes the
entire answer; none repairs, fills in, or fetches unrestricted source text.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TEMPLATE_VERSION = "topos-answer-template/v3"
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


def question_anchors(question: str) -> frozenset[str]:
    return frozenset(word for word in _tokens(question) if len(word) >= 8 and word not in _QUESTION_SCAFFOLD)


def question_only_anchors(question: str, raw_texts: tuple[str, ...]) -> frozenset[str]:
    """Distinctive question words absent from every item the model may see.

    An invented or protected word supplied by the asker is not evidence that
    the share contains it. A short common question word is not an anchor.
    """
    item_words = {word for raw in raw_texts for word in _tokens(raw)}
    return question_anchors(question) - item_words


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
