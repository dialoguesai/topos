"""Fact packs the owner adds from a file (`topos-node packs add FILE`).

A custom pack is one pack YAML in the node's own data directory (`fact-packs/` beside the node's
env file, normally `~/.topos/fact-packs/`), outside the installed package. `packs.load_packs`
composes these files with the bundled catalog, so every reader of the catalog (the registry, the
Fact packs page, the ingest job, a backfill, an app's `pack_facts` read) meets a custom pack the
way it meets a bundled one, and applies the same rules to it.

What is different about a custom pack, all of it decided here and in `registry`:

* Its id and every predicate it declares are in the reserved `custom.` namespace. No bundled pack
  and no grant vocabulary uses that namespace (pinned by tests/features/derivation/
  test_custom_packs.py), so a custom pack cannot shadow, replace or collide with a bundled pack:
  not one that ships today and not one a later release adds.
* It is loaded untrusted. It describes the owner only (`net_subject: deny`, which the loader
  already holds every pack outside the package to), its facts are `owner_only`, it declares stated
  predicates only and no `synthesis`. Those are narrower than a bundled pack on purpose: each is a
  part of the pipeline that was never run against a pack the engine did not ship.
* It starts switched off, and it runs only as the definition the owner switched on: a file that
  changed, or that was removed and added again, is switched off again (`registry.seed_pack_registry`,
  `registry.runs_as_switched_on`, and the stamp `install_pack` and `remove_pack` renew).
* While it is off nothing reads a record for it. The ingest job's shadow trial, which samples
  records for a bundled pack that is off so it can offer it, never runs for a pack from outside
  the package (`derivation_job._self_gate`).
* A file that does not validate is refused by `install_pack` with one plain sentence. At run time
  it is skipped and said once in the log, and every other pack, bundled or added, keeps working:
  one bad file in the owner's data directory must not take the fact layer down, and must not
  switch the owner's other packs off. That holds for a file the parser itself chokes on (a date
  that does not exist, a number too long to read, brackets nested without end): whatever one
  file raises is that file's problem alone (`_scan`). Every part of a pack the engine reads
  is checked for its shape here (`check_definition`), because the ingest job builds a prefilter
  for every loaded pack, on or off: a shape it cannot iterate would stop every pack's batch.
* A refusal names a rule, a position, or a name that already passed its own check. It never
  carries text from the file that did not: a pack file can come from someone else, and the same
  sentence goes to the terminal and the node log.

Nothing here is reachable by an app. There is no route, relay message or tool that adds a pack:
the only writer of that directory is the `packs add` command, which asks a person on a terminal.

One limit, stated: a file removed and put back BY HAND, byte for byte the same, with no seeding in
between, is not told apart from a file that never left. `packs remove` and `packs add` are.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from .packs import (ALTITUDES, CARDINALITIES, KNOWN_VALUE_TYPES, ROLE_POLICIES, SENSITIVITY, TEMPORALS,
                    Pack, PackValidationError, load_pack)

logger = logging.getLogger(__name__)

#: The namespace every custom pack id and predicate lives in.
NAMESPACE = "custom"
MAX_PACKS = 20
MAX_PREDICATES = 50
MAX_FIELDS = 50
MAX_ENUM_VALUES = 100
MAX_BYTES = 256_000
MAX_DEPTH = 12
MAX_ROUTING_ITEMS = 200
MAX_ABSTENTIONS = 50

#: `custom.gardening`, `custom.garden.beds`. Also a pack id `topos-node packs backfill --pack` takes.
_PACK_ID_RE = re.compile(r"^custom(\.[a-z][a-z0-9_]{0,39}){1,3}$")
_PREDICATE_RE = re.compile(r"^custom(\.[a-z][a-z0-9_]{0,39}){1,4}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
_EVENT_IDENTITY_RE = re.compile(r"^(once|dated|windowed:[0-9]{1,4})$")
#: What a pack you add may put under `revision`. `exclusive_with` and `closes` are left out: the writer walks
#: nested shapes for them that were never run against a pack the engine did not ship.
_REVISION_KEYS = ("supersedes_on", "noop_when")
_SCALARS = (str, int, float, bool, type(None), datetime.date)
_VALUE_TYPES = KNOWN_VALUE_TYPES | {"structured", "enum"}

#: Who a pack reads, in the words the `packs add` prompt uses.
ROLE_POLICY_WORDS = {
    "authored_only": "only what you wrote",
    "authored_addressed": "what you wrote and what was written to you",
    "any_with_label": "what you wrote, what was written to you, and what you only saw (each fact says which)",
}


class CustomPackError(PackValidationError):
    """Why a file is not a pack the owner can add. The message is one plain sentence, shown as it is."""


def custom_pack_dir() -> Path:
    """Where this node keeps the packs its owner added: beside the node's env file."""
    from topos.owner_key import _env_path

    return _env_path().parent / "fact-packs"


