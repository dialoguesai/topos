"""verify-claim ``effort: high``: one free first-person sentence read as one Icebreakers answer, or refused.

The control plane's deterministic grammar (``truth_prose``, icebreakers-prose/v2.1) is the fast path: it maps about
half of natural phrasings and refuses the rest with no model. The control plane writes no prompts (owner rule, 1 Oct
2026), so the long tail is read here, on the owner's node, with the node's pinned local model
(``shadow_labeler_local.MODEL`` at ``MODEL_REVISION``). Only a statement the grammar refused, sent with
``effort: high`` to a node that advertises ``CAPABILITY_FIELD``, reaches this module; ``truth_statement`` is the door.

It changes only the interpretation. Evidence, consent and the verdict are the shipped structured check
(``features.calibration.store.verify``) on the answer read here. The model sees the caller's statement and the public
quiz; never an owner answer, record or name. Interpretation runs before the label lookup and does not depend on the
owner's answers, so neither its result nor the time it takes says anything about them.

One statement, inside one deadline (``DEADLINE_SECONDS``):

1. guards (``guard``, deterministic): a question, two sentences, no first person, another subject, negation,
   hedges and modality, a tense or occasion shift, frequency words, sarcasm, "or" between answers, contrast, a
   whole-personality trait, an order, liking with no answer frame, a second first-person clause, two quiz topics
   (each question's topic words and its own answer words). A wrong reading costs far more than a refusal, and these are
   the cases where a model over-accepts most (a frequency claim included); no model may override them.
2. examples: none ship with the node (owner, 20 Oct 2026: the few-shot file stays out of this public repository).
   An ``Interpreter`` given ``Examples`` shows the K most similar refused and K most similar accepted ones, by token
   overlap, refused ones first and each refusal labelled with its reason, so a near-identical refusal is never the
   last thing the model reads before the sentence (it then copies NONE onto a plain statement).
3. call A: one of the 15 answer codes or NONE, held to a JSON-schema enum, temperature 0.
4. topic anchor: the sentence must name the chosen question's topic (``_ANCHOR``), and a mixed answer must not name
   another question's answer (``mixed_across_questions``).
5. call B: the sentence against only that question's three answers or none. Accepted only when B says what A said.

Any timeout, unreviewed model, malformed output, NONE, missing anchor or disagreement refuses, and nothing was checked,
so a refusal is never an ``unknown`` verdict. A refusal decided by the statement is "not understood"; one caused by the
model's state (``OPERATIONAL_REASONS``) is not, so it says nothing to a recipient about how busy the node is.

Capacity: the owner's model is shared with ingestion, sharing assessments and answers, and runs one request at a
time. ``CAPACITY`` lets ``IN_FLIGHT`` interpretations run and ``WAITING`` wait (each within ``QUEUE_WAIT_SECONDS``);
past that the caller gets ``Busy`` (429 at the door). ``ALLOWANCE`` caps each grant's interpretations per hour
(``switches.TRUTH_INTERPRET_PER_GRANT_PER_HOUR``). Both are this process's, so a restart resets them.
"""
from __future__ import annotations

import collections
import contextlib
import json
import re
import threading
import time
import unicodedata
import urllib.request
from dataclasses import dataclass, field

from .truth_contract import BY_ID, PACK

VERSION = "icebreakers-high/v4"
#: In the node's heartbeat capabilities (``engine.registration``): 1 means this node reads a free statement here and
#: answers ``permissions.v2.verify_claim_statement`` and ``permissions.v2.interpret_claim`` (with the
#: ``interpretation_id`` link on ``permissions.v2.verify_claim``); 0 or absent, it does not.
CAPABILITY_FIELD = "permissions_v2_truth_interpret_version"
CAPABILITY_VERSION = 1
#: The longest the two model calls together may take for one statement, from the start of interpretation.
DEADLINE_SECONDS = 10.0
#: The longest a statement waits for the model slot before the caller is told the node is busy.
QUEUE_WAIT_SECONDS = 5.0
IN_FLIGHT = 1
WAITING = 4
#: Examples of each kind (refused, accepted) shown to call A when an interpreter is given any.
K = 5
SEED = 7
#: The model host's reply to `/api/tags` is trusted for this long before the digest is read again.
TAGS_TTL_SECONDS = 60.0
TAGS_TIMEOUT_SECONDS = 2.0

