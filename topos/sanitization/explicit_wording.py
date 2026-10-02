"""The explicit-wording rule: decides a text's NSFW tag from its wording alone. Deterministic; no model.

It replaces a text classifier that could not tell explicit text from ordinary text (invented explicit sentences
and an invented sentence about dinner scored the same) and that read only the first 512 characters. The rule
reads the whole text and answers one question: does this text use explicit sexual wording?

**What flags.**

* *Unambiguous vocabulary* (``UNAMBIGUOUS``, ``_GUARDED``): words for sexual acts, explicit body slang and
  pornography that have no ordinary reading as a whole word. A few of them do have a known ordinary collocation
  (a rooster, a bird, a willow, a golf group, a steel structure, "food porn", "intellectual masturbation"), and each
  of those carries a guard that names its ordinary collocations; outside them the word flags.
* *Ambiguous words, by phrase* (``_FRAMED``): words whose ordinary use is the common one ("sex" as a form field,
  "cum laude", "turned on the lights", "hook up the monitor", "make out the words", "an oral exam", "naked eye").
  These never flag on their own. They flag only inside a phrase that states sexual activity, arousal or nudity:
  "had sex", "sex with her", "turns me on", "we hooked up last night", "made out with him", "got laid".

**What does not flag, on purpose.** Clinical anatomy and health vocabulary on its own, profanity used as an
expletive or an insult, flirtation ("sexy"), relationships and dating, news and policy vocabulary ("sexual
assault", "sex offender", "same-sex", "porn filter"), and the label "nsfw" itself: a label is not explicit wording.

**Limits (accepted).** The rule misses euphemism and innuendo ("slept with", "spent the night"), anything it has
no word for, misspellings and digits-for-letters, emoji, and every language but English. It knows nothing of
violence or drugs. A person's review label, not this rule, covers what a grant actually shares. An ambiguous word
is matched only in the phrases listed here, so an unusual sentence shape is missed rather than guessed at.

**Normalisation.** Like the Off-limits boundary where that is cheap: NFKD, case folded, default-ignorable and
format characters and combining marks read through, the boundary's pinned look-alike letters folded. Letters
stretched for emphasis ("sooo") are also read collapsed. Its transliteration and digits-as-letters readings are
not applied.

``RULE_ID`` names the rule and its version; a tag this rule wrote carries it. Any change to what flags is a new
version: stored tags written under another id are re-evaluated (``topos.disclosure.nsfw_tags``).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

RULE_FAMILY = "explicit-wording/"
RULE_ID = RULE_FAMILY + "v1"
TIER_UNAMBIGUOUS = "unambiguous"
TIER_PHRASE = "phrase"


class Verdict(NamedTuple):
    """Whether the text flags, and which kind of wording decided it. Never the words themselves."""

    flagged: bool
    tier: Optional[str] = None


NOT_FLAGGED = Verdict(False, None)

# --- normalisation ---------------------------------------------------------------------------------------------

# The Off-limits boundary's tables (entity_boundary._IGNORABLE and CONFUSABLES), restated: a test pins the two
# copies together, so neither can drift from the other unnoticed.
_IGNORABLE = dict.fromkeys([0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, *range(0x180B, 0x1810),
                            *range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x2070), 0x3164,
                            *range(0xFE00, 0xFE10), 0xFEFF, 0xFFA0, *range(0xFFF0, 0xFFF9), *range(0x1BCA0, 0x1BCA4),
                            *range(0x1D173, 0x1D17B), *range(0xE0000, 0xE1000)])
_LOOKALIKES = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "м": "m",
    "у": "y", "і": "i", "ј": "j", "Α": "a", "Β": "b", "Ε": "e", "Η": "h", "Ι": "i",
    "Κ": "k", "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t", "Χ": "x",
    "α": "a", "β": "b", "ε": "e", "η": "h", "ι": "i", "κ": "k", "μ": "m", "ν": "n",
    "ο": "o", "ρ": "p", "τ": "t", "χ": "x"})
# Typographic apostrophes and hyphens, read as the ASCII ones the tokens are written with.
_PUNCTUATION = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "′": "'", "´": "'",
                              "‐": "-", "‑": "-"})
_FIRST = {**_IGNORABLE, **_LOOKALIKES, **_PUNCTUATION}
_marks_table: Optional[dict] = None
_STRETCHED = re.compile(r"([^\W\d_])\1{2,}")


def _marks() -> dict:
    """Combining marks and format characters, dropped after decomposition. Built on the first non-ASCII text."""
    global _marks_table
    if _marks_table is None:
        _marks_table = {cp: None for cp in (*range(0x20000), *range(0xE0000, 0xE1000))
                        if unicodedata.category(chr(cp)) in ("Mn", "Mc", "Me", "Cf")}
    return _marks_table


def normalise(text: str) -> str:
    """The text as the rule reads it, case kept (two guards read a capital); tokens are case folded."""
    if text.isascii():
        return text
    value = unicodedata.normalize("NFKD", text.translate(_FIRST))
    return value.translate(_marks()).translate(_FIRST)


# --- tokens ----------------------------------------------------------------------------------------------------

# A word is letters and digits with inner apostrophes. A hyphen between two word characters joins them
# ("turn-on"). Quotes and emphasis marks are read through. Every other punctuation mark, symbol or line break ends
# a clause: no phrase is matched across one.
_TOKEN = re.compile(r"(?P<word>[^\W_]+(?:'[^\W_]+)*)|(?P<hyphen>(?<=[^\W_])-(?=[^\W_]))"
                    r"|(?P<through>['\"*_~`“”]+)|(?P<clause>[^\s\w]|[\r\n])")


class _Text:
    """The words of one text: folded, as written, each with its clause and whether a hyphen joins it to the last."""

    __slots__ = ("low", "raw", "clause", "joined", "n")

    def __init__(self, text: str) -> None:
        self.low: List[str] = []
        self.raw: List[str] = []
        self.clause: List[int] = []
        self.joined: List[bool] = []
        clause, hyphen = 0, False
        for match in _TOKEN.finditer(text):
            kind = match.lastgroup
            if kind == "word":
                word = match.group()
                self.low.append(word.casefold())
                self.raw.append(word)
                self.clause.append(clause)
                self.joined.append(hyphen)
                hyphen = False
            elif kind == "hyphen":
                hyphen = True
            elif kind == "clause":
                clause += 1
                hyphen = False
        self.n = len(self.low)

    def at(self, i: int, k: int) -> Optional[str]:
        """The word ``k`` places from word ``i`` when both are in one clause, else None."""
        j = i + k
        if 0 <= j < self.n and self.clause[j] == self.clause[i]:
            return self.low[j]
        return None

    def left(self, i: int, skip: frozenset = frozenset(), limit: int = 2) -> Optional[int]:
        """The index of the nearest word left of ``i`` in its clause, passing at most ``limit`` words of ``skip``."""
        j, passed = i - 1, 0
        while j >= 0 and self.clause[j] == self.clause[i]:
            if self.low[j] in skip and passed < limit:
                passed, j = passed + 1, j - 1
                continue
            return j
        return None

    def word(self, i: Optional[int]) -> Optional[str]:
        return None if i is None else self.low[i]

    def capitalised(self, i: int) -> bool:
        raw = self.raw[i]
        return raw[:1].isupper() and raw[1:].islower()


def _set(words: str) -> frozenset:
    return frozenset(words.split())


# --- shared word classes ---------------------------------------------------------------------------------------

_SUBJECTS = _set("i we he she they you u who ya yall y'all")
_SUBJECT_CONTRACTIONS = _set("i'm im he's hes she's shes we're they're theyre you're youre i've ive we've they've "
                             "you've i'd we'd he'd she'd they'd you'd i'll we'll he'll she'll they'll you'll")
_BE = _set("am is are was were be been being get gets got getting gotten feel feels feeling felt stay stayed "
           "seem seems seemed")
_CAUSE = _set("got gets get getting made makes make making has have had keeps kept leaves left")
_INTENSIFIERS = _set("so really very super totally incredibly extremely pretty kinda sorta quite still always "
                     "already hella fucking freaking too more most insanely ridiculously crazy fairly rather "
                     "absolutely completely a bit little lil kind of sort just also never not even real genuinely "
                     "honestly actually both all damn weirdly oddly strangely suddenly instantly immediately")
_ADVERBS = _set("just totally finally almost never also kinda sorta drunkenly actually already still then later "
                "basically definitely only ever even really probably maybe literally obviously apparently "
                "accidentally nearly always usually often sometimes")
_OBJECTS = _set("me him her you u them us ya")
_POSSESSIVES = _set("my his her your ur their our")
_DETERMINERS = _set("the a an this that these those its some any every each all both no")
_PEOPLE = _set("him her them you u me someone somebody anyone anybody people guys girls men women strangers "
               "randos everyone everybody eachother")
_PERSON_NOUNS = _set("guy girl dude chick man woman stranger coworker colleague classmate roommate bartender friend "
                     "friends ex boss neighbor neighbour crush bf gf boyfriend girlfriend husband wife partner date "
                     "rando random hottie match professor teacher student intern client married cute hot older "
                     "younger tinder bumble hinge grindr prostitute hooker escort")
_PERSON_LEADS = _set("a an some this that another one my his her your their our the")
# What may follow a verb that takes no object, when the phrase is the sexual one: nothing, or a word that carries
# the sentence on (a time, a manner, a conjunction). An object ("the monitor", "a check") is not here.
_CARRY_ON = _set("again once twice last tonight tonite yesterday today recently before after back sometime "
                 "regularly occasionally constantly drunk sober and but or lol lmao haha hahaha though tho when "
                 "while then so because cuz bc until til till already yet earlier later finally together rn af "
                 "too now right all every since during anyway anyways tbh honestly")
_PLACES = _set("party bar club wedding place apartment house dorm car backseat back bed bedroom room hallway "
               "kitchen shower rain parking elevator stairwell closet bathroom pool park theater theatre movie "
               "movies cinema dark library alley basement couch sofa beach floor dance porch roof rooftop balcony "
               "bench hood stairs concert prom hotel tent lake woods office")


def _person_follows(t: _Text, i: int) -> bool:
    """Whether the words after word ``i`` (a "with") name a person: "with her", "with a guy", "with my ex"."""
    first = t.at(i, 1)
    if first in _PEOPLE:
        return True
    if first == "each" and t.at(i, 2) == "other":
        return True
    return first in _PERSON_LEADS and t.at(i, 2) in _PERSON_NOUNS


def _near(t: _Text, i: int, cues: frozenset, span: int = 8) -> bool:
    """Whether any of ``cues`` stands within ``span`` words of word ``i``, clause boundaries aside."""
    return any(t.low[j] in cues for j in range(max(0, i - span), min(t.n, i + span + 1)) if j != i)


def _carries_on(t: _Text, i: int) -> bool:
    """Whether the sentence ends after word ``i`` or carries on without giving the verb an object."""
    following = t.at(i, 1)
    if following is None or following in _CARRY_ON:
        return True
    if following == "that":
        return t.at(i, 2) in _set("night evening day weekend summer time afternoon morning one")
    if following in ("in", "on", "at") and (t.at(i, 2) in _PLACES or t.at(i, 3) in _PLACES):
        return True
    if following == "for" and t.at(i, 2) in _set("a an the like hours ages months years weeks days what"):
        return True
    return following == "a" and t.at(i, 2) in _set("lot little bit few couple ton bunch")


# --- unambiguous vocabulary ------------------------------------------------------------------------------------

UNAMBIGUOUS = _set(
    "blowjob blowjobs handjob handjobs footjob footjobs rimjob rimjobs titjob titjobs "
    "deepthroat deepthroats deepthroated deepthroating cumshot cumshots precum jizz jizzed jizzing "
    "orgasm orgasms orgasmed orgasming ejaculate ejaculates ejaculated ejaculating ejaculation "
    "fellatio cunnilingus anilingus sext sexts sexted sexting sextape sextapes cybersex sexcapade sexcapades "
    "porno pornos pornstar pornstars pornhub xvideos xhamster xnxx redtube youporn brazzers hentai "
    "erotic erotica camgirl camgirls foreplay bdsm dominatrix gangbang gangbangs gangbanged doggystyle "
    "clit clits titty titties dildo dildos buttplug buttplugs fleshlight strapon fapping fapped upskirt "
    "nympho nymphomaniac fuckbuddy fuckbuddies dickpic dickpics")
# Two words that are explicit only together.
_UNAMBIGUOUS_PAIRS = {
    "blow": _set("job jobs"), "doggy": _set("style"), "missionary": _set("position"), "butt": _set("plug plugs"),
}


def _pair(t: _Text, i: int) -> Optional[str]:
    return TIER_UNAMBIGUOUS if t.at(i, 1) in _UNAMBIGUOUS_PAIRS[t.low[i]] else None


# --- guarded words: explicit unless in a named ordinary collocation -------------------------------------------

_COCK_BIRDS = _set("robin pheasant sparrow bird birds fight fights fighting crow crows crowed crowing feather "
                   "feathers")
# A valve ("drain cock", "sea cock"), a bird ("jungle cock"), a watch part, a weather vane.
_COCK_BEFORE = _set("half weather ball stop game fighting turkey moor drain sea pet plug hose fire air gauge sill "
                    "drip way escape balance draw ground bib bleed test try shutoff steam guinea heath jungle "
                    "sage sand snow chaparral roost black wood")
_COCK_AIMED = _set("head ear ears eyebrow eyebrows gun pistol rifle hammer hat leg wrist hip fist arm weapon "
                   "trigger revolver shotgun firearm bolt lever")
# A rooster, a tap or a firearm's hammer is talked about with these words nearby.
_COCK_CUES = _set("bred hen hens rooster roosters chicken chickens poultry crow crowing crowed cry bird birds fowl "
                  "feather feathers fighting farm barnyard faucet valve tap plumbing pipe drain firearm rifle "
                  "shotgun gun pistol hammer trigger vane domestic pheasant grouse turkey comb spur spurs dawn capon "
                  "capons")
_TITLES = _set("mr mrs ms dr de van prof professor sir")


def _name(t: _Text, i: int) -> bool:
    """A capitalised word right after a capitalised word or a title: a surname or a place, not the common word."""
    previous = t.left(i, limit=0)
    return (t.capitalised(i) and previous is not None
            and (t.capitalised(previous) or t.low[previous] in _TITLES))


def _cock(t: _Text, i: int) -> Optional[str]:
    after, second, before = t.at(i, 1), t.at(i, 2), t.at(i, -1)
    if t.joined[i] or (i + 1 < t.n and t.joined[i + 1]):       # "cock-a-hoop", "cock-up", "weather-cock"
        return None
    if after in ("up", "ups") or after in _COCK_BIRDS or before in _COCK_BEFORE or _name(t, i):
        return None
    if after == "and" and second in ("bull", "hen", "hens"):
        return None
    if after == "a" and second in ("doodle", "hoop", "leekie"):
        return None
    if after == "of" and second == "the":
        return None
    if after in _DETERMINERS | _POSSESSIVES | {"one's", "ones"} and second in _COCK_AIMED:
        return None
    return None if _near(t, i, _COCK_CUES) else TIER_UNAMBIGUOUS


_PUSSY_AFTER = _set("cat cats willow willows riot galore hat hats foot footing footed out bow ass boy bitch whipped "
                    "clover")
_PUSSY_BEFORE = _set("a total complete")


def _pussy(t: _Text, i: int) -> Optional[str]:
    after, before = t.at(i, 1), t.at(i, -1)
    if after in _PUSSY_AFTER or before in _PUSSY_BEFORE:
        return None
    if before in ("you", "u") and after is None:
        return None
    return None if _near(t, i, _set("cat cats kitten kitty purr purred purring meow"), span=6) else TIER_UNAMBIGUOUS


_TITS_BIRDS = _set("blue great coal marsh willow crested bearded tailed penduline varied sombre")
_BIRD_CUES = _set("bird birds sparrow sparrows finch finches warbler warblers robin robins wren wrens swallow "
                  "swallows chickadee chickadees titmice feeder feeders nest nesting nestbox species songbird "
                  "songbirds birdwatching birding garden")


def _tits(t: _Text, i: int) -> Optional[str]:
    before = t.at(i, -1)
    if before in _TITS_BIRDS or t.at(i, 1) == "up":
        return None
    if before in _POSSESSIVES | {"yer"} and t.at(i, -2) in ("calm", "on"):
        return None
    return None if _near(t, i, _BIRD_CUES) else TIER_UNAMBIGUOUS


# "X porn" for anything admired or dwelt on, and the vocabulary of filtering, law and moderation.
_PORN_BEFORE = _set(
    "food earth gear shelf property house travel word poverty torture inspiration revenge ruin map data chart "
    "design cabin car bike tool book type font sky space nature city room desk setup keyboard code science "
    "history architecture interior garden stationery organization organisation productivity weather cloud tech "
    "battlestation typography infrastructure competence trauma outrage misery disaster war tragedy wedding puppy "
    "cat dog coffee wine beer whisky whiskey watch knife sneaker luggage yarn fabric plant van estate grief anti "
    "non child detect detecting detects block blocking blocks filter filtering filters flag flagging flags "
    "classify classifying classifies moderate moderating banning ban banned bans regulate regulating regulates")
_PORN_AFTER = _set("filter filters filtering blocker blockers blocking detection detector detectors classifier "
                   "classifiers moderation policy policies ban bans law laws legislation industry")


def _porn(t: _Text, i: int) -> Optional[str]:
    if t.at(i, -1) in _PORN_BEFORE or t.at(i, 1) in _PORN_AFTER:
        return None
    return TIER_UNAMBIGUOUS


_FIGURATIVE = _set("intellectual mental verbal academic philosophical theoretical conceptual ego creative "
                   "narrative technical engineering design corporate emotional spiritual political legal "
                   "statistical mathematical")


def _masturbation(t: _Text, i: int) -> Optional[str]:
    return None if t.at(i, -1) in _FIGURATIVE else TIER_UNAMBIGUOUS


# Made of horn, or hardened like it: zoology and anatomy write "horny plates", "a horny beak", "horny-handed".
_HORN_CUES = _set(
    "toad toads lizard lizards goat handed beak bill scales scale plates plate shell hoof hoofs hooves nail nails "
    "claw claws keratin skin tissue tissues teeth tooth skeleton wings wing feathers mouth jaw jaws spine spines "
    "crest callus texture substance material layer covering covers sheath growth projection process segments "
    "sections ribs spikes horn horns cells coral corals beetle beetles insect insects bird birds reptile reptiles "
    "mammal mammals calcareous bony cartilaginous chitinous tip structure parts")


def _horny(t: _Text, i: int) -> Optional[str]:
    if (i + 1 < t.n and t.joined[i + 1]) or _near(t, i, _HORN_CUES, span=6):
        return None
    return TIER_UNAMBIGUOUS


def _boner(t: _Text, i: int) -> Optional[str]:
    lead = t.left(i, _set("a another"), limit=1)
    if t.word(lead) in _set("pull pulls pulled pulling"):
        return None
    return TIER_UNAMBIGUOUS


_ERECTION_AFTER = _set("of company crew crews contractor contractors engineer engineers drawing drawings sequence "
                       "plan plans work works site schedule crane cranes cost costs phase team mark marks bolt "
                       "bolts stress stresses load loads")
_ERECTION_BEFORE = _set("steel structural tower crane scaffold scaffolding site building bridge precast turbine "
                        "tank")


def _erection(t: _Text, i: int) -> Optional[str]:
    """Putting up a structure: "erection of the frame", "steel erection", "pre-erection"."""
    if t.at(i, 1) in _ERECTION_AFTER or t.at(i, -1) in _ERECTION_BEFORE or t.joined[i]:
        return None
    return TIER_UNAMBIGUOUS


def _orgy(t: _Text, i: int) -> Optional[str]:
    return None if t.at(i, 1) == "of" else TIER_UNAMBIGUOUS


# Tightly curled or kinked: hair, and anything that comes in a length.
_KINK_CUES = _set("hair curls curly coils coily twists twist texture textured boots afro straight locs braids "
                  "cord cords cable cables wire wires hose rope thread string tail tails fur wool fibers fibres")


def _kinky(t: _Text, i: int) -> Optional[str]:
    return None if _near(t, i, _KINK_CUES, span=3) else TIER_UNAMBIGUOUS


_GOLF = _set("golf tee teed round course hole holes foursome twosome caddie caddy fairway par birdie bogey")


def _threesome(t: _Text, i: int) -> Optional[str]:
    """Three players in golf; otherwise explicit."""
    for j in range(max(0, i - 8), min(t.n, i + 9)):
        if t.low[j] in _GOLF:
            return None
    return TIER_UNAMBIGUOUS


_PLACE_LEADS = _set("in to from near of at")


def _cumming(t: _Text, i: int) -> Optional[str]:
    """A surname and a town are written with a capital after a name, a title or a place word."""
    if t.capitalised(i) and (_name(t, i) or t.at(i, -1) in _PLACE_LEADS):
        return None
    return TIER_UNAMBIGUOUS


def _hard_on(t: _Text, i: int) -> Optional[str]:
    """"hard-on", hyphenated; the idiom "a hard-on for" something is not about sex."""
    if i + 1 < t.n and t.joined[i + 1] and t.at(i, 1) in ("on", "ons") and t.at(i, 2) != "for":
        return TIER_UNAMBIGUOUS
    return None


def _hand_job(t: _Text, i: int) -> Optional[str]:
    """"hand job", not the tail of "second-hand job"."""
    if t.at(i, 1) in ("job", "jobs") and t.at(i, -1) not in _set("second first left right"):
        return TIER_UNAMBIGUOUS
    return None


# --- framed words: flag only inside an explicit phrase -------------------------------------------------------

_SEX_VERBS = _set("have has had having haven't hasn't hadn't want wants wanted wanting need needs needed needing "
                  "crave craves craved craving enjoy enjoys enjoyed enjoying love loves loved initiate initiates "
                  "initiated initiating refuse refused deny denied withhold withheld miss missed")
_SEX_ADJECTIVES = _set(
    "great good amazing rough hot wild casual unprotected oral anal group phone drunk drunken morning shower "
    "incredible awesome kinky passionate sweaty steamy makeup breakup angry lazy quick cyber hate pity revenge "
    "goodbye birthday vacation hotel public outdoor loud best worst bad terrible mediocre boring meaningless "
    "vanilla raw sober hungover mindblowing blowing penetrative vaginal tantric hardcore more less enough no")
_SEX_BETWEEN = _SEX_ADJECTIVES | _set("some any lots of a lot the much actual real regular daily protected "
                                      "consensual first proper full only just really such so slow")
# "sex" as a category, a field or a subject of study, law or news: never a statement about anyone's sex life.
_SEX_ORDINARY_AFTER = _set(
    "ed education educator educators offender offenders trafficking trafficker traffickers worker workers work "
    "ratio ratios difference differences discrimination chromosome chromosomes hormone hormones linked "
    "determination selection selective appeal symbol symbols scandal scandals crime crimes abuse assault "
    "addiction addict addicts therapist therapy drive scene scenes and pistols industry trade tourism change "
    "reassignment organ organs cell cells steroid steroids specific based segregated category field column "
    "variable at assigned or of")
_SEX_COMPOUNDS = _set("toy toys tape tapes doll dolls dream dreams fantasy fantasies shop club clubs party parties "
                      "dungeon chat cam cams buddy buddies position positions video videos")
_SEX_VERDICTS = _set("great good amazing incredible awesome bad terrible ok okay fine rough hot painful quick "
                     "mediocre boring fun intense wild better worse awkward weird nice fantastic disappointing "
                     "passionate")
_SEX_TIMES = {"last": _set("night"), "this": _set("morning afternoon evening weekend"), "all": _set("night"),
              "every": _set("day night morning")}


def _sex(t: _Text, i: int) -> Optional[str]:
    after, before = t.at(i, 1), t.at(i, -1)
    if after in _SEX_COMPOUNDS:
        return TIER_PHRASE
    if after in _SEX_ORDINARY_AFTER or (i + 1 < t.n and t.joined[i + 1]) or t.joined[i]:
        return None
    if before in _SEX_ADJECTIVES:
        return TIER_PHRASE
    if t.word(t.left(i, _SEX_BETWEEN, limit=3)) in _SEX_VERBS:
        return TIER_PHRASE
    if after == "with" and before not in _set("male female biological") and _person_follows(t, i + 1):
        return TIER_PHRASE
    if after in _set("was is wasn't isn't felt feels"):
        j = i + 2
        while j < t.n and t.clause[j] == t.clause[i] and t.low[j] in _INTENSIFIERS and j < i + 4:
            j += 1
        if j < t.n and t.clause[j] == t.clause[i] and t.low[j] in _SEX_VERDICTS:
            return TIER_PHRASE
    if after in ("tonight", "tonite") or t.at(i, 2) in _SEX_TIMES.get(after or "", ()):
        return TIER_PHRASE
    if after == "life" and before in _POSSESSIVES:
        return TIER_PHRASE
    return None


_SEXUAL_AFTER = _set("fantasy fantasies pleasure kink kinks roleplay urges arousal gratification")
_SEXUALLY_AFTER = _set("aroused arousing frustrated satisfied excited stimulated stimulating")


def _sexual(t: _Text, i: int) -> Optional[str]:
    return TIER_PHRASE if t.at(i, 1) in _SEXUAL_AFTER else None


def _sexually(t: _Text, i: int) -> Optional[str]:
    return TIER_PHRASE if t.at(i, 1) in _SEXUALLY_AFTER else None


def _personal_state(t: _Text, i: int) -> bool:
    """Whether word ``i`` is said of a person: "I was so …", "she's …", "got me …"."""
    j = t.left(i, _INTENSIFIERS, limit=3)
    word = t.word(j)
    if word in _SUBJECT_CONTRACTIONS:
        return True
    if word in _BE:
        return t.word(t.left(j, _ADVERBS | _set("have has had been"), limit=2)) in _SUBJECTS | _SUBJECT_CONTRACTIONS
    if word in _OBJECTS:
        return t.word(t.left(j, limit=0)) in _CAUSE
    return False


def _aroused(t: _Text, i: int) -> Optional[str]:
    if not _personal_state(t, i):
        return None
    after = t.at(i, 1)
    if after is None or after in _CARRY_ON or after in _set("thinking watching looking at"):
        return TIER_PHRASE
    return TIER_PHRASE if after == "by" and t.at(i, 2) in _OBJECTS else None


_NOT_AFTER_ON = {"to"} | _DETERMINERS | _POSSESSIVES
_TURN_ON_NOUN_AFTER = _set("voltage time delay transient current threshold sequence characteristics loss losses "
                           "speed behavior behaviour point signal energy resistance pulse command procedure")
_TURN_ON_SIZES = _set("huge big major real total definite massive serious biggest ultimate instant")
_TURN_ME = _set("me him her you u")


def _turn(t: _Text, i: int) -> Optional[str]:
    word, after, second = t.low[i], t.at(i, 1), t.at(i, 2)
    if word == "turn" and after in ("on", "ons"):
        if t.joined[i + 1] or after == "ons":          # "turn-on", "turn ons": the noun
            return None if second in _TURN_ON_NOUN_AFTER else TIER_PHRASE
        sized = t.at(i, -1) in _TURN_ON_SIZES or (
            t.at(i, -1) == "a" and t.at(i, -2) in _set("such what quite definitely totally"))
        if sized and (second is None or second in _CARRY_ON or second == "for"):
            return TIER_PHRASE                          # "a huge turn on"
    if after in _TURN_ME and second == "on":            # "turns me on"
        return None if t.at(i, 3) in _NOT_AFTER_ON else TIER_PHRASE
    if word == "turned" and after == "on" and second not in _NOT_AFTER_ON and _personal_state(t, i):
        return TIER_PHRASE                              # "I was so turned on"
    return None


def _turnon(t: _Text, i: int) -> Optional[str]:
    return None if t.at(i, 1) in _TURN_ON_NOUN_AFTER else TIER_PHRASE


# "We hooked up" is said of people; "the engine is hooked up" is said of a thing, so the passive never counts.
_HOOKED_LEADS = _SUBJECTS | _set("have has had haven't hasn't hadn't i've we've they've you've")
_HOOKING_LEADS = _SUBJECTS | _SUBJECT_CONTRACTIONS | _set("were was are is am been be started kept keep stopped to")
_HOOK_LEADS = _SUBJECTS | _set("to wanna gonna gotta tryna should could would might will didn't don't doesn't "
                               "can let's lets did do")


def _with_person_or_carries_on(t: _Text, i: int) -> bool:
    """After word ``i`` (the "up" or "out" of the phrase): the sentence carries on, or "with" a person follows."""
    if t.at(i, 1) != "with":
        return _carries_on(t, i)
    if not _person_follows(t, i + 1):
        return False
    if t.at(i, 2) in _set("him her them you u me"):
        return _carries_on(t, i + 2)     # "with her tonight"; not "with him for lunch", "with her inheritance"
    return True


def _hook(t: _Text, i: int) -> Optional[str]:
    if t.at(i, 1) != "up":
        return None
    lead = t.left(i, _ADVERBS, limit=2)
    word = t.word(lead)
    if word == "up" and lead is not None and t.at(lead, -1) in _set("ended end ends ending"):
        word = "to"                                     # "ended up hooking up"
    leads = {"hooked": _HOOKED_LEADS, "hooking": _HOOKING_LEADS}.get(t.low[i], _HOOK_LEADS)
    if word not in leads:
        return None
    return TIER_PHRASE if _with_person_or_carries_on(t, i + 1) else None


_HOOKUP_KINDS = _set("random casual drunken drunk tinder grindr bumble hinge sloppy steamy meaningless anonymous "
                     "onetime vacation")
_HOOKUP_AFTER = _set("fee fees kit kits cable cables wire site sites charge charges culture app apps instructions "
                     "diagram")


def _hookup(t: _Text, i: int) -> Optional[str]:
    before = t.at(i, -1)
    if t.at(i, 1) in _HOOKUP_AFTER:
        return None
    if before in _HOOKUP_KINDS or (t.low[i] == "hookups" and before in _POSSESSIVES):
        return TIER_PHRASE
    return None


_MAKING_LEADS = _SUBJECT_CONTRACTIONS | _set("were was are is been be started start starts kept keep keeps stopped "
                                             "stop up still just them us him her me you people couple couples "
                                             "kids teens teenagers")
_MAKE_LEADS = _SUBJECTS | _set("wanna gonna tryna let's lets should would might will we'll then shall didn't "
                               "don't")
_MAKE_TO = _set("want wants wanted going started began proceeded trying tried love loved like liked need needed "
                "wanting")


def _make(t: _Text, i: int) -> Optional[str]:
    if t.at(i, 1) != "out":
        return None
    if t.at(i, 2) == "with":
        return TIER_PHRASE if _with_person_or_carries_on(t, i + 1) else None
    lead = t.left(i, _ADVERBS, limit=2)
    word, form = t.word(lead), t.low[i]
    if form == "making":
        ok = word in _MAKING_LEADS
    elif form == "make":
        ok = word in _MAKE_LEADS or (word == "to" and lead is not None and t.at(lead, -1) in _MAKE_TO)
    else:                                               # made, makes
        ok = word in _SUBJECTS
    return TIER_PHRASE if ok and _carries_on(t, i + 1) else None


def _laid(t: _Text, i: int) -> Optional[str]:
    if t.at(i, -1) not in _set("get gets got getting gotten"):
        return None
    after = t.at(i, 1)
    if after is None or after in _CARRY_ON or after in _set("more enough easily anymore soon"):
        return TIER_PHRASE
    return None


def _down(t: _Text, i: int) -> Optional[str]:
    """"went down on him"; "on her" only when no noun follows ("went down on her birthday")."""
    if t.at(i, -1) not in _set("go goes going went gone") or t.at(i, 1) != "on":
        return None
    who = t.at(i, 2)
    if who in _set("me him you u"):
        return TIER_PHRASE
    if who == "her" and _carries_on(t, i + 2):
        return TIER_PHRASE
    return None


def _eat(t: _Text, i: int) -> Optional[str]:
    if t.at(i, 1) in _set("me her him you u") and t.at(i, 2) == "out" and t.at(i, 3) != "of":
        return TIER_PHRASE
    return None


_JERK_LEADS = _set("to wanna gonna gotta i he she they we and can't don't didn't doesn't would could will just "
                   "usually often sometimes always never still")
_JERK_NOT_AFTER = _set("the of a an my his her their its course balance track")
_JERK_WHO = _set("me him you u myself himself yourself themselves")


def _jerk(t: _Text, i: int) -> Optional[str]:
    word, after = t.low[i], t.at(i, 1)
    if after in _JERK_WHO and t.at(i, 2) == "off":
        return TIER_PHRASE
    if after != "off":
        return None
    if word.endswith(("ing", "ed")):
        return None if t.at(i, 2) in _JERK_NOT_AFTER else TIER_PHRASE
    return TIER_PHRASE if t.at(i, -1) in _JERK_LEADS else None


def _suck(t: _Text, i: int) -> Optional[str]:
    return TIER_PHRASE if t.at(i, 1) in _set("me him you u") and t.at(i, 2) == "off" else None


_FUCK_NOT_AFTER = _set("up over off out with around it things everything this that shit")
_FUCK_MANNER = _set("hard harder good rough raw senseless silly deep slowly bareback") | _set(
    "again twice once last tonight yesterday all in on against like and but until till for so everywhere before "
    "after while when then lol haha though tho right outside upstairs downstairs there from")
_FUCK_LEADS = _set("to wanna gonna i'd id we'd would could will i'll he'd she'd lemme me tryna should")


def _fuck(t: _Text, i: int) -> Optional[str]:
    word, after, second = t.low[i], t.at(i, 1), t.at(i, 2)
    if word == "fuck" and after in ("buddy", "buddies"):
        return TIER_UNAMBIGUOUS
    inflected = word in ("fucks", "fucked", "fucking", "fuckin")
    if word == "fucked" and t.word(t.left(i, _ADVERBS, limit=2)) in ("we", "they"):
        if after is None or (after in _FUCK_MANNER and after not in _FUCK_NOT_AFTER):
            return TIER_PHRASE
    if inflected and after in ("her", "him") and (second is None or second in _FUCK_MANNER):
        return TIER_PHRASE
    if inflected and after in ("me", "you", "u") and second in _FUCK_MANNER - _set("and but or lol haha though tho"):
        return TIER_PHRASE
    if word == "fuck" and after in _set("you u me him her") and second not in _FUCK_NOT_AFTER:
        if t.at(i, -1) in _FUCK_LEADS:
            return TIER_PHRASE
    return None


_CUM_NOT_AFTER = _set("laude grano dividend rights div sum total avg gpa freq frequency dist returns return pct "
                      "percent hoc privilegio")
_CUM_NOT_BEFORE = _set("summa magna egregia insigni maxima")
_CUM_VERB_LEADS = _SUBJECTS | _set("to gonna wanna gotta me him her didn't did don't can't couldn't could would "
                                   "will i'll he'll she'll you'll")
_CUM_AFTER = _set("in inside on onto for all hard harder so again twice together first quickly fast yet already "
                  "now too like when while and from")
_CUM_NOUN_LEADS = _set("my his your ur their of swallow swallowed swallowing taste tasted tasting")


def _cum(t: _Text, i: int) -> Optional[str]:
    after, before = t.at(i, 1), t.at(i, -1)
    if t.joined[i] or (i + 1 < t.n and t.joined[i + 1]):     # "kitchen-cum-diner"
        return None
    if after in _CUM_NOT_AFTER or before in _CUM_NOT_BEFORE:
        return None
    if before in _CUM_NOUN_LEADS:
        return TIER_PHRASE
    if before in _CUM_VERB_LEADS and (after is None or after in _CUM_AFTER):
        return TIER_PHRASE
    return None


_ANAL_AFTER = _set("sex intercourse play beads plug plugs virgin porn fingering penetration toy toys orgasm lube")
_ANAL_LEADS = _set("do did doing does done try tried trying tries into love loves loved like likes liked want "
                   "wants wanted had have having has enjoy enjoys enjoyed")


def _anal(t: _Text, i: int) -> Optional[str]:
    after = t.at(i, 1)
    if after in _ANAL_AFTER:
        return TIER_PHRASE
    if t.at(i, -1) in _ANAL_LEADS and (after is None or after in _CARRY_ON or after == "with"):
        return TIER_PHRASE
    return None


_ORAL_LEADS = _set("gave give gives giving got get gets getting receive received receiving perform performed "
                   "performing")


def _oral(t: _Text, i: int) -> Optional[str]:
    lead = t.left(i, _OBJECTS, limit=1)
    after = t.at(i, 1)
    if t.word(lead) in _ORAL_LEADS and (after is None or after in _CARRY_ON):
        return TIER_PHRASE
    return None


_DICK_PICTURES = _set("pic pics picture pictures pix")
_DICK_SIZES = _set("big huge small hard tiny little fat thick long massive giant limp soft erect")
_DICK_NOT_AFTER = _set("head move moves boss friend of brother neighbor neighbour manager landlord energy van "
                       "tracy cheney clark")
_SUCKS = _set("suck sucks sucked sucking")


def _dick(t: _Text, i: int) -> Optional[str]:
    after, before = t.at(i, 1), t.at(i, -1)
    if after in _DICK_PICTURES:
        return TIER_PHRASE
    if t.word(t.left(i, _POSSESSIVES | _set("a the some on"), limit=2)) in _SUCKS:
        return TIER_PHRASE
    if t.low[i] == "dicks" or after in _DICK_NOT_AFTER or "'" in t.raw[i]:
        return None
    if before in _set("my his your ur") or before in _DICK_SIZES:
        return TIER_PHRASE
    if after == "in" and t.at(i, 2) in _POSSESSIVES:
        return TIER_PHRASE
    return None


def _cunt(t: _Text, i: int) -> Optional[str]:
    return TIER_PHRASE if t.at(i, -1) in _set("my her your ur wet tight") else None


_PICTURES = _set("pic pics picture pictures photo photos selfie selfies video videos vid vids")
_SEEING = _set("see saw seen seeing picture pictured picturing imagine imagined imagining watch watched watching")


def _naked(t: _Text, i: int) -> Optional[str]:
    after, before = t.at(i, 1), t.at(i, -1)
    if after in _PICTURES or before in _set("get gets got getting gotten"):
        return TIER_PHRASE
    if before in _set("him her you u me them") and t.at(i, -2) in _SEEING:
        return TIER_PHRASE
    if after == "together" or (after == "in" and t.at(i, 2) == "bed"):
        return TIER_PHRASE
    if after == "with" and t.at(i, 2) in _set("him her you u me"):
        return TIER_PHRASE
    return None


def _nude(t: _Text, i: int) -> Optional[str]:
    return TIER_PHRASE if t.at(i, 1) in _PICTURES else None


_NUDES_LEADS = _set("send sends sent sending share shared sharing leak leaked leaking trade traded trading swap "
                    "swapped swapping exchange exchanged post posted posting take took taking got get getting "
                    "want wants wanted request requested requesting her his my your ur their some any more "
                    "those these for")


def _nudes(t: _Text, i: int) -> Optional[str]:
    """"send nudes", "sent me nudes", "her nudes"; not a palette or a painter's subject."""
    if t.at(i, -1) in _NUDES_LEADS:
        return TIER_PHRASE
    return TIER_PHRASE if t.at(i, -1) in _OBJECTS and t.at(i, -2) in _NUDES_LEADS else None