def is_custom_id(pack_id: Any) -> bool:
    return isinstance(pack_id, str) and bool(_PACK_ID_RE.match(pack_id))


# --- validation ----------------------------------------------------------------------------------
def _refuse(message: str) -> None:
    raise CustomPackError(message)


def _text(value: Any, low: int, high: int) -> bool:
    return isinstance(value, str) and low <= len(value.strip()) <= high


def _one_line(value: Any, low: int, high: int) -> bool:
    """Text that can be printed on a terminal as it is: no line break, no control or formatting character.
    The `packs add` prompt prints the title above what the owner is asked to approve, so a title that could
    move the cursor or start a new line could rewrite the lines under it."""
    return _text(value, low, high) and value.isprintable()


def _one_of(value: Any, allowed: Iterable[str]) -> bool:
    """A word from a fixed list. Asked of text only: a list or a mapping where a word belongs cannot be looked
    up in a set at all, and the lookup raised instead of refusing."""
    return isinstance(value, str) and value in allowed


def _texts(value: Any, low: int, high: int, longest: int) -> bool:
    return (isinstance(value, list) and low <= len(value) <= high
            and all(_text(item, 1, longest) for item in value))


_NOT_UNICODE = "The file holds text that is not valid Unicode. Remove the escape that starts with a backslash and `ud`."


def _storable(text: str) -> bool:
    """Text that can be written down as UTF-8. A YAML escape for half of a surrogate pair parses to text that
    cannot: the registry digest, the database and the model request would each raise on it."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _check_plain_tree(raw: Any) -> None:
    """The whole file, before any rule reads a part of it: plain YAML only. Text keys, plain values, each part
    written once. The digest the registry records (`registry.definition_sha`) and the engine walk the parsed
    file on every load; a key that is not text cannot be ordered, and an alias that repeats a part can make a
    small file a very large walk."""
    seen: set = set()
    stack: List[Tuple[Any, int]] = [(raw, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, (dict, list)):
            if id(node) in seen:
                _refuse("The file uses a YAML anchor or alias. Write each part out in full.")
            seen.add(id(node))
            if depth >= MAX_DEPTH:
                _refuse(f"The file nests more than {MAX_DEPTH} levels deep.")
            if isinstance(node, dict):
                if not all(isinstance(key, str) for key in node):
                    _refuse("Every key in the file must be text. Put a key in quotes if it looks like a "
                            "number, a date or yes/no.")
                if not all(_storable(key) for key in node):
                    _refuse(_NOT_UNICODE)
                stack.extend((value, depth + 1) for value in node.values())
            else:
                stack.extend((value, depth + 1) for value in node)
        elif not isinstance(node, _SCALARS):
            _refuse("The file uses a YAML type a pack cannot hold. Use text, numbers, true/false, lists and "
                    "mappings.")
        elif isinstance(node, str) and not _storable(node):
            _refuse(_NOT_UNICODE)


def _check_enum(values: Any, where: str) -> None:
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_ENUM_VALUES
            or not all(_text(v, 1, 200) for v in values) or len(set(values)) != len(values)):
        _refuse(f"{where} must list 1 to {MAX_ENUM_VALUES} different text values.")


def _check_field(spec: Any, where: str, scales: Dict[str, List[str]]) -> None:
    """One field of a structured value: a known type, a list of allowed values, `{enum: [...]}`, or
    `{scale: <a scale Topos ships>}`. Anything else the extraction layer would treat as free text."""
    if isinstance(spec, str):
        if spec.strip() not in KNOWN_VALUE_TYPES:
            _refuse(f"{where} has a type Topos does not know. Use one of: {', '.join(sorted(KNOWN_VALUE_TYPES))}.")
        return
    if isinstance(spec, list):
        return _check_enum(spec, where)
    if isinstance(spec, dict) and set(spec) == {"enum"}:
        return _check_enum(spec["enum"], where)
    if isinstance(spec, dict) and set(spec) == {"scale"}:
        if not _one_of(spec["scale"], scales):
            _refuse(f"{where} names a scale Topos does not ship. Use one of: {', '.join(sorted(scales))}.")
        return
    _refuse(f"{where} must be a type name, a list of allowed values, {{enum: [...]}} or {{scale: name}}.")


def _check_field_names(value: Any, schema: Any, where: str) -> None:
    """`key_fields` / `required_fields`: a list of fields the predicate's `value_schema` declares."""
    if value is None:
        return
    if (not isinstance(schema, dict) or not isinstance(value, list) or not 1 <= len(value) <= MAX_FIELDS
            or not all(isinstance(field, str) and field in schema for field in value)
            or len(set(value)) != len(value)):
        _refuse(f"{where} must list fields of that predicate's `value_schema`, each once.")


