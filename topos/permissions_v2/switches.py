"""Every sharing switch of the node, read in one place (any-to-any N1, decision D5).

Until 1.5.0 the 28 ``TOPOS_PERMISSIONS_V2_*`` switches were read in 19 files by three different parsers, and
sharing worked only on a node where someone had typed the right ones into its env file by hand. This module owns
them, plus the one related setting only sharing code reads (``TOPOS_OWNER_CAPTURE_APP_IDS``). ``SWITCHES`` below
is the single source of truth: each switch's name, what it does, and its default on a node that is not bound for
sharing and on one that is. No other file reads one of these names from the environment or from settings
(``tests/permissions_v2/test_switches_guard.py`` walks the syntax tree of every file under ``topos/``).

**Bound.** A node is bound for sharing when its private sharing config exists and would load: the file is where
the node looks for it (``TOPOS_PERMISSIONS_V2_CONFIG_PATH`` when that is set, otherwise
``permissions-v2/config.json`` beside the database the node serves), it is a private regular file, it parses as
the node config, it names a beta environment and at least one control-plane key, and it binds the database this
node serves. Those are the checks ``runtime.load_runtime`` makes before it opens or creates anything; the ones
after (the key file, the process lock, the ledger, the protection clock) still refuse at load, as before.
``TOPOS_PERMISSIONS_V2_ENABLED`` set off overrides all of it: the node is then not bound. ``is_bound()`` is the one
place this is decided, and it logs nothing above debug. It costs one ``stat`` per call (where to look is re-derived
at most every ``LOCATION_SECONDS``), a config is parsed once per version of the file, and a config that appears
while the node runs is seen by the next call: no restart. ``forget_bound()`` drops what is remembered, for a
caller that has just written the config.

**Defaults.** A node that is not bound reads every switch exactly as 1.4.4 did: nothing new starts and nothing
new is served (the answer-quality switches of 1.5.7 differ from 1.5.6 on both kinds of node, and are inert where
answers are off). On a bound node with an empty environment these are on: the master switch, the knowledge search
door and its batch form, the refresh loop (index restore and assessment catch-up), the owner's identity
confirmations and the iMessage proof lane, and the kinds messages, AI chats, journal entries, browsing interests,
goals and relationships. Facts, entailment and everything that is experiment-, shadow- or lab-only stay off.

**Overrides.** Every name works as it always did, both ways: a value the environment sets wins over either
default, so each feature keeps its kill switch. One parser reads them all:

- on/off: ``true``, ``1``, ``yes``, ``on`` are on; ``false``, ``0``, ``no``, ``off`` are off; case and surrounding
  spaces are ignored. Unset or blank is no override. Anything else is off, and is logged once by name, never by
  value: a value the node cannot read never shares more.
- numbers: ASCII digits, raised to the switch's floor. Unset, blank or anything else is no override (logged once).
- choices: one of the switch's values, case and spaces ignored. Unset or blank is none; anything else is none
  (logged once).
- paths: the value exactly as written. Unset or blank is no override.
- lists: the comma-separated entries, each trimmed, empty ones dropped. A blank value is an empty list (it names
  nothing); only an unset one is no override.

Callers keep their own rules about how switches combine (the batch door needs search, the catch-up needs the
restore, derived facts need the journal kind, the model judge needs entailment grounding): this module decides
what each one switch says, never what a feature does with it.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

PREFIX = "TOPOS_PERMISSIONS_V2_"
#: Where a node keeps its sharing state, beside the database it serves (``runtime.load_runtime``).
DURABLE_DIRECTORY = "permissions-v2"
#: How long the derived place to look for the config is reused before it is derived again (a profile switch).
LOCATION_SECONDS = 2.0

Kind = Literal["bool", "int", "choice", "path", "list"]
_ON = frozenset({"1", "true", "yes", "on"})
_OFF = frozenset({"0", "false", "no", "off"})
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Switch:
    """One row of the table. ``unbound`` is 1.4.4's default; ``bound`` is the default on a bound node."""
    name: str
    kind: Kind
    unbound: object
    bound: object
    purpose: str
    floor: int = 0                 # numbers: the smallest value a setting is read as
    choices: tuple = ()            # choices: the values a node may name


def _switch(suffix: str, kind: Kind, unbound, bound, purpose: str, **extra) -> Switch:
    return Switch(PREFIX + suffix, kind, unbound, bound, purpose, **extra)


# --- the table ---------------------------------------------------------------------------------------------------
# name (prefix TOPOS_PERMISSIONS_V2_)   kind      unbound  bound    what it does

ENABLED = _switch(
    "ENABLED", "bool", False, True,
    "Master switch. Off, nothing of sharing loads whatever else is set: the kill switch for all of it.")
CONFIG_PATH = _switch(
    "CONFIG_PATH", "path", f"{DURABLE_DIRECTORY}/config.json", f"{DURABLE_DIRECTORY}/config.json",
    "Where the private sharing config is. Unset: beside the database the node serves. A node is bound when a "
    "config that loads is there.")
EVIDENCE_REVIEWS = _switch(
    "EVIDENCE_REVIEWS_ENABLED", "bool", True, True,
    "The private review store: the owner's opt-outs and the machine assessments. Every share's index needs it.")
