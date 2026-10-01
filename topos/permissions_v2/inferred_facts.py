"""Derived facts (IF-6 v1): a fact the node's extractor drew from one journal entry, released as `inferred`.

The extractor writes facts about the owner from journal entries (`signal_objects`, `object_type = 'fact'`, citing
`journal_entries`). They release only when the entry states the value in one of its class's first-person forms,
which on the measured node is never. With ``TOPOS_PERMISSIONS_V2_DERIVED_FACTS`` on (the owner's opt-in, global,
default off) such a fact releases with the entry it cites, under a grant that already signs the "Journal entries"
option, marked by the existing wire value ``assertion: "inferred"`` (IF-6 §5: no new field, because the CP relay
and the recipient app refuse an unknown key). The recipient already reads the entry; the added exposure is the
extractor's reading of it, so the guards here bound what an inference may say.

This module decides only the value-level guards (IF-6 §3). Everything a stated fact must clear still runs first in
``knowledge_projections.fact_projection`` (closed facts, disclosure, class, scalar, the attested subject, tombstones,
owner-only, Off-limits over the stored row, the entry's own qualification, window, NSFW, the grant's decision and
lineage); the inferred path is reached only when the stated floor and OD-38 have both failed (§2 step 7).

``refusal`` returns the first guard that withholds, as a code (never text), or None; the order is fixed:

1. ``inferred_entry_labels``: the entry's qualified labels are not owner-authored original wording with nothing
   protected (a re-check of what qualification already required, at the point of use); or (v1b) the review's own
   protected_content before any floor was not `none`. OD-58 lets an entry release when the model said `unknown`;
   the inference drawn from it adds exposure, so the fact does not.
2. ``inferred_entry_sensitivity``: the entry's own sensitivity is neither none nor personal.
2a. ``inferred_entry_marked_special`` (v1c): the entry carries an explicit label that marks it private, sensitive,
   confidential, special or a special category: a metadata_json key or value, a label line in its text ("Tags: ...",
   "Sensitivity: ..."), a hashtag, a bracketed or one-line tag, or an instruction not to share it. No rule read
   these before; the model may still have said `none`.
2b. ``inferred_entry_special_cue`` (v1c): any text of the entry (its content, people, metadata keys and values, and
   the category, mood and place columns where the row has them) carries a special-category cue by H1's lists (the
   same ``journal_goal_field._special`` guard 5 reads), with format characters and marks removed and look-alike
   letters mapped first. The entry's own release and the journal floors are unchanged (an owner decision): only
   the inference drawn from such an entry withholds.
3. ``inferred_value_shape``: not one plain scalar label (type, 2-200 characters, 1-12 words, the shared atomic
   label syntax, and characters the guards can read: NFKC-stable, no control or format character, Latin letters
   only, no combining mark).
4. ``inferred_value_protected``: the value, or the item's wire content, carries an Off-limits term, or the value
   carries a bare part of an Off-limits name as a whole word (the journal family's own rule, since the value is
   drawn from an entry); ``inferred_boundary_unavailable`` when the boundary is missing or cannot answer.
5. ``inferred_value_special``: a special category, by Lane H1's lists (``journal_goal_field._special``, with no
   verb slot: "weed", "fast", "scan" and "smoke" count, except a smoke test); and (v1b) H1's closed vocabulary:
   every word one H1's rule has vetted, except a value that is one capitalised token with no special root (a
   project, employer or place name), which guards 4 and 8 still judge.
6. ``inferred_value_question_or_quote``: a question mark, a quote mark, a stray apostrophe, or a question word first.
7. ``inferred_value_not_a_value``: a URL, handle, path or domain; template or code characters; a placeholder; the
   class key or the predicate echoed back; or no letter at all.
8. ``inferred_value_names_person``: a name the node holds for someone else or the entry's people column names; a
   pronoun, kinship, role or trade word (v1b: a closed trades list and trade compounds, under every predicate); an
   honorific; a possessive; or, for a predicate whose value is not a proper noun, a capitalised word after the first.

The lists are Lane H1's (``journal_goal_field``) and OD-38's (``entailment_grounding``), read by name, never copied:
one vocabulary, one place. ``VERSION`` moves with any change to a guard or a list it reads; the index basis carries
it while the flag is on (``search_index._family_rubric_basis``), so every index is rebuilt when it moves. v1b (after
blind set 2, which released 14 must-withhold facts: 11 special categories in ordinary words, a trade, an Off-limits
nickname form, and a model `unknown` the journal floor had turned into `none`) adds the closed vocabulary, the
trades and the unfloored label; nothing v1 withheld can release. v1c (after blind set 4, which released a fact
from an entry carrying an explicit special label no rule read) adds guards 2a and 2b; nothing v1b withheld can release.
Accepted residual (IF-6 §3, guard 8): a person the node does not know, named only as the proper-noun value of a
works_at / worked_at / studied_at / member_of / lives_in / works_on / work.project fact, with no Off-limits term,
honorific or people-column mention, is not caught. The blind set (Lane O) reports it.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata

from . import entailment_grounding as eg
from . import journal_goal_field as jgf
from .fact_contract import atomic_label_syntax
from .predicate_classes import CLASSES

FLAG = "TOPOS_PERMISSIONS_V2_DERIVED_FACTS"
VERSION = "inferred-fact-guards/v1c"
CODES = ("inferred_entry_labels", "inferred_entry_sensitivity", "inferred_entry_marked_special",
         "inferred_entry_special_cue", "inferred_value_shape", "inferred_value_protected",
         "inferred_boundary_unavailable", "inferred_value_special", "inferred_value_question_or_quote",
         "inferred_value_not_a_value", "inferred_value_names_person")
# Predicates whose value is expected to be a proper noun (an employer, a school, a city, a project): a capitalised
# word does not withhold by itself (OD-38's NAMED_PREDICATES requires one; 35 of the 37 releasable-class facts
# measured are work.project).
PROPER_NOUN_PREDICATES = eg.NAMED_PREDICATES | frozenset({"works_on", "work.project"})
HONORIFICS = frozenset("mr mrs ms mx miss dr prof sir dame lord lady rev fr".split())
# v1b (blind set 2): a person by trade, under every predicate, beside H1's roles, relations and "-ologist" endings.
# Words whose ordinary sense in H1's vocabulary is a thing (a model, a printer, a worker pool, a vendor) are left out.
TRADES = frozenset("""
actor actors actress architect architects baker bakers banker bankers barber barbers barista baristas bartender
bartenders bookkeeper bookseller booksellers broker brokers builder builders butcher butchers butler buyer cabbie
captain carer carers carpenter carpenters cashier cashiers caterer caterers chauffeur chef chefs clerk clerks
cleaner cleaners cobbler concierge conductor courier couriers curator dancer dancers dealer dealers decorator
decorators designer designers detective developer developers diver driver drivers drummer engineer engineers
farmer firefighter firefighters fisherman fishermen foreman gardener gardeners grocer groundskeeper guard guards
hairdresser hairdressers housekeeper hunter inspector inspectors installer instructor instructors janitor jeweler
jeweller joiner judge labourer laborer landscaper librarian lifeguard locksmith machinist maid maids maker makers
mason masseur masseuse miner navigator officer officers operator operators painter painters pilot pilots plasterer
player players porter postman postwoman potter preacher producer programmer programmers publisher ranger realtor
receptionist referee reporter reporters roofer runner sailor salesman saleswoman salesperson secretary seller
sellers servant sheriff shopkeeper singer singers smith soldier soldiers steward stylist surveyor tailor teller
trader traders trainer trainers translator tutor tutors usher valet waiter waiters waitress warden welder welders
writer writers
""".split())
# A compound by trade: a bookseller, a shoemaker, a gatekeeper, a fishmonger, a playwright, a goldsmith, a postman.
TRADE_ENDINGS = ("seller", "sellers", "maker", "makers", "keeper", "keepers", "monger", "mongers", "wright",
                 "wrights", "smith", "smiths", "man", "men", "woman", "women", "person", "persons")
TRADE_ENDING_EXEMPT = frozenset("humans german germans romans omens stamen stamens specimen specimens acumen regimen "
                                "regimens talisman talismans ottoman ottomans batman caiman ramen".split())
TEMPLATE_CHARACTERS = frozenset("{}<>[]$`=;")
# Guard 2a (v1c): words that mark an entry when they label it. A key holding one of them ("private", "is_sensitive",
# "Sensitivity:") marks the entry unless its value says it does not (UNSET); a label's value holding one ("Tags:
# private") marks it too. "personal" is no marker: it is the ordinary level of a journal entry (guard 2 admits it).
MARKER_WORDS = frozenset("""
private privately privacy sensitive sensitivity confidential confidentiality special secret secrets restricted
classified intimate nsfw hidden
""".split())
# A special category named as a label ("Category: health", "#finance", {"tags": ["legal"]}). Only where the entry
# labels itself: in prose these words are ordinary, and guard 2b reads prose by H1's lists alone.
SPECIAL_LABELS = frozenset("""
health healthcare wellness wellbeing medicine meds race legal law finance financial finances money debt debts banking
salary income tax taxes intimacy
""".split())
MARKER_PHRASES = jgf._phrases("""
do not share|don't share|dont share|not for sharing|not to be shared|never share|eyes only|for me only|only for me|
just for me|off the record|off limits|not public|keep this private|keep it private|private entry|private note|
special category|special categories
""")
# Keys whose value is a label ("Tags: ...", {"category": ...}). Any other key is read only for a marker word in the
# key itself (guard 2a) and for special cues (guard 2b); a prose key (the template's "goal") only for cues.
LABEL_KEYS = frozenset("""
label labels tag tags category categories class classification type kind topic topics flag flags marker markers
status access audience visibility privacy sensitivity level share sharing shared
""".split())
UNSET = frozenset("""
none no false off 0 public normal low ordinary open everyone anyone all na null nil default standard unrestricted
personal shareable shared
""".split())
# A key about sharing ({"share": false}, "Shareable: no") that says not to share marks the entry too.
SHARE_KEYS = frozenset("share sharing shareable shared public publish published visible".split())
SHARE_REFUSED = frozenset("no false never none off 0 nobody noone private".split())
PROSE_KEYS = frozenset("goal".split())     # the template's own field: its text is read for cues (2b), not as a label
MAX_LABEL_WORDS = 4                # a label line's key, and a one-line tag, is at most this many words
# A line, or a bracketed tag opening or closing one, is a tag when every word is a marker or one of these ("Private
# entry", "[Confidential]", "Note: this is private"); prose that merely uses the word ("Nothing special today") is not.
TAG_WORDS = frozenset("""
entry note notes only label tag this is it marked as category content flagged flag do not share keep please very
highly strictly data info journal log page item mark
""".split())
_LABEL_LINE = re.compile(r"^[\s>*#\-\u2022(\[]*([^\W\d_][\w' /-]{0,40}?)[\s*\])]*[:=]\s*(.*)$")
_HASHTAG = re.compile(r"#([^\W_][\w-]*)")
_BRACKETED = re.compile(r"[\[(]\s*([^\[\]()]{1,60}?)\s*[\])]")
_DOMAIN = re.compile(r"\w\.\w{2,}")
_PLURAL_POSSESSIVE = re.compile(r"([^\W\d_]+s)'(?=\s|$|[.!])")


def enabled(env=None) -> bool:
    """The node flag, read as the family flags are (1/true/yes/on). Inert unless the journal family is on too: a
    journal citation does not resolve without it, so the flag would change nothing a recipient can receive."""
    env = os.environ if env is None else env
    if str(env.get(FLAG, "")).strip().lower() not in ("1", "true", "yes", "on"):
        return False
    from .evidence_families import family
    return family("journal_entries").enabled(env)


def refusal(value, predicate, entry, labels, *, boundary, people, env=None, model_protected_content=None) -> str | None:
    """Why the extractor's ``value`` for ``predicate`` may not release as inferred from this journal ``entry``.

    ``entry``: the member row as qualification loaded it (``content``, ``people``, ``metadata_json``, NSFW flag).
    ``labels``: the entry's qualified classification (``classifications[0]``) before the merge with the fact's
    implicit labels; the owner's correction already wins there, as for a message.
    ``boundary``: an object with ``mentions_protected(*texts)`` (``EntityBoundary``); None withholds.
    ``people``: ``journal_goal_field.known_people(conn)``; None withholds (no third party can be ruled out).
    ``env``: unused by the v1 guards (the flag is the caller's, read before the call); kept so every caller passes
    one signature.
    ``model_protected_content`` (v1b): the protected_content the entry's own review gave before any floor: the
    machine review's model label (`MachineMessageReview.model_protected_content`), or the owner's own correction.
    The journal family's floor (OD-58) lets the entry release when the model said `unknown`; an inference drawn
    from it adds exposure, so the fact needs the model's own `none`. None (never recorded) withholds.
    """
    if not _labels_are(labels, "authorship", "owner_authored") or not _labels_are(labels, "speech", "original_message") \
            or not _labels_are(labels, "protected_content", "none") or model_protected_content != "none":
        return "inferred_entry_labels"
    if getattr(labels, "sensitivity", None) not in ("none", "personal"):
        return "inferred_entry_sensitivity"
    return entry_refusal(entry) or value_refusal(value, predicate, entry, boundary=boundary, people=people)


def entry_refusal(entry) -> str | None:
    """Guards 2a and 2b (v1c): the entry itself, read for an explicit label marking it special or private, and for
    a special-category cue anywhere in its text. An entry whose parts cannot be read withholds."""
    try:
        texts, free, labels = _entry_parts(entry)
        if _marked_special(free, labels):
            return "inferred_entry_marked_special"
        if any(jgf._special(_scan_words(text), None) for text in texts):
            return "inferred_entry_special_cue"
    except Exception:  # noqa: BLE001 -- an entry the guards cannot read withholds
        return "inferred_entry_marked_special"
    return None


def value_refusal(value, predicate, entry, *, boundary, people) -> str | None:
    """Guards 3-8 of ``refusal``: the value on its own, read against the boundary and the node's people."""
    if _shape_refused(value):
        return "inferred_value_shape"
    if boundary is None:
        return "inferred_boundary_unavailable"
    try:
        if boundary.mentions_protected(value, wire_content(predicate, value)) or _name_part(boundary, value):
            return "inferred_value_protected"
    except Exception:  # noqa: BLE001 -- an Off-limits check that cannot answer withholds
        return "inferred_boundary_unavailable"
    raw = jgf._TOKEN.findall(jgf._fold(value))
    plain = [jgf._plain(word) for word in raw]
    if jgf._special(plain, None) or not _vocabulary(raw, plain):
        return "inferred_value_special"
    if _question_or_quote(value, [word.casefold() for word in raw]):
        return "inferred_value_question_or_quote"
    if _not_a_value(value, predicate, plain):
        return "inferred_value_not_a_value"
    if _names_person(value, predicate, entry, people, raw, plain):
        return "inferred_value_names_person"
    return None


def wire_content(predicate, value) -> str | None:
    """The item's wire content, exactly as ``fact_projection`` writes it; None for a predicate it never writes."""
    from .knowledge_projections import PREDICATE_TEXT
    if predicate not in PREDICATE_TEXT or not isinstance(value, str):
        return None
    return f"Owner {PREDICATE_TEXT[predicate]} {value}."


def snapshot_people(conn, boundary) -> frozenset:
    """``known_people(conn)``, read once per read snapshot.

    The resolver keeps one ``EntityBoundary`` per read transaction (``EvidenceResolver.entity_boundary``; its
    ``rebind`` is only for a transaction proven to hold the same rows), so the set is kept on that object and dies
    with it. A boundary built outside such a transaction is a fresh object, and the set is read again. Raises what
    ``known_people`` raises; the caller withholds.
    """
    cached = getattr(boundary, "_inferred_fact_people", None)
    if isinstance(cached, frozenset):
        return cached
    people = jgf.known_people(conn)
    try:
        boundary._inferred_fact_people = people
    except AttributeError:
        pass   # a boundary that keeps no attributes is simply read again next time
    return people


# --- the guards ----------------------------------------------------------------------------------

def _name_part(boundary, value) -> bool:
    """The journal family's own Off-limits rule on the value: a bare part of an Off-limits name, as a whole word.

    `mentions_protected` matches whole terms only, but the value is drawn from a journal entry, and an entry
    withholds on a part of an Off-limits name (`entity_boundary.NAME_PART_TABLES`). A name-only Off-limits term
    belongs to no person or contact the node holds, so neither the fact row's own veto nor guard 8 sees it. The
    value alone: the wire content's other words are the node's own template, which a whole term spanning into the
    value is already checked against. Read through the boundary's own name-part scan; a boundary without it cannot
    answer, and the caller withholds."""
    return bool(boundary.name_part_match_only("journal_entries", {"value": value}))


def _vocabulary(raw: list, plain: list) -> bool:
    """Guard 5's closed vocabulary (v1b, after blind set 2): the special lists alone missed indirect special
    categories in ordinary words, as they did for Lane H1's goals. Every word must be one H1's rule has vetted
    (`journal_goal_field._vetted`: its vocabulary, an OD-38 stem of it, a number or code, or a contraction of one).

    One exception: a value that is a single capitalised token the vocabulary lacks (a project, employer or place
    name), with no special-category root inside it. Such a value still meets guard 4 before this (Off-limits,
    whole terms and name parts) and guard 8 after it (the node's people and the entry's, relations, trades,
    honorifics), so a known person's name or name part never passes on the exception."""
    if all(jgf._vetted(word) for word in plain):
        return True
    return (len(raw) == 1 and raw[0][:1].isupper()
            and not any(root in plain[0] for root in jgf.SPECIAL_ROOTS))


ENTRY_COLUMNS = ("content", "people", "category", "mood_tag", "place_name")
MAX_METADATA_ITEMS = 2000


def _scan_words(text: str) -> list:
    """The plain words of free text as the special lists read them: format characters (zero-width, soft hyphen,
    tag characters) and marks removed and look-alike letters mapped first (the boundary's own normaliser), so a
    split or disguised word is read whole."""
    from .entity_boundary import normalized
    return [jgf._plain(word) for word in jgf._TOKEN.findall(normalized(jgf._fold(text)))]


def _key_words(key) -> list:
    """A metadata key or a label line's key as words: "isSensitive", "privacy_level", "Privacy level"."""
    return _scan_words(re.sub(r"([a-z])([A-Z])", r"\1 \2", str(key)).replace("_", " ").replace("-", " "))


def _column(entry, name):
    try:
        return entry[name]           # the row as loaded: a dict, or a sqlite3.Row
    except (KeyError, IndexError, TypeError):
        return None


def _entry_parts(entry) -> tuple[list, list, list]:
    """(every text of the entry, its free text, its labels as (key words, value)). Texts: the content, people,
    category, mood and place columns, and every metadata key and string value; metadata that is not JSON is read
    as text. Free text: the same without the metadata keys (a key is read as a label, never as a line). Labels: each
    metadata key with its value (a prose key's value is text only), each label line of the content ("Tags: ...")
    and the category column."""
    texts, free, labels = [], [], []
    for name in ENTRY_COLUMNS:
        value = _column(entry, name)
        if isinstance(value, str) and value:
            texts.append(value)
            free.append(value)
    category = _column(entry, "category")
    if isinstance(category, str) and category:
        labels.append((["category"], category))
    content = _column(entry, "content")
    if isinstance(content, str):
        for line in content.splitlines():
            match = _LABEL_LINE.match(line)
            if match:
                words = _key_words(match.group(1))
                if 1 <= len(words) < MAX_LABEL_WORDS:
                    labels.append((words, match.group(2)))
    raw = _column(entry, "metadata_json")
    metadata = raw if isinstance(raw, (dict, list)) else None
    if isinstance(raw, str) and raw.strip():
        try:
            metadata = json.loads(raw)
        except ValueError:
            texts.append(raw)        # not JSON: its words are still the entry's
            free.append(raw)
    stack, seen = [(None, metadata)], 0
    while stack:
        key, value = stack.pop()
        seen += 1
        if seen > MAX_METADATA_ITEMS:
            raise ValueError("metadata too large to read")
        if isinstance(value, dict):
            for inner, item in value.items():
                texts.append(str(inner))
                stack.append((str(inner), item))
            continue
        if isinstance(value, list):
            stack.extend((key, item) for item in value)
            continue
        if isinstance(value, str):
            texts.append(value)
            free.append(value)
        if key is not None and value is not None:
            words = _key_words(key)
            if not PROSE_KEYS & set(words):
                labels.append((words, value))
    return texts, free, labels


def _is_set(value) -> bool:
    """A label value that says something: not False, 0, empty or one of UNSET ("none", "public", "personal", ...)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    words = _scan_words(str(value))
    return bool(words) and not all(word in UNSET for word in words)


def _marks(words: list) -> bool:
    """A label's words that mark the entry: a marker word or phrase, a special category named as a label, or a
    special-category cue by H1's lists."""
    return bool((MARKER_WORDS | SPECIAL_LABELS) & set(words)) \
        or any(jgf._has(words, phrase) for phrase in MARKER_PHRASES) or jgf._special(words, None)


def _marked_special(free: list, labels: list) -> bool:
    """Guard 2a: an explicit label marking the entry private, sensitive, confidential, special or a special
    category. A key that names a marker with a value set ({"private": true}, "Sensitivity: high"); a label's value
    that holds one ("Tags: private", {"category": "health"}); a sharing key that refuses ({"share": false}); a
    hashtag that holds one; a tag line or a bracketed tag opening or closing a line (`_tag`); or a marker phrase
    ("do not share", "eyes only") anywhere."""
    for key_words, value in labels:
        keys = set(key_words)
        if MARKER_WORDS & keys and _is_set(value):
            return True
        if SHARE_KEYS & keys and (value is False or value == 0 or (
                isinstance(value, str) and SHARE_REFUSED & set(_scan_words(value)))):
            return True
        if LABEL_KEYS & keys and _marks(_scan_words(str(value))):
            return True
    for text in free:
        words = _scan_words(text)
        if any(jgf._has(words, phrase) for phrase in MARKER_PHRASES):
            return True
        if any(_marks(_scan_words(tag.replace("-", " ").replace("_", " "))) for tag in _HASHTAG.findall(text)):
            return True
        for line in text.splitlines():
            stripped = line.strip()
            if _tag(_scan_words(stripped)):
                return True
            if any((match.start() == 0 or match.end() == len(stripped)) and _tag(_scan_words(match.group(1)))
                   for match in _BRACKETED.finditer(stripped)):
                return True
    return False


def _tag(words: list) -> bool:
    """A one-line or bracketed tag: at most MAX_LABEL_WORDS words, a marker or a special category named as a label
    among them, nothing but TAG_WORDS beside ("Private entry", "[Health]")."""
    found, markers = set(words), MARKER_WORDS | SPECIAL_LABELS
    return 0 < len(words) <= MAX_LABEL_WORDS and bool(markers & found) and found <= markers | TAG_WORDS


def _labels_are(labels, field: str, expected: str) -> bool:
    return getattr(labels, field, None) == expected


def _shape_refused(value) -> bool:
    if type(value) is not str or not 2 <= len(value.strip()) <= 200 or not 1 <= len(eg.tokens(value)) <= 12:
        return True
    try:
        atomic_label_syntax(value)
    except ValueError:
        return True
    if unicodedata.normalize("NFKC", value) != value:
        return True                  # compatibility forms and decomposed marks: never one plain label
    for ch in value:
        category = unicodedata.category(ch)
        if category[0] in ("C", "M"):
            return True              # controls, zero-width and other format characters, unassigned; combining marks
        if category[0] == "L" and not unicodedata.name(ch, "").startswith("LATIN "):
            return True              # the guards read English: a word they cannot read withholds
    return False


def _question_or_quote(value: str, low: list) -> bool:
    if "?" in value or any(mark in value for mark in jgf._QUOTES) or any(mark in value for mark in eg._QUOTES):
        return True
    last = len(value) - 1
    for index, ch in enumerate(value):
        if ch in "'\u2019":
            before = value[index - 1] if index else ""
            after = value[index + 1] if index < last else ""
            if not (before.isalnum() and (after.isalnum() or before in "sS")):
                return True          # an apostrophe that is not inside a word or a plural possessive
    opening = jgf._starts(low, 0, jgf.LEADING_TIME)
    return opening < len(low) and low[opening] in jgf.QUESTION_START


def _not_a_value(value: str, predicate, plain: list) -> bool:
    folded = value.casefold()
    if "://" in value or "www." in folded or any(ch in value for ch in "/@#") or _DOMAIN.search(value):
        return True                  # a URL, a handle or a path, or a domain
    if any(ch in TEMPLATE_CHARACTERS for ch in value):
        return True                  # template or code
    if jgf.PLACEHOLDERS & set(plain) or any(jgf._has(plain, phrase) for phrase in jgf.PLACEHOLDER_PHRASES):
        return True
    klass = CLASSES.get(predicate)
    echoed = {predicate.casefold()} if isinstance(predicate, str) else set()
    if klass is not None and klass.key:
        echoed.add(klass.key.casefold())
    if value.strip().casefold() in echoed:
        return True                  # the class's own field name, or the predicate, as the value
    return not any(ch.isalpha() for ch in value)   # nothing to say: digits and punctuation only


def _names_person(value: str, predicate, entry, people, raw: list, plain: list) -> bool:
    if people is None:
        return True                  # the node's people could not be read: no third party can be ruled out
    words = set(plain)
    try:
        listed = entry["people"]     # the row as loaded: a dict, or a sqlite3.Row
    except (KeyError, IndexError, TypeError):
        listed = None                # a row with no people column names no one there
    if (frozenset(people) | jgf._names_in(listed)) & words:
        return True                  # someone the node knows, or the entry says was there
    if jgf._PEOPLE & words or any(len(word) > 3 and eg.stem(word) in jgf._PEOPLE_STEMS for word in plain):
        return True                  # a pronoun, a relation or a role
    if any(jgf._has(plain, phrase) for phrase in jgf.THIRD_PARTY_PHRASES):
        return True
    if any(word.endswith(jgf.PERSON_SUFFIXES) and word not in jgf.PERSON_SUFFIX_EXEMPT and len(word) > 4
           for word in plain):
        return True                  # a person by trade or field
    if HONORIFICS & words:
        return True
    if TRADES & words or any(word.endswith(TRADE_ENDINGS) and word not in TRADE_ENDING_EXEMPT
                             and len(word) > 5 for word in plain):
        return True                  # a person by trade (v1b), under every predicate
    if any(word.endswith("'s") and word[:-2] not in jgf.TIME_WORDS for word in plain):
        return True                  # someone's: a possessive other than a time's
    if any(jgf._plain(match.group(1)) not in jgf.TIME_WORDS for match in _PLURAL_POSSESSIVE.finditer(jgf._fold(value))):
        return True                  # a plural possessive; "two weeks'" is a time's
    if predicate not in PROPER_NOUN_PREDICATES and any(jgf._name_like(word) for word in raw[1:]):
        return True                  # a capitalised word after the first, where the value is not a proper noun
    return False