def _check_predicate(pred: Any, position: int, seen: set, scales: Dict[str, List[str]]) -> None:
    if not isinstance(pred, dict) or not isinstance(pred.get("name"), str):
        _refuse("Each entry under `predicates` needs a `name`.")
    name = pred["name"]
    if not _PREDICATE_RE.match(name):
        _refuse(f"The name of predicate {position} must start with `{NAMESPACE}.` and use lowercase letters, "
                "digits and underscores, like custom.plant.")
    if name in seen:
        _refuse(f"The predicate {name} is declared twice.")
    seen.add(name)
    if not _one_of(pred.get("cardinality"), CARDINALITIES):
        _refuse(f"{name}: `cardinality` must be one of: {', '.join(CARDINALITIES)}.")
    if not _one_of(pred.get("temporal"), TEMPORALS):
        _refuse(f"{name}: `temporal` must be one of: {', '.join(TEMPORALS)}.")
    if pred.get("altitude") != "stated":
        allowed = ", ".join(a for a in ALTITUDES if a != "stated")
        _refuse(f"{name}: `altitude` must be stated. A pack you add reads what a record says; "
                f"{allowed} facts are not supported in one yet.")
    if pred.get("sensitivity") is not None and not _one_of(pred.get("sensitivity"), SENSITIVITY):
        _refuse(f"{name}: `sensitivity` must be one of: {', '.join(SENSITIVITY)}.")
    value_type = pred.get("value_type")
    if not _one_of(value_type, _VALUE_TYPES):
        _refuse(f"{name}: `value_type` must be structured, enum or one of: {', '.join(sorted(KNOWN_VALUE_TYPES))}.")
    schema, values = pred.get("value_schema"), pred.get("values")
    if value_type == "structured":
        if not isinstance(schema, dict) or not 1 <= len(schema) <= MAX_FIELDS:
            _refuse(f"{name}: a structured predicate needs `value_schema` with 1 to {MAX_FIELDS} fields.")
        for number, (field, spec) in enumerate(schema.items(), 1):
            if not isinstance(field, str) or not _FIELD_RE.match(field):
                _refuse(f"{name}: the name of field {number} must use lowercase letters, digits and underscores.")
            _check_field(spec, f"{name}.{field}", scales)
    elif schema is not None:
        _refuse(f"{name}: `value_schema` goes with value_type: structured.")
    if value_type == "enum":
        _check_enum(values, f"{name}: `values`")
    elif values is not None:
        _refuse(f"{name}: `values` goes with value_type: enum.")
    _check_field_names(pred.get("key_fields"), schema, f"{name}: `key_fields`")
    _check_field_names(pred.get("required_fields"), schema, f"{name}: `required_fields`")
    if pred.get("qualifiers") is not None and not isinstance(pred["qualifiers"], dict):
        _refuse(f"{name}: `qualifiers` must be a mapping.")
    identity = pred.get("event_identity")
    if identity is not None and not (isinstance(identity, str) and _EVENT_IDENTITY_RE.match(identity)):
        _refuse(f"{name}: `event_identity` must be once, dated or windowed:<days>, like windowed:45.")
    if pred.get("note") is not None and not _text(pred["note"], 0, 2000):
        _refuse(f"{name}: `note` must be text of up to 2000 characters.")