QUESTIONS = {
    "drink": ("What is your go-to drink?", {"coffee": "coffee", "tea": "tea", "neither": "neither coffee nor tea"}),
    "time": ("When do you feel most like yourself?", {"morning": "morning", "evening": "evening", "it-varies": "it varies"}),
    "weekend": ("Pick your ideal weekend.", {"outdoors": "outdoors", "at-home": "at home", "a-bit-of-both": "a bit of both"}),
    "planning": ("How do you like to make plans?", {"plan-ahead": "plan ahead", "be-spontaneous": "be spontaneous", "a-bit-of-both": "a bit of both"}),
    "company": ("How do you recharge?", {"alone": "alone", "with-people": "with people", "a-bit-of-both": "a bit of both"}),
}
assert all(list(choices) == BY_ID[q]["choices"] for q, (_, choices) in QUESTIONS.items())
CODES = [f"{q}={v}" for q, (_, choices) in QUESTIONS.items() for v in choices] + ["NONE"]
# The control plane's wording of each answer (truth_prose.PROPOSITIONS), so the confirm step shows what is checked.
_PROPOSITIONS = {"drink": "Your go-to drink is {}.", "time": "You feel most like yourself in the {}.",
                 "weekend": "Your ideal weekend is {}.", "planning": "You like to make plans: {}.",
                 "company": "You recharge {}."}

SYSTEM_A = """You are a strict interpreter for a party game. Earlier, the speaker answered a five-question quiz. Now they say one sentence about themself. Decide whether that sentence, taken literally, says the same thing as giving exactly one quiz answer. If it does, return that answer's code. Otherwise return NONE. A wrong answer is far worse than NONE.

Quiz:
1. What is your go-to drink? drink=coffee | drink=tea | drink=neither (neither coffee nor tea)
2. When do you feel most like yourself? time=morning | time=evening | time=it-varies
3. Pick your ideal weekend. weekend=outdoors | weekend=at-home | weekend=a-bit-of-both
4. How do you like to make plans? planning=plan-ahead | planning=be-spontaneous | planning=a-bit-of-both
5. How do you recharge? company=alone | company=with-people | company=a-bit-of-both

Return NONE when the sentence:
- is about someone other than the speaker;
- negates or hedges the claim, or is about the past, the future or one specific occasion;
- makes two separate claims, or could answer more than one question;
- only says the speaker likes, enjoys, owns or often does something related, without saying it is their go-to drink, ideal weekend, way to recharge, way of making plans, or the time they feel most like themself;
- names a broad personality trait (introvert, extrovert, ambivert, homebody) or uses always or every time;
- is a question, a joke, sarcasm, or too vague to pick one answer.
Preferences and identities that pick one side do count ("I prefer tea", "I'm a tea person", "I'm a morning person"), and so do idioms that clearly mean one answer. A mix of both sides of a question (half and half, both, a balance) is that question's a-bit-of-both answer; for time it is time=it-varies; for drink, liking both coffee and tea is NONE."""

SYSTEM_B = """You check one sentence a party-game player said about themself against one quiz question. Say which of the listed answers the sentence gives, taken literally and as a general preference (not one occasion). If it does not clearly give exactly one of them, or it only says the player likes, enjoys or does something related, reply none."""

# --- the control plane grammar's normalisation (truth_prose v2.1), restated so both read a statement alike --------
MAX_CHARS = 280
_LEAD_INS = re.compile(r"^(?:honestly|truthfully|frankly|to be honest|well|oh|ok|okay|yeah|yes|so|for the record|"
                       r"fun fact|full disclosure|i will tell you|i will say|i can tell you)\b,? *")
_INTENSIFIERS = re.compile(r"\b(?:really|definitely|totally|absolutely|truly|100 percent|for sure)\b")
_FIRST_PERSON = re.compile(r"\b(?:i|my|me|myself|mine)\b")
_RELATIONS = (r"mom|mother|dad|father|sister|brother|friend|friends|partner|wife|husband|boyfriend|girlfriend|son|"
              r"daughter|kid|kids|family|boss|cofounder|co founder|roommate|team|colleague|coworker|parents|neighbor")


