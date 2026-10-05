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
new is served. On a bound node with an empty environment these are on: the master switch, the knowledge search
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
SOURCE_RELEASE = _switch(
    "SOURCE_RELEASE_ENABLED", "bool", False, False,
    "The locator door (p2a; dark).")
FACT_RELEASE = _switch(
    "FACT_RELEASE_ENABLED", "bool", False, False,
    "The fact door (P2b; dark).")
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
SHADOW_INDEX = _switch(
    "SHADOW_INDEX_ENABLED", "bool", False, False,
    "Shadow audit: an index of the locator door's releases.")
SHADOW_LABELER = _switch(
    "SHADOW_LABELER", "choice", None, None,
    "Shadow audit: its second labeler.", choices=("local",))
OWNER_CAPTURE_APP_IDS = Switch(
    "TOPOS_OWNER_CAPTURE_APP_IDS", "list", ("chatgpt-shadow-extension",), ("chatgpt-shadow-extension",),
    "Capture apps whose stamped AI-chat rows count as the owner's (OD-39); mirrors the control plane's list.")

SWITCHES = (
    ENABLED, CONFIG_PATH, EVIDENCE_REVIEWS, PROJECTION_REVIEWS, IDENTITY_ATTESTATIONS, INGEST_SNAPSHOTS,
    INGEST_SNAPSHOT_ROOT, MESSAGE_SEARCH, MESSAGE_SEARCH_BATCH, ANSWERS, SOURCE_RELEASE, FACT_RELEASE, INDEX_RESTORE,
    ASSESSMENT_CATCHUP, INDEX_RESTORE_MIN_INTERVAL, ASSESSMENT_CATCHUP_MAX_PER_PASS, AUTO_RESYNC, JOURNAL_SOURCES,
    JOURNAL_GOAL_FIELD, INTEREST_SOURCES, INTEREST_RELABEL, DERIVED_FACTS, PERMITTED_DERIVATION,
    ENTAILMENT_GROUNDING, ENTAILMENT_MODEL_JUDGE, ENTAILMENT_SENTENCE_REPORTING, SEARCH_TIMINGS, SHADOW_INDEX,
    SHADOW_LABELER, OWNER_CAPTURE_APP_IDS,
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
    return default(item, env) if found is None else found


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