def _check_routing(routing: Any) -> None:
    """`routing` feeds the prefilter the ingest job builds for EVERY loaded pack, on or off."""
    if routing is None:
        return
    if not isinstance(routing, dict):
        _refuse("`routing` must be a mapping.")
    for key in ("descriptors", "exemplars"):
        if routing.get(key) is not None and not _texts(routing[key], 0, MAX_ROUTING_ITEMS, 500):
            _refuse(f"`routing.{key}` must be a list of up to {MAX_ROUTING_ITEMS} short texts.")


def _check_guidance(guidance: Any) -> None:
    if guidance is None:
        return
    if not isinstance(guidance, dict):
        _refuse("`guidance` must be a mapping.")
    if guidance.get("definitions") is not None and not _text(guidance["definitions"], 0, 4000):
        _refuse("`guidance.definitions` must be text of up to 4000 characters.")
    if guidance.get("abstention") is not None and not _texts(guidance["abstention"], 0, MAX_ABSTENTIONS, 1000):
        _refuse(f"`guidance.abstention` must be a list of up to {MAX_ABSTENTIONS} texts.")


def _check_revision(revision: Any, predicates: List[Dict[str, Any]]) -> None:
    if revision is None:
        return
    if not isinstance(revision, dict) or not set(revision) <= set(_REVISION_KEYS):
        _refuse("`revision` must be a mapping that holds only `supersedes_on` and `noop_when` in a pack you add.")
    if revision.get("noop_when") is not None and not _text(revision["noop_when"], 0, 200):
        _refuse("`revision.noop_when` must be short text.")
    supersedes = revision.get("supersedes_on")
    if supersedes is None:
        return
    schemas = {pred["name"]: pred.get("value_schema") for pred in predicates}
    if not isinstance(supersedes, dict) or not all(
            name in schemas and isinstance(schemas[name], dict) and isinstance(fields, list) and fields
            and all(isinstance(field, str) and field in schemas[name] for field in fields)
            for name, fields in supersedes.items()):
        _refuse("`revision.supersedes_on` must map a structured predicate of this pack to a list of its fields.")