PROJECTION_REVIEWS = _switch(
    "PROJECTION_REVIEWS_ENABLED", "bool", False, False,
    "Owner reviews of what the fact door would release (P2b; lab).")
IDENTITY_ATTESTATIONS = _switch(
    "IDENTITY_ATTESTATIONS_ENABLED", "bool", False, True,
    "The owner's \"this is me\" confirmations. Relationships release only about a confirmed self.")
INGEST_SNAPSHOTS = _switch(
    "INGEST_SNAPSHOTS_ENABLED", "bool", False, True,
    "The signed snapshot lane: iMessage proof (recover, refresh, the owner's standing statement) and the owner's "
    "signed snapshot commands.")
INGEST_SNAPSHOT_ROOT = _switch(
    "INGEST_SNAPSHOT_ROOT", "path", None, f"{DURABLE_DIRECTORY}/ingest-snapshots",
    "Where the snapshot lane keeps its snapshots, beside the database; the runtime refuses any other folder.")
MESSAGE_SEARCH = _switch(
    "MESSAGE_SEARCH_ENABLED", "bool", False, True,
    "The knowledge search door, each share's index, and the index rebuilds after the owner's changes.")
MESSAGE_SEARCH_BATCH = _switch(
    "MESSAGE_SEARCH_BATCH_ENABLED", "bool", False, True,
    "The batch search door and its heartbeat advert. Needs search on.")
ANSWERS = _switch(
    "ANSWERS_ENABLED", "bool", False, True,
    "Local answer submit and fetch doors. Needs knowledge search and the pinned checking model.")
INDEX_RESTORE = _switch(
    "INDEX_RESTORE_ENABLED", "bool", False, True,
    "Refresh loop: rebuild a share's index that a change dropped, so the share keeps serving unattended.")
ASSESSMENT_CATCHUP = _switch(
    "ASSESSMENT_CATCHUP_ENABLED", "bool", False, True,
    "Refresh loop: keep each share's window assessed by the local model as it rolls. Needs the restore.")
INDEX_RESTORE_MIN_INTERVAL = _switch(
    "INDEX_RESTORE_MIN_INTERVAL_SECONDS", "int", 300, 300,
    "Fewest seconds between two restore passes.", floor=60)
ASSESSMENT_CATCHUP_MAX_PER_PASS = _switch(
    "ASSESSMENT_CATCHUP_MAX_PER_PASS", "int", 500, 500,
    "Most local-model calls in one assessment pass.", floor=1)
AUTO_RESYNC = _switch(
    "AUTO_RESYNC", "bool", True, True,
    "Protection doorbell: tell the control plane the protection state moved, so it re-sends the unchanged shares.")
JOURNAL_SOURCES = _switch(
    "JOURNAL_SOURCES", "bool", False, True,
    "The journal entries kind.")
JOURNAL_GOAL_FIELD = _switch(
    "JOURNAL_GOAL_FIELD", "bool", False, True,
    "Goals from a journal entry's own goal field. Needs the journal kind; the entity graph reads it too.")
INTEREST_SOURCES = _switch(
    "INTEREST_SOURCES", "bool", False, True,
    "The browsing interests kind.")
INTEREST_RELABEL = _switch(
    "INTEREST_RELABEL", "bool", True, True,
    "A second try at an interest label that names a site, a page title or a person. Inert without interests.")
DERIVED_FACTS = _switch(
    "DERIVED_FACTS", "bool", False, False,
    "Facts inferred from journal entries (IF-6). Needs the journal kind.")
SELECTIVE_FACTS = _switch(
    "SELECTIVE_FACTS_ENABLED", "bool", False, False,
    "Ordinary owner-stated facts with explicit signed ontology consent. Off until release gates pass.")
PERMITTED_DERIVATION = _switch(
    "PERMITTED_DERIVATION", "bool", False, False,
    "The owner's local-model pass that derives facts and goals from what shares permit (OD-46).")
ENTAILMENT_GROUNDING = _switch(
    "ENTAILMENT_GROUNDING", "bool", False, False,
    "Facts and goals grounded by meaning rather than by wording (OD-38).")
ENTAILMENT_MODEL_JUDGE = _switch(
    "ENTAILMENT_MODEL_JUDGE", "bool", False, False,
    "The local model judge of entailment grounding. Needs entailment grounding.")
ENTAILMENT_SENTENCE_REPORTING = _switch(
    "ENTAILMENT_SENTENCE_REPORTING", "bool", False, False,
    "Reported speech vetoes only in the sentence that states the value (OD-45).")
SEARCH_TIMINGS = _switch(
    "SEARCH_TIMINGS", "bool", False, False,
    "Owner-local timing lines for each search; no content.")
DECIDE_OUTSIDE_GATE = _switch(
    "DECIDE_OUTSIDE_GATE", "bool", False, False,
    "NR166 R1. A release is decided on a read snapshot with the write gate NOT held, then confirmed under it: every "
    "store the decision read must be unchanged since before the snapshot, or the decision runs once more and then "
    "refuses. Off (the default): decided under the gate, as always. Off until an independent security review.")