_Handler = Callable[[_Text, int], Optional[str]]
_GUARDED: Dict[str, _Handler] = {
    **dict.fromkeys(("cock", "cocks"), _cock), "pussy": _pussy, "tits": _tits,
    **dict.fromkeys(("porn", "pornography", "pornographic"), _porn),
    **dict.fromkeys(("masturbate", "masturbates", "masturbated", "masturbating", "masturbation", "masturbatory"),
                    _masturbation),
    "horny": _horny, **dict.fromkeys(("boner", "boners"), _boner),
    **dict.fromkeys(("erection", "erections"), _erection), **dict.fromkeys(("orgy", "orgies"), _orgy),
    "kinky": _kinky, **dict.fromkeys(("threesome", "threesomes"), _threesome), "cumming": _cumming,
    "hard": _hard_on, "hand": _hand_job, **dict.fromkeys(_UNAMBIGUOUS_PAIRS, _pair),
}
_FRAMED: Dict[str, _Handler] = {
    "sex": _sex, "sexual": _sexual, "sexually": _sexually, "aroused": _aroused,
    **dict.fromkeys(("turn", "turns", "turned", "turning"), _turn), **dict.fromkeys(("turnon", "turnons"), _turnon),
    **dict.fromkeys(("hook", "hooks", "hooked", "hooking"), _hook), **dict.fromkeys(("hookup", "hookups"), _hookup),
    **dict.fromkeys(("make", "makes", "made", "making"), _make), "laid": _laid, "down": _down,
    **dict.fromkeys(("eat", "eats", "ate", "eating", "eaten"), _eat),
    **dict.fromkeys(("jerk", "jerks", "jerked", "jerking", "jack", "jacks", "jacked", "jacking"), _jerk),
    **dict.fromkeys(_SUCKS, _suck), **dict.fromkeys(("fuck", "fucks", "fucked", "fucking", "fuckin"), _fuck),
    **dict.fromkeys(("cum", "cums"), _cum), "anal": _anal, "oral": _oral,
    **dict.fromkeys(("dick", "dicks"), _dick), "cunt": _cunt, "naked": _naked,
    **dict.fromkeys(("nude", "topless"), _nude), "nudes": _nudes,
}
assert not set(_GUARDED) & set(_FRAMED) and not UNAMBIGUOUS & (set(_GUARDED) | set(_FRAMED))
_HANDLERS: Dict[str, _Handler] = {**_GUARDED, **_FRAMED}