def check_definition(raw: Any, *, scales: Optional[Dict[str, List[str]]] = None) -> None:
    """The rules a custom pack is held to beyond the engine's own loader, each refusal one plain sentence.
    Raises :class:`CustomPackError`."""
    if not isinstance(raw, dict):
        _refuse("A pack file is a YAML mapping: `pack: custom.name`, `version: ...`, `predicates: ...`.")
    _check_plain_tree(raw)
    pid = raw.get("pack")
    if not is_custom_id(pid):
        _refuse(f"`pack` must be an id that starts with `{NAMESPACE}.`, like custom.gardening "
                "(lowercase letters, digits and underscores). That keeps it apart from the packs Topos ships.")
    if not isinstance(raw.get("version"), str) or not _VERSION_RE.match(raw["version"]):
        _refuse('`version` must be text like "1.0.0". Put it in quotes if it looks like a number.')
    if not _one_line(raw.get("title"), 1, 120):
        _refuse("`title` must be a short name for the pack on one line (1 to 120 characters, "
                "no control characters).")
    if not _one_of(raw.get("sensitivity_class"), SENSITIVITY):
        _refuse(f"`sensitivity_class` must be one of: {', '.join(SENSITIVITY)}.")
    if not _one_of(raw.get("role_policy"), ROLE_POLICIES):
        _refuse(f"`role_policy` must be one of: {', '.join(ROLE_POLICIES)}.")
    if str(raw.get("net_subject") or "deny").strip().lower() != "deny":
        _refuse("A pack you add can only describe you. Remove `net_subject: allow`.")
    if raw.get("disclosure_default") is not None and raw.get("disclosure_default") != "owner_only":
        _refuse("`disclosure_default` must be owner_only (or left out).")
    if raw.get("synthesis"):
        _refuse("`synthesis` is not supported in a pack you add yet. Remove it.")
    predicates = raw.get("predicates")
    if not isinstance(predicates, list) or not 1 <= len(predicates) <= MAX_PREDICATES:
        _refuse(f"`predicates` must list 1 to {MAX_PREDICATES} predicates.")
    if scales is None:
        from .packs import load_scales
        from .registry import bundled_pack_dir

        scales = load_scales(bundled_pack_dir())
    seen: set = set()
    for position, pred in enumerate(predicates, 1):
        _check_predicate(pred, position, seen, scales)
    evaluation = raw.get("eval")
    for kind in ("gold", "negative_controls"):
        cases = evaluation.get(kind) if isinstance(evaluation, dict) else None
        if (not isinstance(cases, list) or not cases
                or not all(isinstance(case, dict) and _text(case.get("text"), 1, 4000) for case in cases)):
            _refuse(f"`eval.{kind}` must list at least one example, each with a `text`.")
    consumers = raw.get("consumers")
    if not isinstance(consumers, list) or not consumers or not all(_text(c, 1, 60) for c in consumers):
        _refuse("`consumers` must list at least one name, for example [assistant_context].")
    _check_routing(raw.get("routing"))
    _check_guidance(raw.get("guidance"))
    _check_revision(raw.get("revision"), predicates)


#: The one sentence for a file no rule names: said instead of whatever a check or the engine's loader raised.
_UNREADABLE = "The file is not a pack Topos can read."


def parse_definition(content: bytes) -> Dict[str, Any]:
    """The pack a file's bytes declare, checked. Raises :class:`CustomPackError`; never echoes the content."""
    if len(content) > MAX_BYTES:
        _refuse(f"The file is larger than {MAX_BYTES // 1000} KB.")
    try:
        raw = yaml.safe_load(content.decode("utf-8"))
    except UnicodeDecodeError:
        _refuse("The file is not UTF-8 text.")
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        _refuse("The file is not valid YAML" + (f" (line {mark.line + 1})." if mark is not None else "."))
    except RecursionError:
        _refuse(f"The file nests more than {MAX_DEPTH} levels deep.")
    except Exception:  # noqa: BLE001 -- the parser's own error for a value it cannot build, never passed on
        # Not a `YAMLError`: the parser raises a bare ValueError for a date that does not exist (2026-13-45) or a
        # whole number of more than 4300 digits, and KeyError or AttributeError for a value that does not fit the
        # tag written before it (`!!bool maybe`, `!!timestamp soon`). Its message quotes the value.
        _refuse("The file holds a value that cannot be read: a date or time that does not exist, a number that "
                "is too long, or a value that does not fit its `!!` tag. Put it in quotes to keep it as text.")
    try:
        check_definition(raw)
    except CustomPackError:
        raise
    except RecursionError:
        _refuse(f"The file nests more than {MAX_DEPTH} levels deep.")
    except Exception as exc:  # noqa: BLE001 -- a shape no rule above names; never a raw error to the caller
        logger.warning("custom fact pack: a check raised %s; the file is refused", type(exc).__name__)
        _refuse(_UNREADABLE)
    return raw