REVIEW_PASS_BOUNDARY = _switch(
    "REVIEW_PASS_BOUNDARY", "bool", False, False,
    "NR186. An automatic-review pass (the owner's, or the refresh loop's catch-up) keeps one Off-limits boundary for "
    "its rows instead of building one per row. At each row the kept one is used only when it is what a fresh one "
    "would be (no commit since it was proven, or every read it was built from reads the same); otherwise it is built "
    "again. It ends with the pass. Read at every row: off from that row on, every read builds its own. Off (the "
    "default): one boundary per row, as always. Off until an independent security review.")
REVIEW_PASS_BOUNDARY_MAX_AGE = _switch(
    "REVIEW_PASS_BOUNDARY_MAX_AGE_SECONDS", "int", 600, 600,
    "NR186. The longest a review pass keeps its Off-limits boundary, in seconds, read at every row and taken as 60 to "
    "3600: an older one is built again at the next read, and one the pass has gone this long without a row for is "
    "dropped. Inert while REVIEW_PASS_BOUNDARY is off.", floor=60)
SWEEP_INTEREST_VERDICT = _switch(
    "SWEEP_INTEREST_VERDICT", "bool", False, False,
    "NR187. The daemon index sweep reuses a share index's last True browsing-interest verdict while the index file, "
    "its record-id key, the canonical database, the Off-limits closure and the interest switches are all unchanged "
    "(it keeps a boolean and change tokens, nothing derived from a name). Off (the default): every sweep rebuilds "
    "each index's interest members, as always.")
SWEEP_INTEREST_VERDICT_FULL_EVERY = _switch(
    "SWEEP_INTEREST_VERDICT_FULL_EVERY", "int", 30, 30,
    "NR187 (WS0 review, Q3). With the sweep's interest verdict kept, every this-many-th sweep of an index decides its "
    "interests in full all the same (30, about 5 minutes at the daemon's pace). 1 is every sweep; the reader holds it "
    "to at most 360.", floor=1)
SEARCH_ATTRIBUTION = _switch(
    "SEARCH_ATTRIBUTION", "bool", False, False,
    "Owner-local attribution lines beside the timing lines (NR166 R0): write-gate wait and hold by code location "
    "and caller, and each \"nothing changed\" fast path's hits and misses; no content. Needs search timings.")
# Lane NR183 (15 Oct 2026, plan item O1, the owner's ruling D3: the owner's own requests go ahead of recipients' when
# the node is busy). Off bound and unbound alike until WS0 flips it after the owner confirms; reading it costs no
# `is_bound()` stat. Read once, when the control-plane client is made (`owner_lane.slots_for`): a change needs a restart.
OWNER_LANE = _switch(
    "OWNER_LANE", "bool", False, False,
    "NR183 O1. Of the node's inbound handler slots for control-plane requests (16), OWNER_LANE_SLOTS are kept for "
    "the owner's interactive requests (a type in OWNER_LANE_TYPES the control plane signed as the owner's own app, "
    "never a recipient door's), which take those first, borrow at most OWNER_LANE_SHARED_MAX of the others, and go "
    "first when a slot comes free; everything else, recipients' share requests, the owner's background work and "
    "every unstamped frame included, shares the rest in arrival order. Each class's waiting places count its own "
    "frames only, so the owner's activity never makes a recipient's request busy (DR-04). Off (the default): one "
    "pool in arrival order, as in 1.5.9.")
OWNER_LANE_SLOTS = _switch(
    "OWNER_LANE_SLOTS", "int", 4, 4,
    "NR183 O1. How many inbound slots the owner lane keeps, at least 1 and never all; the shared class's waiting "
    "places are 128 less this. Read only while OWNER_LANE is on.", floor=1)
OWNER_LANE_SHARED_MAX = _switch(
    "OWNER_LANE_SHARED_MAX", "int", 2, 2,
    "NR183 O1. How many shared slots the owner's interactive requests may hold at once, beyond their kept ones (and "
    "never all of them): recipients always run on the rest. 0 keeps the owner on its kept slots only, so recipients' "
    "slots never depend on the owner's activity. Read only while OWNER_LANE is on.")
OWNER_LANE_TYPES = _switch(
    "OWNER_LANE_TYPES", "list",
    ("query", "aggregate", "tools_retrieve", "get_sources", "get_analytics", "get_messages", "get_oplog",
     "get_table_rows", "get_table_schema", "get_table_count", "list_database_tables", "llm_generation",
     "upsert_home_chat_session", "delete_home_chat_session", "home_chat_memory"),
    ("query", "aggregate", "tools_retrieve", "get_sources", "get_analytics", "get_messages", "get_oplog",
     "get_table_rows", "get_table_schema", "get_table_count", "list_database_tables", "llm_generation",
     "upsert_home_chat_session", "delete_home_chat_session", "home_chat_memory"),
    "NR183 O1. The owner's interactive request types the owner lane takes (someone at the screen waiting): the home "
    "chat's searches and reads through the agent gateway and the explorer's table reads, the home chat's generation "
    "(unstamped today, so inert until the control plane stamps it) and its own store. Any other owner type (ingest, "
    "sync, upload, reprocess, settings, sharing commands) shares the slots in arrival order; a recipient door's type "
    "is never taken whatever this says. A blank value names nothing. Read only while OWNER_LANE is on.")