# One cheap pass decides whether a text can flag at all: its runs of letters and digits, as a set, against every
# word that starts a check. The words too common to look up on their own count only with the word that must follow
# them. A text this does not pass is not tokenised. It is one C-level scan and set lookups, never a regex
# alternation over the text: on a long text that alternation held the interpreter for a third of a second.
_RUN = re.compile(r"[^\W_]+")
_COMMON_FOLLOW: Dict[str, Callable[[List[str], int], bool]] = {
    **dict.fromkeys(("turn", "turns", "turned", "turning"), lambda w, i: _nth(w, i, 1) in ("on", "ons") or (
        _nth(w, i, 1) in _TURN_ME and _nth(w, i, 2) in ("on", "ons"))),
    **dict.fromkeys(("make", "makes", "made", "making"), lambda w, i: _nth(w, i, 1) == "out"),
    "down": lambda w, i: _nth(w, i, 1) == "on",
    **dict.fromkeys(("eat", "eats", "ate", "eating", "eaten"),
                    lambda w, i: _nth(w, i, 1) in _set("me her him you u") and _nth(w, i, 2) == "out"),
    **dict.fromkeys(("jerk", "jerks", "jerked", "jerking", "jack", "jacks", "jacked", "jacking"),
                    lambda w, i: "off" in (_nth(w, i, 1), _nth(w, i, 2))),
    **dict.fromkeys(_SUCKS, lambda w, i: _nth(w, i, 1) in _set("me him you u") and _nth(w, i, 2) == "off"),
    "hard": lambda w, i: _nth(w, i, 1) in ("on", "ons"),
    "hand": lambda w, i: _nth(w, i, 1) in ("job", "jobs"),
    **{word: (lambda follows: lambda w, i: _nth(w, i, 1) in follows)(follows)
       for word, follows in _UNAMBIGUOUS_PAIRS.items()},
}
_COMMON_WORDS = frozenset(_COMMON_FOLLOW)
_STARTS = frozenset(UNAMBIGUOUS | set(_HANDLERS)) - _COMMON_WORDS
assert _COMMON_WORDS <= set(_HANDLERS)