def _load_checked(path: Path) -> Pack:
    """One custom pack file through both checks: this module's and the engine loader's. The loader's own refusal
    quotes the value it refused, so it is not passed on: every rule it holds a pack to is one `check_definition`
    states first, in a sentence of its own."""
    content = path.read_bytes()
    parse_definition(content)
    try:
        pack = load_pack(path, trusted=False)
    except CustomPackError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the loader's refusal, or a shape it did not expect
        raise CustomPackError(_UNREADABLE) from exc
    if pack.first_party or not is_custom_id(pack.pack):
        _refuse("A pack you add cannot be one Topos ships.")
    return pack


def _shown_name(path: Path) -> str:
    """A file's name as the log and `packs list` may print it. A name is text from outside too."""
    name = path.name
    return name if name.isprintable() and len(name) <= 120 else "(a file whose name cannot be shown)"


def _is_plain_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _scan() -> Tuple[Dict[str, Pack], Dict[str, str]]:
    """Every file in the custom directory: the packs that load, and for each file that does not, why."""
    packs: Dict[str, Pack] = {}
    problems: Dict[str, str] = {}
    directory = custom_pack_dir()
    from .registry import bundled_pack_dir

    try:
        if not directory.is_dir() or directory.resolve() == bundled_pack_dir().resolve():
            return packs, problems
        files = sorted(directory.glob("*.yaml"))
    except OSError:
        return packs, problems
    predicates: Dict[str, str] = {}
    for path in files:
        try:
            if not _is_plain_file(path):
                _refuse("It must be a regular file, not a link.")
            if path.stat().st_size > MAX_BYTES:
                _refuse(f"The file is larger than {MAX_BYTES // 1000} KB.")
            pack = _load_checked(path)
            if path.stem != pack.pack:
                _refuse(f"The file must be named {pack.pack}.yaml, after the pack it declares.")
            if len(packs) >= MAX_PACKS:
                _refuse(f"A node holds at most {MAX_PACKS} packs you added.")
            taken = [name for name in pack.predicates if name in predicates]
            if taken:
                _refuse(f"The predicate {taken[0]} is already declared by {predicates[taken[0]]}.")
        except CustomPackError as exc:
            problems[_shown_name(path)] = str(exc)
            continue
        except OSError:
            problems[_shown_name(path)] = "The file could not be read."
            continue
        except Exception as exc:  # noqa: BLE001 -- one file's error is that file's problem, whatever it is
            # Without this, one file the checks raised on (not refused) emptied the whole scan: no added pack
            # loaded, `packs list` named no file, and the next seeding switched every added pack off.
            logger.warning("custom fact pack %s: a check raised %s; the file is left out",
                           _shown_name(path), type(exc).__name__)
            problems[_shown_name(path)] = _UNREADABLE
            continue
        packs[pack.pack] = pack
        predicates.update({name: pack.pack for name in pack.predicates})
    return packs, problems


#: (file name, reason) pairs already said in the log: `load_packs` runs many times a minute.
_SAID: set = set()


def load_custom_packs() -> Dict[str, Pack]:
    """The owner's packs that load. A file that does not is left out and said once in the log (its name and
    the reason, never its content); it never raises."""
    try:
        packs, problems = _scan()
    except Exception:  # noqa: BLE001 -- the bundled packs must load whatever is in the owner's directory
        logger.warning("custom fact packs: the directory could not be read; none loaded", exc_info=True)
        return {}
    for name, reason in problems.items():
        if (name, reason) not in _SAID:
            _SAID.add((name, reason))
            logger.warning("custom fact pack %s is not loaded: %s", name, reason)
    return packs


def custom_pack_problems() -> Dict[str, str]:
    """file name -> why it is not loaded, for `topos-node packs list`."""
    try:
        return _scan()[1]
    except Exception:  # noqa: BLE001
        return {}


# --- adding and removing ---------------------------------------------------------------------------
def _bundled_names() -> Tuple[set, set]:
    from .packs import load_packs
    from .registry import bundled_pack_dir

    bundled = load_packs(bundled_pack_dir(), include_custom=False)
    return set(bundled), {name for pack in bundled.values() for name in pack.predicates}