OWNER_CAPTURE_APP_IDS = Switch(
    "TOPOS_OWNER_CAPTURE_APP_IDS", "list", ("chatgpt-shadow-extension",), ("chatgpt-shadow-extension",),
    "Capture apps whose stamped AI-chat rows count as the owner's (OD-39); mirrors the control plane's list.")
# Lane AQ158 (answer quality candidates, 12 Oct 2026) and DF160 (13 Oct 2026, A2A-4 amendment 11): the candidate is
# the default from 1.5.7 on. None of these changes which items a share releases, only which sentences about released
# items are kept and how many released items the model sees. Each keeps its old name, so the behaviour of 1.5.6 is
# one environment line away: ANSWER_SHORT_WORD_NAMES=true, ANSWER_SUBJECT_WORDS=true, ANSWER_QUESTION_ANCHOR=true,
# ANSWER_SUPPORT_JUDGE=false. Bound and unbound defaults are equal on purpose: reading one costs no `is_bound()` stat.
ANSWER_SUBJECT_WORDS = _switch(
    "ANSWER_SUBJECT_WORDS", "bool", False, False,
    "V1. The subject check requires the question's topic words in the cited items (A2A-4 amendment 3; withdrawn by "
    "amendment 11.2). Off (the default): the name rule, the namesake guard, citation, copy, echo, Off-limits and "
    "scrub still run, and the closed non-answer filter and the question-item rule run beside the judge (11.4a).")
ANSWER_SUPPORT_JUDGE = _switch(
    "ANSWER_SUPPORT_JUDGE", "bool", True, True,
    "V2 (amendment 11.4). After the deterministic checks, the pinned local model is asked whether each kept sentence "
    "is supported by the items it cites and answers the question; anything but a closed yes drops the sentence. One "
    "bounded call per sentence. Off: the receipt of 1.5.6, no judge call.")
ANSWER_SUPPORT_JUDGE_TEMPLATE = _switch(
    "ANSWER_SUPPORT_JUDGE_TEMPLATE", "choice", "v4", "v4",
    "V2's judge template: v1 (strict: no addition of any kind), v2 (WS0, 13 Oct: the item's own date and kind lines "
    "count as stated; paraphrase, summary and tense change are allowed; only an added fact, person, time, cause, or "
    "a question turned into a fact is a no), v3 (amendment 11.4: v2's rules with the intention-as-done rule first, "
    "and the judge also sees the question and must say the sentence answers it), or v4 (the default, amendment 12: "
    "v3 with today's date and the question's dates, and a sentence that presents an item older than the question's "
    "dates as current, or a past item as upcoming, is not supported).", choices=("v1", "v2", "v3", "v4"))
ANSWER_ITEMS = _switch(
    "ANSWER_ITEMS", "int", 30, 30,
    "V3. Released items the answer model sees, 1 to 30. The default is 30 since 1.5.10 (owner's decision of 16 Oct "
    "2026), answer protocol 4's cap; 1.5.9's was 8. On the grantee density fixtures (synthetic owner, blind review, "
    "16 Oct) the 1.5.10 writer at 30 items gave 4.85 supported sentences per answer against 4.00 at 16 and 2.05 "
    "with 1.5.9's settings, at a false-sentence rate in the same band (11 to 15%). Set 8 for 1.5.9's number. The "
    "grant's own max_k "
    "(the owner's items per answer) still bounds retrieval, and so does the asker's protocol: an asker whose control "
    "plane accepts 4 is served at most 30 items and 30 cited records, one that accepts 3 at most 16 and 16, and one "
    "that says nothing at most 8 and 8, whatever this says.",
    floor=1)
ANSWER_QUESTION_ANCHOR = _switch(
    "ANSWER_QUESTION_ANCHOR", "bool", False, False,
    "V1b. Before the model, abstain when every distinctive word of the question (eight letters or more) is absent "
    "from the released items (A2A-4 amendment 2; withdrawn by amendment 11.3). Off (the default): the model runs; "
    "the echo check still drops any sentence that says such a word back, and every other check runs.")
ANSWER_SHORT_WORD_NAMES = _switch(
    "ANSWER_SHORT_WORD_NAMES", "bool", False, False,
    "V0. A lower-case question word that equals a known person's word binds as that person's name even when it is "
    "a closed function word (\"to\", \"up\", \"been\"; amendment 11.1). Off (the default): such a word binds only "
    "when written with a capital; a person's word that is no function word (\"ivo\") still binds in lower case.")
# Lane TR162 (13 Oct 2026, A2A-4 amendment 12): answers respect the question's time. Seen live: "this week" and
# "lately" were answered from items one to two months old. None of these releases an item the share would not
# release: B narrows retrieval inside the grant's own window, C adds lines the node writes, D only drops sentences.
# All four are inert under a grant whose `release_event_time` is "none" (the grant withholds times). The behaviour of
# 1.5.9 is one environment line away: ANSWER_TIME_SCOPE=false (with ANSWER_SUPPORT_JUDGE_TEMPLATE=v3 for the judge).
ANSWER_TIME_SCOPE = _switch(
    "ANSWER_TIME_SCOPE", "bool", True, True,
    "TR162 A. The question's time words (a closed list: today, this week, last month, the past N days, lately, "
    "coming up and so on) are read as a window relative to the job's clock, in whole UTC days. Off: no scope, and "
    "the retrieval, the guard and the prompt's scope line have nothing to read.")
