"""TX-series: transform-over-own-data — "do X to my data", where X names a FORM.

THE CLASS
---------
A request in this class has two halves: a SUBJECT that lives in the owner's data
("the work I have been doing lately") and a TRANSFORM that names the shape of the
answer ("a 3 stanza iambic pentameter poem"). Retrieval only ever needed the first
half. The failure is that the second half is what survived distillation.

Live 2026-09-05, home chat, `work_context:read`, owner-authenticated app client:

    Q  Take the work I have been doing lately, and put it into a 3 stanza iambic
       pentameter poem for me
    A  I couldn't find any recent work activity in your synced data … your work
       context might not be synced or available right now.

    narrowing.ledger = [
      {stage: retrieval, action: rewrote,  reason: embedded_subject_not_instruction},
      {stage: rare_gate, action: emptied,  reason: rare_token_unevidenced, dropped: 63}
    ]

Sixty-three evidence items were retrieved and then discarded, because `iambic` (df 0)
and `pentameter` (df 0) are tokens no stored row contains. The same scope, node and
window answered "what have I been working on lately" with 25 items in the same session.

WHY THE SUBJECT LOSES AND THE INSTRUCTION WINS
----------------------------------------------
`_residual_content_tokens` strips recency framing (`lately`) and surface vocabulary
(`work` names the goals surface). Both strips are correct. But nothing stripped the
transform, so after distillation the needle set was the poem's metre and NOTHING ELSE:
the ask's own subject had been removed as framing while the instruction was promoted to
the only discriminative content in the query. The gate then did exactly what it is for —
"you asked about something your data does not mention" — about a word describing the
output format.

WHAT MAKES IT DANGEROUS
-----------------------
It is CORPUS-DEPENDENT and SILENT. Whether "sonnet", "kanban" or "swot" vetoes depends
on whether that owner's corpus happens to contain the word, so the same request answers
on one node and returns "your data might not be synced" on another — and a fresh node,
with the smallest corpus, fails the most of them. Nothing warns: the lane returns a
well-formed empty result and the model relays a sync problem that does not exist.

THE CONTRACT THIS CATALOG PINS
------------------------------
`expect="answerable"`  the subject is ordinary owner data, so the lane MUST NOT be
                       gate-vetoed. Grading the poem is not this lane's job — reaching
                       the data is.
`expect="abstain"`     the SUBJECT is genuinely absent, so the gate MUST still fire.
                       These exist because the fix loosens an abstention, and a
                       loosening that also silences absence honesty is a worse bug than
                       the one it fixes. Note NEG-2/NEG-4 carry a transform too: the
                       form word must stop vetoing while the fabricated subject keeps
                       vetoing, in the SAME sentence.

Baseline measured on the owner's live node 2026-09-05 (engine-direct,
`work_context:read`, summary): 6/35 answerable cases returned zero with
`empty_cause=gate_vetoed`; 4/4 negatives abstained. After the fix: 0/35 and 4/4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal

TRANSFORM_CATALOG_VERSION = "tx-catalog-1"


@dataclass(frozen=True)
class TransformCase:
    case_id: str
    subclass: str
    query: str
    #: "answerable" — must reach data; "abstain" — the subject is absent, gate must fire.
    expect: Literal["answerable", "abstain"]
    #: Words in the query that name the OUTPUT, not any row. Empty for controls.
    shape_words: tuple = ()
    #: The fabricated subject a negative case must keep vetoing on.
    absent_subject: str = ""


def _tx(cid, sub, q, shape=()) -> TransformCase:
    return TransformCase(cid, sub, q, "answerable", tuple(shape))


def _neg(cid, q, absent, shape=()) -> TransformCase:
    return TransformCase(cid, "negative", q, "abstain", tuple(shape), absent)


TRANSFORM_CASES: List[TransformCase] = [
    # --- TX-A: verse and metre. TX-A1 is the reported failure, verbatim. -------------
    _tx("TX-A1", "verse",
        "Take the work I have been doing lately, and put it into a 3 stanza iambic "
        "pentameter poem for me", ("stanza", "iambic", "pentameter", "poem")),
    _tx("TX-A2", "verse", "Write a haiku about what I worked on this week", ("haiku",)),
    _tx("TX-A3", "verse", "Turn my recent journal entries into a sonnet", ("sonnet",)),
    _tx("TX-A4", "verse", "Make a limerick out of my week", ("limerick",)),
    _tx("TX-A5", "verse",
        "Write song lyrics in the style of a sea shanty about my projects",
        ("lyrics", "shanty")),
    _tx("TX-A6", "verse",
        "Give me a four line rhyming couplet summary of my meetings",
        ("rhyming", "couplet")),

    # --- TX-B: document, medium and artifact shape -----------------------------------
    _tx("TX-B1", "doc", "Turn what I've been working on into a bulleted executive summary",
        ("bulleted",)),
    _tx("TX-B2", "doc", "Draft a LinkedIn post about my recent work"),
    _tx("TX-B3", "doc", "Make a tweet thread out of my week", ("tweet",)),
    _tx("TX-B4", "doc", "Write a changelog entry for the work I did this week", ("changelog",)),
    _tx("TX-B5", "doc",
        "Put my recent work into a markdown table with columns for project and status",
        ("markdown", "table", "columns")),
    _tx("TX-B6", "doc", "Create slide bullets for a standup deck from my week",
        ("slide", "bullets", "deck")),
    _tx("TX-B7", "doc", "Write a one-pager memo about what I've been focused on", ("memo",)),
    _tx("TX-B8", "doc", "Give me a resume bullet for the work I did this month", ("bullet",)),

    # --- TX-C: persona, voice and register -------------------------------------------
    _tx("TX-C1", "voice", "Describe my week in the voice of a pirate", ("pirate",)),
    _tx("TX-C2", "voice", "Explain what I've been working on like I'm five"),
    _tx("TX-C3", "voice", "Narrate my recent work as a noir detective monologue",
        ("noir", "monologue")),
    _tx("TX-C4", "voice", "Summarize my week sarcastically"),
    _tx("TX-C5", "voice", "Write my recent work up as if Shakespeare wrote it",
        ("shakespeare",)),

    # --- TX-D: analytic frameworks asked for as an output format ---------------------
    _tx("TX-D1", "framework", "Do a SWOT analysis of what I've been working on", ("swot",)),
    _tx("TX-D2", "framework", "Give me a pros and cons list of my current projects"),
    _tx("TX-D3", "framework", "Rank my recent work by impact and score each out of 10"),
    _tx("TX-D4", "framework",
        "Turn my week into a kanban board with todo, doing and done columns",
        ("kanban", "columns")),
    _tx("TX-D5", "framework", "Build a timeline infographic outline of my recent work",
        ("infographic", "outline")),

    # --- TX-E: length and structure constraints --------------------------------------
    _tx("TX-E1", "length", "Summarize my week in exactly three sentences", ("sentences",)),
    _tx("TX-E2", "length", "Give me five bullets on what I've been doing", ("bullets",)),
    _tx("TX-E3", "length", "Write 200 words about my recent work"),
    _tx("TX-E4", "length", "Give me a two paragraph recap of my week", ("paragraph",)),

    # --- TX-F: occasional and narrative artifacts ------------------------------------
    _tx("TX-F1", "artifact", "Write a toast for my team based on what we shipped", ("toast",)),
    _tx("TX-F2", "artifact",
        "Draft a thank you note to the people I talked with most this week"),
    _tx("TX-F3", "artifact", "Make a mock newspaper headline about my week", ("headline",)),
    _tx("TX-F4", "artifact", "Write a fortune cookie message based on my recent work"),

    # --- Controls: the plain ask. If these ever go empty the fix broke retrieval, ----
    # --- not just the gate. ----------------------------------------------------------
    _tx("CTL-1", "control", "What have I been working on lately"),
    _tx("CTL-2", "control", "Summarize my week"),
    _tx("CTL-3", "control", "What did I work on this week"),

    # --- Negatives: absence honesty must survive the loosening -----------------------
    _neg("NEG-1", "What did I say about zorblatt tourism", "zorblatt"),
    _neg("NEG-2", "Write a poem about my years as a competitive falconer", "falconer",
         ("poem",)),
    _neg("NEG-3", "Summarize my notes on quantum chromodynamics in three bullets",
         "chromodynamics", ("bullets",)),
    _neg("NEG-4", "Make a haiku about my trip to Ulaanbaatar", "ulaanbaatar", ("haiku",)),
]

ANSWERABLE_CASES = [c for c in TRANSFORM_CASES if c.expect == "answerable"]
ABSTAIN_CASES = [c for c in TRANSFORM_CASES if c.expect == "abstain"]