def check_content(content: bytes) -> Pack:
    """The pack these bytes would add, fully checked. Writes nothing in the node's directory. Raises
    :class:`CustomPackError`."""
    raw = parse_definition(content)
    # The loader reads a path, and decides first-party-ness from it: check a private copy of the bytes, so the
    # answer is about what will be written and not about where the owner kept the file.
    try:
        with tempfile.TemporaryDirectory(prefix="topos-pack-check-") as scratch:
            copy = Path(scratch) / f"{raw['pack']}.yaml"
            copy.write_bytes(content)
            pack = _load_checked(copy)
    except OSError:
        _refuse("The file could not be checked: Topos could not write a temporary copy of it.")
    ids, names = _bundled_names()
    if pack.pack in ids or set(pack.predicates) & names:
        _refuse("That pack uses a name Topos ships. Give it and its predicates names of their own.")
    return pack


def read_candidate(source: Path) -> Tuple[Pack, bytes]:
    """The pack a file would add, fully checked, and the exact bytes that were checked: what `packs add` shows
    the owner, and what it hands to :func:`install_content` after the yes. Raises :class:`CustomPackError`."""
    source = Path(source)
    try:
        if not source.is_file():
            _refuse("That is not a file.")
        if source.stat().st_size > MAX_BYTES:
            _refuse(f"The file is larger than {MAX_BYTES // 1000} KB.")
        content = source.read_bytes()
    except OSError:
        _refuse("The file could not be read.")
    return check_content(content), content


def _write_beside(destination: Path, content: bytes) -> None:
    """Write a file in the pack directory in one step: staged beside the destination, then moved over it."""
    handle, name = tempfile.mkstemp(prefix=".adding-", suffix=".tmp", dir=destination.parent)
    staged = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


def _renew_stamp(pack_id: str) -> None:
    """A new random stamp beside the pack's file (`registry.definition_sha` reads it). Renewed whenever a pack
    file is written or removed, and before the file itself changes: a pack that was removed and added again
    is then never the one the registry row was switched on for, whether or not a seeding ran in between."""
    from .registry import install_stamp_path

    directory = custom_pack_dir()
    _write_beside(install_stamp_path(directory / f"{pack_id}.yaml", pack_id), secrets.token_hex(16).encode("ascii"))


def _others(pack_id: str) -> Dict[str, Pack]:
    return {pid: pack for pid, pack in _scan()[0].items() if pid != pack_id}


def install_pack(source: Path) -> Dict[str, Any]:
    """Check a pack file and put it in the node's directory, switched off: one read of the file, checked and
    written. See :func:`install_content`. Raises :class:`CustomPackError`."""
    return install_content(read_candidate(Path(source))[1])


def install_plan(pack: Pack) -> Dict[str, Any]:
    """What adding this checked pack would do on this node, writing nothing: `{"would", "replaces"}`, where
    `would` is `new`, `replace` or `unchanged` and `replaces` is the version already there (None for a new pack).
    Raises :class:`CustomPackError` for a pack this node would refuse: the rules that depend on what the node
    already holds, which checking the file alone cannot see. `install_content` decides by this, so what
    `packs add --check --json` says would happen is what happens."""
    directory = custom_pack_dir()
    from .registry import bundled_pack_dir, definition_sha

    if directory.resolve() == bundled_pack_dir().resolve():
        _refuse("This node's pack directory is the one Topos ships its own packs in.")
    others = _others(pack.pack)
    if len(others) >= MAX_PACKS:
        _refuse(f"This node already holds {MAX_PACKS} packs you added. Remove one first.")
    for other in others.values():
        taken = sorted(set(pack.predicates) & set(other.predicates))
        if taken:
            _refuse(f"The predicate {taken[0]} is already declared by {other.pack}.")
    destination = directory / f"{pack.pack}.yaml"
    if _is_plain_file(destination):
        try:
            old = _load_checked(destination)
        except (CustomPackError, OSError):
            old = None                              # an unreadable file there is simply replaced
        if old is not None:
            if definition_sha(old, stamped=False) == definition_sha(pack, stamped=False):
                return {"would": "unchanged", "replaces": old.version}
            if old.version == pack.version:
                _refuse(f"{pack.pack} {pack.version} is already added and this file differs from it. "
                        "Give the changed pack a new `version`.")
            return {"would": "replace", "replaces": old.version}
    return {"would": "new", "replaces": None}