ANSWER_TIME_RETRIEVAL = _switch(
    "ANSWER_TIME_RETRIEVAL", "bool", True, True,
    "TR162 B. Retrieval ranks and walks inside the question's scope intersected with the grant's window; fewer than "
    "two items there and it widens once to three times the span, still inside the grant's window. \"Lately\" and "
    "its like climb a ladder instead (the last 3, 7, 14, then 30 days; two items fill a rung, the last needs one). "
    "Off: the whole grant window, as in 1.5.9.")
ANSWER_TIME_FILL = _switch(
    "ANSWER_TIME_FILL", "bool", True, True,
    "Lane 172. Inside a question's time window, when the words match fewer items than retrieval asks for, the "
    "window's other items follow, newest first, through the same checks (\"what did <owner> do yesterday\" names a "
    "day, not a subject). Off: only items the words or the meaning match, as in TR162.")
ANSWER_TIME_PROMPT = _switch(
    "ANSWER_TIME_PROMPT", "bool", True, True,
    "TR162 C. The answer prompt says today's date and the question's dates at the grant's precision (a date, never a "
    "time of day; nothing when the grant withholds times), and that a past item is never upcoming.")
ANSWER_TIME_GUARD = _switch(
    "ANSWER_TIME_GUARD", "bool", True, True,
    "TR162 D. A sentence citing only items outside the question's scope is dropped (`dropped_out_of_scope`); when "
    "retrieval had to widen, it stands only when it says it is about an earlier time (a month name, a date, "
    "\"earlier\", \"last month\": a closed list).")
# Lane CT176 (14 Oct 2026): why the 9B writer's sentences were dropped for citation. The switch releases no item
# and keeps no sentence the rule would not keep for the citation the model wrote on that sentence.
ANSWER_CITATION_FORMS = _switch(
    "ANSWER_CITATION_FORMS", "bool", True, True,
    "CT176. Before the citation rule, a citation the model wrote in another bracket form is read in the node's own "
    "form, where it stands: a range \"[1-3]\" is \"[1, 2, 3]\" when every number of it is an item, a padded \"[ 1 ]\" "
    "is \"[1]\", and full-width brackets (\"【1】\", \"［1］\") are \"[1]\". Never moves a citation to another "
    "sentence, never fills in a sentence without one; a range past the items is read as \"[0]\" and its sentence is dropped. Off: "
    "only \"[1]\", \"[1, 2]\" and \"[1; 2]\" are read, as in 1.5.9.")
# Lane GD-B (15 Oct 2026, grantee density): the name rule's closed non-person classes.
ANSWER_NAME_CLASSES = _switch(
    "ANSWER_NAME_CLASSES", "bool", True, True,
    "GD-B. A capitalised question word of a closed non-person class does not bind as a person's name: weekday and "
    "month names (April, May, June and August only after a time word or before a day number), a code in capitals "
    "with a digit or of two letters (\"Q4\", \"AI\"), and a closed function word right after a comma. A known "
    "person's word still binds, and an unknown capitalised name binds as before (\"Ivo\", \"TOPOS\"). Off: every "
    "capitalised word binds as in 1.5.9.")
# Lane GD-A (15 Oct 2026, grantee density): the dense writer template. On by default since 1.5.10 (owner's decision of
# 16 Oct 2026, after the grantee density runs), with GD-A′.
ANSWER_DENSE = _switch(
    "ANSWER_DENSE", "bool", True, True,
    "GD-A. The writer's template v9: seven to twelve cited sentences in two or three paragraphs, citing at most "
    "sixteen different items, with a 2,048-token output budget; the writer's paragraph breaks are carried through "
    "the checks and delivered as a blank line. Every check and receipt counter is unchanged. Off: template v8 (up to "
    "eight short sentences, one per line, 1,024 tokens), as in 1.5.9.")
# Lane GD-R (15 Oct 2026, grantee density round 2): the vector lane's floor in answers mode. 15 by default since 1.5.10
# (owner's decision of 16 Oct 2026); 1.5.9's floor was 25.
ANSWER_VECTOR_FLOOR = _switch(
    "ANSWER_VECTOR_FLOOR", "int", 15, 15,
    "GD-R. The vector lane's cosine floor in answers mode, in hundredths (0 to 100; more reads as 100). With the "
    "node's default embedding model (all-MiniLM-L6-v2) a broad question scores low against short first-person "
    "notes: on the grantee density fixture (synthetic owner, 15 Oct) 1 of 22 supporting records cleared 0.25, 6 "
    "cleared 0.20, 11 cleared 0.15 and 16 cleared 0.10, so the writer saw one record. It changes only which "
    "already-permitted members are ranked into the answer's candidates, never what a grant permits: every member "
    "still passes the same walk and checks. Named search, batch and dispatch keep 0.25 whatever this says. The "
    "default, 15, is 1.5.10's (0.10 was measured and gained nothing at 30 items); 25 is 1.5.9's floor.")