def normalize(text: str) -> str:
    """Case, quotes, contractions, hyphens and punctuation folded; meaning-bearing words untouched."""
    t = unicodedata.normalize("NFKC", text).replace("’", "'").replace("‘", "'")
    t = t.replace("“", '"').replace("”", '"').lower().strip().strip('"').strip()
    t = re.sub(r"[.!]+$", "", t).strip()
    t = t.replace("&", " and ")
    t = re.sub(r"\bwon't\b", "will not", t)
    t = re.sub(r"\bcan't\b", "can not", t)
    t = re.sub(r"n't\b", " not", t)
    t = re.sub(r"\bi'm\b", "i am", t)
    t = re.sub(r"\bi've\b", "i have", t)
    t = re.sub(r"\bi'd rather\b", "i prefer to", t)
    t = re.sub(r"\bi would rather\b", "i prefer to", t)
    t = re.sub(r"\bi'd\b", "i would", t)
    t = re.sub(r"\bi'll\b", "i will", t)
    t = re.sub(r"\blet's\b", "let us", t)
    t = re.sub(r"\bit's\b", "it is", t)
    t = re.sub(r"\bthat's\b", "that is", t)
    t = re.sub(r"(?<=\w)-(?=\w)", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _flat(t: str) -> str:
    """The template view: lead-ins and intensifiers dropped, commas removed."""
    previous = None
    while previous != t:
        previous, t = t, _LEAD_INS.sub("", t)
    t = _INTENSIFIERS.sub(" ", t).replace(",", " ")
    return re.sub(r"\s+", " ", t).strip()


# Every word of the grammar's cue phrases (truth_prose._CUES): a capitalised first word that is one of these is a quiz
# word, not a person ("Tea beats coffee for me").
_QUIZ_WORDS = frozenset((
    "a about advance ahead alive all alone and approach around as at back batteries be being best beverage bird by "
    "choice coffee come comes company day default doors dream drink drinker early energy evening evenings everyday "
    "favorite favourite feel first flow fly for friends get getting go going good great heaven home idea ideal in "
    "indoors it keep keeping kind last late like locked love make making me minute moment morning mornings most my "
    "myself nature neither night nights nighttime nor of on other others out outdoors outdoorsy outside owl own "
    "people perfect person plan planned planner planning plans prefer recharge recharges recharging refuel refuels "
    "social socialising socializing solitude solo sort spontaneous spontaneously spur staying style tea team the "
    "thing things time to type usual weekend weekends well when wing winging with").split()) | {
    "mornings", "evenings", "weekends", "solitude"}

# --- guards -----------------------------------------------------------------------------------------------------
_CONTRAST = re.compile(r"(?<![\w])(?:but|although|though|except|whereas|however|yet)(?![\w])")
_AUX_QUESTION = re.compile(r"^(?:is|am|are|do|does|did|can|could|would|should|will|was|were|have|has)\b")
_OTHER_START = re.compile(r"^(?:he|she|they|you|we|his|her|their|your|our|everyone|everybody|someone|somebody)\b"
                          r"|^my (?:" + _RELATIONS + r")\b")
_REPORT_VERBS = frozenset("says said thinks thought prefers likes loves recharges feels drinks swears knows "
                          "reckons insists claims tells told reckoned".split())  # no copula: "Espresso is my drink" names a drink
_TRAIT = re.compile(r"(?<![\w])(?:introvert|introverted|introverts|extrovert|extroverted|extravert|extraverted|ambivert|"
                    r"homebody|home body|couch potato|social butterfly|people person|loner|hermit|party animal|"
                    # whole-personality framing: "a spontaneous soul", "a free spirit", "a planner at heart"
                    r"soul|free spirit|spirit|creature|at heart|by nature|deep down|personality|temperament)(?![\w])")
_ORDER = re.compile(r"^(?:(?:coffee|tea|neither|nor|or|and|a|the|cup|of|black|green|herbal|just|please|thanks|thank you|"
                    r"one|for me|me)\s*)+$")
_TOPIC = {  # explicit topic words; two different questions named means the statement is not one quiz answer
    "drink": re.compile(r"(?<![\w])(?:coffee|tea|drink|drinks|beverage|latte|espresso|chai|matcha|caffeine|cuppa)(?![\w])"),
    "time": re.compile(r"(?<![\w])(?:morning|mornings|evening|evenings|nighttime|early bird|night owl|sunrise|sunset|dawn)(?![\w])"),
    "weekend": re.compile(r"(?<![\w])(?:weekend|weekends|saturday|saturdays|sunday|sundays)(?![\w])"),
    "planning": re.compile(r"(?<![\w])(?:plan|plans|planning|planner|planned|spontaneous|spontaneity|schedule|itinerary)(?![\w])"),
    "company": re.compile(r"(?<![\w])(?:recharge|recharges|recharging|refuel|refuels|refueling|batteries|my battery|"
                          # the recharge choices name the topic too: "an outdoor weekend by myself" touches two questions
                          r"by myself|on my own|solitude|solo|with friends|with people|around people|with other people|"
                          r"crowd|crowds|socializing|socialising)(?![\w])"),
}
# After the model: the statement must name the chosen question's topic (a choice word counts where it is unambiguous).
_ANCHOR = {
    "drink": re.compile(r"(?<![\w])(?:coffee|tea|drink\w*|beverage|cup|cuppa|mug|pot|brew\w*|caffeine|java|joe|latte|"
                        r"espresso|cappuccino|americano|mocha|macchiato|flat white|cold brew|chai|earl grey|green tea|herbal|"
                        r"matcha|rooibos|water|sparkling|seltzer|soda|juice|kombucha|milk|hot chocolate|cocoa|smoothie|"
                        r"lemonade|sip\w*|go to)(?![\w])"),
    # A time-of-day word, or the question's own frame (feeling most like oneself). Never a bare "me" or "myself":
    # every first-person sentence has one ("It varies for me" names no question).
    "time": re.compile(r"(?<![\w])(?:morning|mornings|evening|evenings|night|nights|nighttime|day|days|daytime|dark|dawn|"
                       r"daybreak|sunrise|sunup|sunset|sundown|dusk|sun|breakfast|midnight|noon|late|early|hour|hours|lark|"
                       r"owl|early bird|afternoon|afternoons|bedtime|am|pm|o'clock|clock|time of day|"
                       r"(?:most|truest|true|real|actual|authentic\w*|fully|properly|best) (?:\w+ ){0,2}(?:me|myself|self)|"
                       r"(?:feel|feels|felt|feeling) (?:\w+ ){0,3}(?:me|myself|self))(?![\w])"),
    "weekend": re.compile(r"(?<![\w])(?:weekend|weekends|saturday|saturdays|sunday|sundays)(?![\w])"),
    "planning": re.compile(r"(?<![\w])(?:plan\w*|schedul\w*|spontane\w*|wing it|winging it|wing it person|on the fly|spur|"
                           r"advance|ahead|last minute|itinerar\w*|calendar|flow|improvis\w*|mapped out|organi[sz]\w*|"
                           r"seat of my pants|by ear|on the day|day of|decid\w*|decision\w*|book\w*|spreadsheet\w*|"
                           r"flexib\w*|loose|structure\w*|whim\w*|beforehand)(?![\w])"),
    "company": re.compile(r"(?<![\w])(?:recharg\w*|energy|energi[sz]\w*|refuel\w*|refill\w*|batter(?:y|ies)|tank|"
                          r"recover\w*|unwind\w*|decompress\w*|reset|restor\w*|rejuvenat\w*|reviv\w*|drain\w*|"
                          r"recuperat\w*|wind down|charge\w*|top up|topped up)(?![\w])"),
}
#: How each refused example is labelled for the model.
WHY = {"negation": "negated", "hedge_or_modality": "hedged, or not about now in general", "compound": "two claims",
       "question": "a question", "other_subject": "about someone else",
       "related_not_equivalent": "related but weaker than a quiz answer", "vague_or_no_topic": "names no quiz answer",
       "ambiguous_multiple_topics": "touches more than one question", "policy": "a trait, an order or a frequency claim"}
_STOP = frozenset("i am is are a an the my me myself of to for in at on with by from it that what when how one more much most just all be being".split())
_SAFE_IDIOMS = re.compile(r"(?<![\w])(?:no contest|no question about it|no question|no doubt about it|no doubt|hands down|full stop|period|"
                          r"kind of person|sort of person|kind of guy|kind of girl|kind of gal|type of person|last minute)(?![\w])")
_NEG = re.compile(r"(?<![\w])(?:not|never|no|hate|hates|dislike|dislikes|cannot|can not|nope|no way|no longer)(?![\w])")
# "nobody around" or "without anyone" can describe a choice (alone); only a denial refuses.
_HEDGE = re.compile(r"(?<![\w])(?:think|guess|suppose|maybe|perhaps|probably|possibly|might|may|could|should|will|would|going to|gonna|"
                    r"used to|sometimes|usually|often|occasionally|kind of|kinda|sort of|sorta|mostly|generally|typically|tend to|"
                    r"try to|trying to|want to|wanna|hope|wish|if|someday|lately|these days|nowadays|this weekend|this morning|"
                    r"tonight|today|tomorrow|yesterday|last week|last weekend|last night|last year|last month|next week|next weekend|"
                    r"next month|next year|next time|from now on|any more|anymore|apparently|"
                    r"supposedly|pretty sure|i bet|always|every time|every single time|whenever|for now|at the moment|right now)(?![\w])")
# v4: how often, in the control plane grammar's own words, and a quantified occasion ("on most occasions", "most days").
_FREQUENCY = re.compile(r"(?<![\w])(?:(?:most|many|half|each|nine) (?:of )?(?:the |my )?(?:times|days|occasions|nights|mornings|"
                        r"evenings|weekends)|(?:most|half|all|much|some) of the time|all the time|half the time|"
                        r"more often than not|for the most part|as a rule|at times|rarely|seldom|on occasion|times out of ten|"
                        r"nearly always|almost always|hardly ever)(?![\w])")
# v4: liking is weaker than an answer ("I like tea", "I'm fond of mornings"): a liking verb is refused unless the
# sentence also frames an answer (a preference, an identity, a mix, or a question's own words). "Like to" is the
# planning question's own frame ("How do you like to make plans?"), not liking.
_LIKING = re.compile(r"(?<![\w])(?:(?:like|likes|love|loves)(?! to(?![\w]))|enjoy|enjoys|adore|adores|fond of|keen on|"
                     r"am (?:so |very |big |totally )?into|fan of|partial to|soft spot)(?![\w])")
_FRAME = re.compile(r"(?<![\w])(?:prefer\w*|rather|over|than|beats|favou?rite|ideal|perfect|dream|best|go to|of choice|default|"
                    r"usual|first choice|top|staple|team|person|drinker|camp|most|feel\w*|recharg\w*|energy|batter\w*|"
                    r"restor\w*|unwind\w*|refuel\w*|plan\w*|schedul\w*|spontane\w*|wing\w*|improvis\w*|advance|ahead|"
                    r"beforehand|weekend\w*|how i|way|mix|both|balance|blend|half|varies|depends)(?![\w])")
# v4: one occasion, whatever the unit: this, next, last or the coming week, weekend, evening, season, weekday; or an
# event named as this one ("for this reunion", "at that party").
_OCCASION = re.compile(r"(?<![\w])(?:(?:this|next|last|coming|upcoming) (?:week|weekend|month|year|morning|afternoon|evening|"
                       r"night|summer|winter|spring|autumn|fall|season|holiday|holidays|trip|monday|tuesday|wednesday|"
                       r"thursday|friday|saturday|sunday)s?"
                       r"|(?:for|on|at|during|until|after|before|over) (?:this|that|tonight's|today's|tomorrow's) "
                       r"(?!is(?![\w])|way(?![\w])|one(?![\w]))\w+)(?![\w])")
_SARCASM = re.compile(r"^(?:oh,? )?(?:sure|yeah,? right|as if)\b|(?<![\w])(?:yeah right|as if|just kidding|kidding|jk|said no one|not!)(?![\w])")
_OR = re.compile(r"(?<![\w])or(?![\w])")
# v4: "..., and I have ...", "... plus my ...": a second clause with its own first-person subject is a second claim,
# whichever word joins it.
_SECOND_CLAUSE = re.compile(r"(?<![\w])(?:and|plus|also|then|besides|as well as) (?:i|my)(?![\w])")
# v4: each question's own answer words. A mixed answer ("a bit of both") mixes one question's two sides; a sentence
# read as one that names another question's answer ("a balance of outdoors and solitude" to recharge) mixes two.
_CHOICE_WORDS = {
    "drink": r"coffee|tea",
    "time": r"morning|mornings|evening|evenings",
    "weekend": r"outdoors|outdoor|outside|indoors|at home|staying in",
    "planning": r"plan ahead|planning ahead|spontaneous|spontaneously",
    # the recharge question's two sides as a category: being alone, and being with other people
    "company": r"alone|solitude|solo|by myself|on my own|people|friends|friend|others|company|crowd|crowds|group|groups|"
               r"social|socializing|socialising|gathering|gatherings",
}
_OTHER_CHOICES = {q: re.compile(r"(?<![\w])(?:" + "|".join(w for o, w in _CHOICE_WORDS.items() if o != q) + r")(?![\w])")
                  for q in _CHOICE_WORDS}
# v4: each question's family, its topic words and its own answer words. A sentence that names two families touches
# two questions ("quiet evenings alone": a time and a way to recharge), whichever of its words are topic words.
_FAMILY = {q: re.compile(rx.pattern[:-len(r")(?![\w])")] + "|" + _CHOICE_WORDS[q] + r")(?![\w])") for q, rx in _TOPIC.items()}
_VARIES = re.compile(r"(?<![\w])(?:depends|depending|vary|varies|changes|change|different|whichever)(?![\w])")


def guard(statement) -> str | None:
    """The deterministic refusals that no model may override: the reason, or None when the model may read it."""
    if not isinstance(statement, str) or not statement.strip():
        return "vague_or_no_topic"
    if len(statement) > MAX_CHARS:
        return "too_long"
    raw = normalize(statement)
    if "?" in raw:
        return "question"
    if _SARCASM.search(raw):
        return "hedge_or_modality"  # insincere: irony is not an assertion
    flat = _flat(raw)
    if _AUX_QUESTION.match(flat):
        return "question"
    if re.search(r"[.!;]", raw):
        return "compound"
    if not _FIRST_PERSON.search(flat):
        return "vague_or_no_topic"
    words = _LEAD_INS.sub("", statement.strip().lstrip("\"'")).split()
    # "Rowan prefers tea": a capitalised non-quiz word followed by a third-person verb names someone else
    # ("Tea beats coffee for me" does not).
    named = (len(words) > 1 and words[0].isalpha() and words[0][:1].isupper() and words[0] != "I"
             and words[0].lower() not in _QUIZ_WORDS and words[1].lower().strip(",") in _REPORT_VERBS)
    if _OTHER_START.match(flat) or named or re.search(r"(?<![\w])(?:we|our|us|you|your)(?![\w])", flat):
        return "other_subject"
    scrubbed = _SAFE_IDIOMS.sub(" ", flat)
    if _NEG.search(re.sub(r"neither (?:\w+ ){0,4}?nor", " ", scrubbed)):
        return "negation"
    if _HEDGE.search(scrubbed) or _FREQUENCY.search(scrubbed) or _OCCASION.search(scrubbed):
        return "hedge_or_modality"
    if _OR.search(flat) and not _VARIES.search(flat):
        return "compound"  # alternatives offered instead of one answer ("whether ... or ... depends" is it-varies)
    if _CONTRAST.search(flat):
        return "compound"  # "but", "although": two claims set against each other, never one mixed answer
    if _SECOND_CLAUSE.search(flat):
        return "compound"
    if _TRAIT.search(flat):
        return "related_not_equivalent"  # policy: a broad personality trait is a different claim
    if _ORDER.match(flat):
        return "related_not_equivalent"  # policy: an order ("tea for me") asserts no preference
    if _LIKING.search(flat) and not _FRAME.search(flat):
        return "related_not_equivalent"
    if len([q for q, rx in _FAMILY.items() if rx.search(flat)]) > 1:
        return "ambiguous_multiple_topics"
    return None


def anchored(question_id: str, statement: str) -> bool:
    return bool(_ANCHOR[question_id].search(_flat(normalize(statement))))


def mixed_across_questions(question_id: str, value: str, statement: str) -> bool:
    """A mixed answer read from a sentence that names another question's answer (v4)."""
    return value == "a-bit-of-both" and bool(_OTHER_CHOICES[question_id].search(_flat(normalize(statement))))


def proposition(question_id: str, value: str) -> str:
    """The answer in words, as the player is shown it ("Your go-to drink is coffee.")."""
    if question_id == "time" and value == "it-varies":
        return "When you feel most like yourself varies."
    return _PROPOSITIONS[question_id].format(QUESTIONS[question_id][1][value])


# --- examples -----------------------------------------------------------------------------------------------------
def _tokens(text):
    return {t for t in re.findall(r"[a-z]+", normalize(text)) if t not in _STOP}


class Examples:
    """Labelled synthetic statements to show call A. Rows are (statement, code, why), code "NONE" for a refusal.
    None ship with the node; an interpreter without them sends the sentence alone."""

    def __init__(self, rows):
        self.rows = [(s, c if c != "NONE" else "NONE (" + WHY.get(why, "not one quiz answer") + ")", _tokens(s))
                     for s, c, why in rows]

    def nearest(self, statement, k=K, exclude=None):
        mine = _tokens(statement)
        score = lambda toks: len(mine & toks) / (len(mine | toks) or 1)
        cands = [(score(t), s, c) for s, c, t in self.rows if s != exclude]
        cands.sort(key=lambda x: -x[0])
        acc = [x for x in cands if not x[2].startswith("NONE")][:k]
        rej = [x for x in cands if x[2].startswith("NONE")][:k]
        # Refused examples first, then accepted ones, each by rising similarity: a near-identical refusal must not be
        # the last thing the model reads before the sentence (it then copies NONE).
        ordered = sorted(rej, key=lambda x: x[0]) + sorted(acc, key=lambda x: x[0])
        return [(s, c) for _, s, c in ordered]


# --- the model ----------------------------------------------------------------------------------------------------
class ModelUnavailable(Exception):
    """The pinned model could not be asked, did not answer in time, or is not the reviewed one: a refusal."""


#: Refusals that say how the node is doing, not what the statement says: a door must not answer them as "not
#: understood" (DR-04: no new way for a recipient to tell that the owner's node is busy).
OPERATIONAL_REASONS = frozenset({"deadline", "model_error", "model_unreachable", "model_unreviewed"})


class PinnedChat:
    """One `/api/chat` call with a JSON-schema answer, at the node's configured model host, against the reviewed tag
    and digest (the sharing paths' pinned model). Never a pull, a fallback or a launch, and no proxy from the
    environment. `num_ctx` is left to the host unless set: asking another context than the node's other model work
    reloads the model on the host."""

    def __init__(self, base_url: str | None = None, *, num_ctx: int | None = None):
        from .shadow_labeler_local import MODEL, MODEL_REVISION, configured_base_url
        self.model, self.revision = MODEL, MODEL_REVISION
        self.base_url = str(base_url or configured_base_url()).rstrip("/")
        self.num_ctx = num_ctx
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._checked_at = None
        self._lock = threading.Lock()

    def _post(self, path, body, timeout):
        request = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with self._opener.open(request, timeout=max(0.5, timeout)) as response:
            return json.loads(response.read().decode("utf-8"))

    def verify(self) -> None:
        with self._lock:
            if self._checked_at is not None and time.monotonic() - self._checked_at < TAGS_TTL_SECONDS:
                return
        try:
            request = urllib.request.Request(self.base_url + "/api/tags", method="GET")
            with self._opener.open(request, timeout=TAGS_TIMEOUT_SECONDS) as response:
                models = json.loads(response.read().decode("utf-8")).get("models") or []
        except Exception as exc:  # noqa: BLE001 -- a host that did not list its models has not been heard from
            raise ModelUnavailable("model_unreachable") from exc
        installed = [row for row in models if isinstance(row, dict) and row.get("name") == self.model]
        if len(installed) != 1 or installed[0].get("digest") != self.revision:
            raise ModelUnavailable("model_unreviewed")
        with self._lock:
            self._checked_at = time.monotonic()

    def __call__(self, system: str, user: str, schema: dict, timeout: float) -> dict:
        if timeout <= 0:
            raise ModelUnavailable("deadline")
        self.verify()
        options = {"temperature": 0, "seed": SEED}
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        try:
            reply = self._post("/api/chat", {"model": self.model, "stream": False, "format": schema, "think": False,
                                             "options": options,
                                             "messages": [{"role": "system", "content": system},
                                                          {"role": "user", "content": user}]}, timeout)
        except Exception as exc:  # noqa: BLE001 -- timeout, refused connection, error status: no answer
            raise ModelUnavailable("model_error") from exc
        if not isinstance(reply, dict) or reply.get("model") != self.model or reply.get("done") is not True:
            raise ModelUnavailable("model_error")
        try:
            answer = json.loads((reply.get("message") or {}).get("content") or "")
        except (TypeError, ValueError) as exc:
            raise ModelUnavailable("malformed_model_output") from exc
        if not isinstance(answer, dict):
            raise ModelUnavailable("malformed_model_output")
        return answer


@dataclass(frozen=True)
class Reading:
    """What one statement was read as. `question_id`/`value` are None for a refusal; `reason` is None when accepted.
    `stage` is where it ended (guard, model_a, anchor, model_b), `ms` each stage's time, `proposed` the code call A
    gave when a later stage refused it. No statement text."""
    question_id: str | None
    value: str | None
    stage: str
    reason: str | None
    ms: dict = field(default_factory=dict)
    proposed: str | None = None

    @property
    def accepted(self) -> bool:
        return self.question_id is not None

    @property
    def operational(self) -> bool:
        """Refused because the model could not be asked or did not finish, not because of what was said."""
        return not self.accepted and self.reason in OPERATIONAL_REASONS

    def interpretation(self) -> dict:
        return {"pack_id": PACK, "question_id": self.question_id, "value": self.value,
                "proposition": proposition(self.question_id, self.value)}

    def alternatives(self) -> list:
        return [{"value": v, "proposition": proposition(self.question_id, v)}
                for v in QUESTIONS[self.question_id][1] if v != self.value]

    def diagnostics(self) -> dict:
        """The owner's view of a reading: where it ended and why, with stage times. Never the recipient's."""
        return {"stage": self.stage, "reason": self.reason, "proposed": self.proposed, "ms": dict(self.ms),
                "interpreter": VERSION}


class Interpreter:
    def __init__(self, chat=None, examples: Examples | None = None, *, deadline_s: float = DEADLINE_SECONDS, k: int = K):
        self.chat = chat if chat is not None else PinnedChat()
        self.examples = examples
        self.deadline_s, self.k = deadline_s, k

    def interpret(self, statement, *, exclude=None) -> Reading:
        """Never raises for a statement: every failure is a refusal."""
        started = time.perf_counter()
        ms = {}
        mark = lambda name, t0: ms.__setitem__(name, round((time.perf_counter() - t0) * 1000, 2))
        left = lambda: self.deadline_s - (time.perf_counter() - started)
        t0 = time.perf_counter()
        reason = guard(statement)
        mark("guard", t0)
        if reason:
            return Reading(None, None, "guard", reason, ms)
        t0 = time.perf_counter()
        examples = self.examples.nearest(statement, k=self.k, exclude=exclude) if self.examples and self.k else []
        mark("retrieval", t0)
        shots = "\n".join(f"Sentence: {s}\nAnswer: {c}" for s, c in examples)
        user = (f"Examples:\n{shots}\n\n" if shots else "") + f"Sentence: {statement}\nAnswer:"
        try:
            t0 = time.perf_counter()
            first = self.chat(SYSTEM_A, user, {"type": "object", "properties": {"option": {"type": "string", "enum": CODES}},
                                               "required": ["option"]}, left())
            mark("model_a", t0)
        except ModelUnavailable as exc:
            return Reading(None, None, "model_a", str(exc), ms)
        except Exception:  # noqa: BLE001 -- whatever the transport did, nothing was read
            return Reading(None, None, "model_a", "model_error", ms)
        code = first.get("option")
        if code not in CODES:
            return Reading(None, None, "model_a", "malformed_model_output", ms)
        if code == "NONE":
            return Reading(None, None, "model_a", "model_none", ms)
        question, value = code.split("=", 1)
        if not anchored(question, statement):
            return Reading(None, None, "anchor", "missing_topic_anchor", ms, code)
        if mixed_across_questions(question, value, statement):
            return Reading(None, None, "anchor", "mixed_across_questions", ms, code)
        prompt, choices = QUESTIONS[question]
        listing = "\n".join(f"- {v}: {label}" for v, label in choices.items()) + "\n- none: the sentence does not clearly give one of these"
        try:
            t0 = time.perf_counter()
            second = self.chat(SYSTEM_B, f"Quiz question: {prompt}\nAnswers:\n{listing}\n\nSentence: {statement}\nAnswer:",
                               {"type": "object", "properties": {"answer": {"type": "string", "enum": [*choices, "none"]}},
                                "required": ["answer"]}, left())
            mark("model_b", t0)
        except ModelUnavailable as exc:
            return Reading(None, None, "model_b", str(exc), ms, code)
        except Exception:  # noqa: BLE001
            return Reading(None, None, "model_b", "model_error", ms, code)
        if second.get("answer") != value:
            return Reading(None, None, "model_b", "verification_disagreed", ms, code)
        return Reading(question, value, "model_b", None, ms)


# --- capacity -----------------------------------------------------------------------------------------------------
class Busy(Exception):
    """No model slot within the wait, the queue is full, or the grant's hourly allowance is spent: try again later.
    Operational, never a verdict and never "not understood"."""

    def __init__(self, code: str = "interpret_busy"):
        super().__init__(code)
        self.code = code


class Capacity:
    """`in_flight` interpretations at once and `waiting` more queued; anyone beyond is Busy at once."""

    def __init__(self, in_flight: int = IN_FLIGHT, waiting: int = WAITING):
        self._slots = threading.BoundedSemaphore(in_flight)
        self._limit = in_flight + waiting
        self._admitted = 0
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def slot(self, wait_s: float = QUEUE_WAIT_SECONDS):
        with self._lock:
            if self._admitted >= self._limit:
                raise Busy()
            self._admitted += 1
        try:
            if not self._slots.acquire(timeout=max(0.0, wait_s)):
                raise Busy()
            try:
                yield
            finally:
                self._slots.release()
        finally:
            with self._lock:
                self._admitted -= 1


class Allowance:
    """At most `per_hour` interpretations per grant in any rolling hour, counted in this process."""

    def __init__(self, per_hour=None, clock=time.monotonic):
        self._per_hour, self._clock = per_hour, clock
        self._seen = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def limit(self) -> int:
        if self._per_hour is not None:
            return int(self._per_hour)
        from . import switches
        return switches.number(switches.TRUTH_INTERPRET_PER_GRANT_PER_HOUR)

    def take(self, grant_id: str) -> None:
        now, limit = self._clock(), self.limit()
        with self._lock:
            for key in [key for key, seen in self._seen.items() if not seen or seen[-1] <= now - 3600]:
                del self._seen[key]  # grants quiet for an hour are forgotten
            seen = self._seen[grant_id]
            while seen and seen[0] <= now - 3600:
                seen.popleft()
            if len(seen) >= limit:
                raise Busy("interpret_allowance")
            seen.append(now)


CAPACITY = Capacity()
ALLOWANCE = Allowance()
_default = None
_default_lock = threading.Lock()


def default_interpreter() -> Interpreter:
    """The node's interpreter: the pinned model at the configured host, no examples (made once)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Interpreter()
        return _default


def available() -> bool:
    """The pinned model is ready on the host (the checking model's status)."""
    try:
        default_interpreter()
        from .checking_model import status
        return status()["status"] == "ready"
    except Exception:  # noqa: BLE001 -- anything unreadable is not available
        return False


def read(statement: str, *, interpreter: Interpreter | None = None, wait_s: float = QUEUE_WAIT_SECONDS) -> Reading:
    """One statement through the node's model slot. Raises Busy when no slot comes free within `wait_s`."""
    interpreter = interpreter or default_interpreter()
    if guard(statement) is not None:  # a guard refusal needs no model and waits for no slot
        return interpreter.interpret(statement)
    with CAPACITY.slot(wait_s):
        return interpreter.interpret(statement)