def install_content(content: bytes) -> Dict[str, Any]:
    """Check these bytes and put them in the node's directory as a pack, switched off. Returns its id, version
    and title, the version it replaced (None for a new pack) and whether it was already there unchanged.

    It takes bytes, not a path, so `packs add` installs what it showed the owner: the file is read once, before
    the question, and a file that changed while the prompt was open is not read again. The bytes are checked
    again here, so nothing unchecked can be written by a caller that skipped the check.

    All or nothing: the bytes that were checked are written beside the destination and moved over it in one
    step, so a refusal leaves the directory as it was. Raises :class:`CustomPackError`."""
    pack = check_content(content)
    plan = install_plan(pack)
    directory = custom_pack_dir()
    destination = directory / f"{pack.pack}.yaml"
    if plan["would"] == "unchanged":
        return {"pack_id": pack.pack, "version": pack.version, "title": pack.title,
                "replaced": plan["replaces"], "unchanged": True, "path": str(destination)}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _renew_stamp(pack.pack)
        _write_beside(destination, content)
    except OSError:
        _refuse("The pack could not be written to this node's pack directory.")
    return {"pack_id": pack.pack, "version": pack.version, "title": pack.title,
            "replaced": plan["replaces"], "unchanged": False, "path": str(destination)}


def routing_reach(pack: Pack) -> Dict[str, List[str]]:
    """Which records this pack reads, as the engine decides it: `{"words", "phrases"}`.

    A record is sent to the model for a pack when it contains one of the phrases, or ONE of the words
    (`prefilter.PackPrefilter`: recall over precision). The words are that prefilter's own lexicon, taken from the
    object it builds and not worked out again here: the routing descriptors and exemplars, the allowed values of
    the pack's fields, and the texts of its own gold examples, less a short list of words that would match
    anything. So an everyday word in a gold text or an exemplar ("every", "today") sends most records to the
    model. Nothing here reads a record."""
    from .prefilter import PackPrefilter

    prefilter = PackPrefilter(pack)
    return {"words": sorted(prefilter.lexicon), "phrases": list(prefilter.exemplars)}


def remove_pack(pack_id: str) -> bool:
    """Take a custom pack's file out of the node's directory. True when a file was removed. The facts it wrote
    stay; the registry switches the pack off the next time it is seeded, and the renewed stamp stays behind so
    the same file put back before that seeding is not taken for the pack that was switched on."""
    if not is_custom_id(pack_id):
        _refuse(f"Only a pack you added can be removed: its id starts with `{NAMESPACE}.`.")
    path = custom_pack_dir() / f"{pack_id}.yaml"
    try:
        if not path.exists() and not path.is_symlink():
            return False
        _renew_stamp(pack_id)
        path.unlink()
    except OSError:
        _refuse("The pack file could not be removed.")
    return True


def describe(pack: Pack) -> List[str]:
    """What a pack would do, in lines the `packs add` prompt prints: its own declarations and the rules it runs
    under. Ids, counts and fixed words only."""
    names: Iterable[str] = sorted(pack.predicates)
    return [
        f"{pack.pack}  {pack.version}  {pack.title}",
        f"  reads: {ROLE_POLICY_WORDS[pack.role_policy]}",
        f"  sensitivity: {pack.sensitivity_class}",
        f"  writes {len(pack.predicates)} kind{'s' if len(pack.predicates) != 1 else ''} of fact, about you only: "
        + ", ".join(names),
    ]