# Lane GD-A′ (15 Oct 2026, grantee density round 2): the dense template asks for sentences that answer the
# question. On by default since 1.5.10 (owner's decision of 16 Oct 2026); inert while ANSWER_DENSE is off.
ANSWER_DENSE_ON_QUESTION = _switch(
    "ANSWER_DENSE_ON_QUESTION", "bool", True, True,
    "GD-A′. With ANSWER_DENSE on, the writer's template is v10: v9 with only its length sentence replaced, so "
    "the writer writes only sentences that answer the question, as many as the items support up to twelve, in one "
    "to three paragraphs, and never presents an item about something else as the answer. On the grantee density "
    "fixture v9's \"seven to twelve sentences\" made the 9B writer pad with unrelated items when the items did not "
    "answer the question. The 2,048-token budget, the paragraphs, every check and every receipt counter are v9's; "
    "the receipt and the writer say v10. With ANSWER_DENSE off it changes nothing (template v8, as in 1.5.9).")
# Lane GD-L (15 Oct 2026, grantee density round 3): the name rule in one cited item. The owner's direction of 15 Oct:
# for grants, the looser method that gives the most facts, never anything the grant does not permit.
ANSWER_NAME_ONE_ITEM = _switch(
    "ANSWER_NAME_ONE_ITEM", "bool", False, False,
    "GD-L. A capitalised name in the question that nothing on this node is named by (any entity's name or alias, "
    "of any type; any contact's name or handle; any Off-limits entry), that is no word of the owner's (their own "
    "names, the account's words) and whose name has no such word beside it (\"Lena Ortiz\" binds whole when Lena is "
    "known) has to be in one item a sentence cites, not in every one; two or more such names in the same item. A "
    "known person's name still has to be in every cited item, and so does every name of a sentence that cites an "
    "item lacking the loosened name and naming a known person. Acts only while ANSWER_SUPPORT_JUDGE is on. The "
    "owner-word rule, the Off-limits refusal and body check, retrieval and the grant boundary are unchanged. Nothing "
    "loosens when the node's people, its names or the owner's names cannot be read or are empty. Off: every name in "
    "every cited item, as in 1.5.9.")
# Lane NR179 (15 Oct 2026, the 1.5.10 latency plan): an answer job's time under the node's own background load. On
# bound and unbound alike since 1.5.10 (owner decisions D1, D2); reading one costs no `is_bound()` stat.
ANSWER_HOLDS_BACKGROUND = _switch(
    "ANSWER_HOLDS_BACKGROUND", "bool", True, True,
    "NR179 G1 (decision D2). While a share's answer job is queued or running, a search-index build the owner-change "
    "queue or the refresh loop's restore would start, and an automatic or upgrade-step graph rebuild, wait until it "
    "ends (at most 110 s, the job's own deadline, and never past the node's stop) before they start. Starts only: a "
    "build already running is not stopped or shortened. An ended share's removal never waits, and the owner's own "
    "rebuilds never wait. Fresh data reaches the index later. On bound and unbound alike since 1.5.10 (owner, 15 Oct: 'Yes on "
    "the first 4 of those', D2). Off: they start at once, as in 1.5.9.")
ANSWER_JUDGE_RESERVE_SECONDS = _switch(
    "ANSWER_JUDGE_RESERVE_SECONDS", "int", 8, 8,
    "NR179 G2 (decision D1). Seconds before an answer job's deadline (110 s from acceptance) under which no further "
    "support-judge call starts: the sentences not yet judged are dropped (`dropped_unjudged`; the judge only drops) "
    "and the job ends answered with the sentences already kept, or as all_sentences_dropped, instead of `deadline`. "
    "Set, each judge call is also cut off 5 s before the deadline, and its sentence counts as unjudged. A set value "
    "is read within 6 to 60. Because of that cut-off a reserve above about 10 buys little (in WS0's review grid 20 "
    "cost 10 answers); 6 to 10 is the suggestion. 8 is the default on bound and unbound alike since 1.5.10 (owner, "
    "16 Oct: '8 seconds (Recommended)', D1). 0: no reserve, as in 1.5.9.")

SWITCHES = (
    ENABLED, CONFIG_PATH, EVIDENCE_REVIEWS, PROJECTION_REVIEWS, IDENTITY_ATTESTATIONS, INGEST_SNAPSHOTS,
    INGEST_SNAPSHOT_ROOT, MESSAGE_SEARCH, MESSAGE_SEARCH_BATCH, ANSWERS, INDEX_RESTORE,
    ASSESSMENT_CATCHUP, INDEX_RESTORE_MIN_INTERVAL, ASSESSMENT_CATCHUP_MAX_PER_PASS, AUTO_RESYNC, JOURNAL_SOURCES,
    JOURNAL_GOAL_FIELD, INTEREST_SOURCES, INTEREST_RELABEL, DERIVED_FACTS, PERMITTED_DERIVATION,
    ENTAILMENT_GROUNDING, ENTAILMENT_MODEL_JUDGE, ENTAILMENT_SENTENCE_REPORTING, SEARCH_TIMINGS, SEARCH_ATTRIBUTION,
    DECIDE_OUTSIDE_GATE, OWNER_LANE, OWNER_LANE_SLOTS, OWNER_LANE_SHARED_MAX, OWNER_LANE_TYPES, REVIEW_PASS_BOUNDARY,
    REVIEW_PASS_BOUNDARY_MAX_AGE, SWEEP_INTEREST_VERDICT, SWEEP_INTEREST_VERDICT_FULL_EVERY,
    OWNER_CAPTURE_APP_IDS, SELECTIVE_FACTS, ANSWER_SUBJECT_WORDS, ANSWER_SUPPORT_JUDGE, ANSWER_ITEMS,
    ANSWER_SHORT_WORD_NAMES, ANSWER_QUESTION_ANCHOR, ANSWER_SUPPORT_JUDGE_TEMPLATE,
    ANSWER_TIME_SCOPE, ANSWER_TIME_RETRIEVAL, ANSWER_TIME_FILL, ANSWER_TIME_PROMPT, ANSWER_TIME_GUARD,
    ANSWER_CITATION_FORMS, ANSWER_NAME_CLASSES, ANSWER_DENSE, ANSWER_VECTOR_FLOOR, ANSWER_DENSE_ON_QUESTION,
    ANSWER_NAME_ONE_ITEM, ANSWER_HOLDS_BACKGROUND, ANSWER_JUDGE_RESERVE_SECONDS,
)
BY_NAME = {item.name: item for item in SWITCHES}