def _nth(words: List[str], i: int, k: int) -> Optional[str]:
    return words[i + k] if i + k < len(words) else None


def _may_flag(folded: str) -> bool:
    """False only when no word of the text can start a check; never False for a text the rule would flag."""
    words = _RUN.findall(folded)
    present = set(words)
    if not present.isdisjoint(_STARTS):
        return True
    common = present & _COMMON_WORDS
    return bool(common) and any(word in common and _COMMON_FOLLOW[word](words, i) for i, word in enumerate(words))


def _read(text: str) -> Optional[str]:
    """The strongest tier any word of the text reaches, or None."""
    words = _Text(text)
    found = None
    for i, word in enumerate(words.low):
        if word in UNAMBIGUOUS:
            return TIER_UNAMBIGUOUS
        handler = _HANDLERS.get(word)
        if handler is None:
            continue
        tier = handler(words, i)
        if tier == TIER_UNAMBIGUOUS:
            return tier
        found = found or tier
    return found


def _readings(text: str) -> Tuple[str, ...]:
    """The text, and with letters stretched for emphasis collapsed to one and to two."""
    if not _STRETCHED.search(text):
        return (text,)
    return (text, _STRETCHED.sub(r"\1", text), _STRETCHED.sub(r"\1\1", text))


def evaluate(text, *, prefilter: bool = True) -> Verdict:
    """The rule's verdict on one text. Anything that is not text, and text with no letters, does not flag.

    ``prefilter=False`` reads every text word by word; it exists for the test that the ``_may_flag`` shortcut never
    changes a verdict.
    """
    if not isinstance(text, str) or not text.strip():
        return NOT_FLAGGED
    found = None
    for reading in _readings(normalise(text)):
        if prefilter and not _may_flag(reading.casefold()):
            continue
        tier = _read(reading)
        if tier == TIER_UNAMBIGUOUS:
            return Verdict(True, tier)
        found = found or tier
    return Verdict(True, found) if found else NOT_FLAGGED