# --- the one parser ----------------------------------------------------------------------------------------------

def lookup(switch) -> Switch:
    """A row of the table, by the row itself or by its environment name. Any other name is a programming error."""
    if isinstance(switch, Switch):
        return switch
    found = BY_NAME.get(switch)
    if found is None:
        raise KeyError(f"not a sharing switch: {switch!r}")
    return found


_unreadable_logged: set = set()


def _unreadable(item: Switch, reading: str):
    if item.name not in _unreadable_logged:
        _unreadable_logged.add(item.name)
        _log.warning("permissions v2: %s holds a value this node cannot read; it reads as %s", item.name, reading)


def parse(switch, raw):
    """What one setting says, by the rules in the module docstring. None means it says nothing: the default holds."""
    item = lookup(switch)
    if raw is None:
        return None
    raw = str(raw)
    if item.kind == "list":
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    text = raw.strip()
    if not text:
        return None
    if item.kind == "path":
        return raw
    folded = text.lower()
    if item.kind == "bool":
        if folded in _ON:
            return True
        if folded not in _OFF:
            _unreadable(item, "off")
        return False
    if item.kind == "int":
        if text.isascii() and text.isdigit():
            return max(int(text), item.floor)
        _unreadable(item, "its default")
        return None
    if item.kind == "choice":
        if folded in item.choices:
            return folded
        _unreadable(item, "none")
        return None
    raise ValueError(f"unknown switch kind {item.kind!r}")


def explicit(switch, env: Mapping | None = None):
    """The override the environment sets for this switch, or None when it sets none."""
    item = lookup(switch)
    env = os.environ if env is None else env
    return parse(item, env.get(item.name))


def default(switch, env: Mapping | None = None):
    """The switch's default for this node now: its bound default on a bound node, its 1.4.4 default otherwise."""
    item = lookup(switch)
    if item.unbound == item.bound:
        return item.bound
    return item.bound if is_bound(env) else item.unbound


def value(switch, env: Mapping | None = None):
    """What the switch is: the environment's override when it sets one, otherwise its default for the node."""
    item = lookup(switch)
    env = os.environ if env is None else env
    found = parse(item, env.get(item.name))
    result = default(item, env) if found is None else found
    reads = _OBSERVED.get()
    if reads is not None:
        reads.append((item.name, result))
    return result


#: NR187 (WS0 review): the switch reads of the computation running inside `observed()` on this context, else None.
_OBSERVED: contextvars.ContextVar = contextvars.ContextVar("topos_permissions_v2_switch_reads", default=None)


@contextlib.contextmanager
def observed():
    """Every switch this context reads inside, in order, as (name, value): what a computation actually read, for a
    caller that keeps its result only while the switches are what it read (search_index `_interests_current_kept`).
    A context variable, so another thread's reads are never in it. Switch names and values only."""
    reads: list = []
    token = _OBSERVED.set(reads)
    try:
        yield reads
    finally:
        _OBSERVED.reset(token)


def on(switch, env: Mapping | None = None) -> bool:
    """Whether an on/off switch is on."""
    item = lookup(switch)
    if item.kind != "bool":
        raise TypeError(f"{item.name} is not an on/off switch")
    return bool(value(item, env))


def number(switch, env: Mapping | None = None) -> int:
    item = lookup(switch)
    if item.kind != "int":
        raise TypeError(f"{item.name} is not a number")
    return int(value(item, env))


def choice(switch, env: Mapping | None = None) -> str | None:
    item = lookup(switch)
    if item.kind != "choice":
        raise TypeError(f"{item.name} is not a choice")
    return value(item, env)


def entries(switch, env: Mapping | None = None) -> tuple:
    item = lookup(switch)
    if item.kind != "list":
        raise TypeError(f"{item.name} is not a list")
    return tuple(value(item, env))


def snapshot_root(canonical_database, env: Mapping | None = None) -> str | None:
    """``INGEST_SNAPSHOT_ROOT``: as set, or on a bound node the one folder the runtime accepts, beside the database."""
    found = explicit(INGEST_SNAPSHOT_ROOT, env)
    if found is not None:
        return found
    if not is_bound(env):
        return None
    return str(Path(canonical_database).parent / INGEST_SNAPSHOT_ROOT.bound)


# --- bound -------------------------------------------------------------------------------------------------------

_lock = threading.Lock()
_served_memo: tuple | None = None    # (what it was derived from, until when, the served database or None)
_verdicts: dict = {}                 # (served, config, the config file's stat) -> whether it binds
_bound_seen: bool | None = None


def _served_database(now: float) -> Path | None:
    """The database this node serves (``storage.db.paths.resolve_active_database``, as the runtime binds it).

    Resolved, links included, as ``load_runtime`` and the bind resolve it: a database file that is a link keeps its
    sharing folder beside the file it links to, so "beside the served database" means one folder everywhere (review
    N2 finding 2: the bind wrote there while this looked beside the link, and the node could never bind).

    Remembered for ``LOCATION_SECONDS`` against what it is derived from: the explicit database path (environment
    and settings), the home directory and the resolver itself. A change to any of them is seen at once."""
    global _served_memo
    try:
        from topos.storage.db import paths
        resolve = paths.resolve_active_database
    except Exception:  # noqa: BLE001 -- no storage layer: nothing is bound to it
        return None
    loaded = sys.modules.get("topos.config.settings")
    pinned = getattr(getattr(loaded, "settings", None), "topos_database_path", None)
    key = (os.environ.get("TOPOS_DATABASE_PATH"), os.environ.get("HOME"), str(pinned), resolve)
    memo = _served_memo
    if memo is not None and memo[0] == key and now < memo[1]:
        return memo[2]
    try:
        found = resolve().path
        served = Path(found).resolve() if found else None
    except Exception:  # noqa: BLE001 -- no database to serve, or a link that loops: nothing is bound to it
        served = None
    _served_memo = (key, now + LOCATION_SECONDS, served)
    return served


def default_config_path() -> Path | None:
    """Where a node keeps its private sharing config when ``TOPOS_PERMISSIONS_V2_CONFIG_PATH`` is unset."""
    served = _served_database(time.monotonic())
    return None if served is None else served.parent / CONFIG_PATH.bound


def _refusal(config: Path, served: Path | None) -> str | None:
    """Why this config would not bind this node, by the checks ``load_runtime`` makes before it opens anything."""
    from .canonical import PolicyError
    from .runtime import BETA_ENVIRONMENT_PREFIX, NodeProtocolConfig, _private_file
    try:
        parsed = NodeProtocolConfig.parse(_private_file(config.resolve(strict=True)))
    except PolicyError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 -- unreadable or not the node config
        return "private_config_invalid"
    if not parsed.identity.environment_id.startswith(BETA_ENVIRONMENT_PREFIX) or not parsed.trusted_cp_keys:
        return "beta_configuration_required"
    canonical = Path(parsed.canonical_database_path)
    if not canonical.is_absolute():
        return "absolute_paths_required"
    try:
        if served is None or canonical.resolve(strict=True) != served.resolve(strict=True):
            return "canonical_database_binding"
    except OSError:
        return "canonical_database_binding"
    return None


def _seen(bound: bool) -> bool:
    """Debug lines only: a predicate read on every request must not change what a node logs."""
    global _bound_seen
    if bound != _bound_seen:
        if bound:
            _log.debug("permissions v2: this node is bound for sharing (its private config loads)")
        elif _bound_seen:
            _log.debug("permissions v2: this node is no longer bound for sharing")
        _bound_seen = bound
    return bound


def is_bound(env: Mapping | None = None) -> bool:
    """Whether this node is bound for sharing (module docstring). Never raises, never writes, never logs a path."""
    env = os.environ if env is None else env
    if parse(ENABLED, env.get(ENABLED.name)) is False:
        return False
    served = _served_database(time.monotonic())
    set_path = parse(CONFIG_PATH, env.get(CONFIG_PATH.name))
    if set_path is not None:
        config = Path(set_path)
        if not config.is_absolute():
            return _seen(False)
    elif served is None:
        return _seen(False)
    else:
        config = served.parent / CONFIG_PATH.bound
    try:
        found = os.stat(config)
    except OSError:
        return _seen(False)
    key = (str(served), str(config), found.st_dev, found.st_ino, found.st_mtime_ns, found.st_size, found.st_mode)
    verdict = _verdicts.get(key)
    if verdict is None:
        refusal = _refusal(config, served)
        if refusal is not None:   # the runtime refuses with the same code when something asks it to load
            _log.debug("permissions v2: a sharing config is present but does not bind this node (%s)", refusal)
        verdict = refusal is None
        with _lock:
            if len(_verdicts) >= 32:
                _verdicts.clear()
            _verdicts[key] = verdict
    return _seen(verdict)


def forget_bound() -> None:
    """Forget where the config was looked for and what it said: the next ``is_bound()`` derives both again."""
    global _served_memo
    with _lock:
        _served_memo = None
        _verdicts.clear()


def table() -> list[dict]:
    """The table as rows (name, kind, unbound default, bound default, purpose), for reports and release notes."""
    return [{"name": item.name, "kind": item.kind, "unbound": item.unbound, "bound": item.bound,
             "purpose": item.purpose} for item in SWITCHES]
