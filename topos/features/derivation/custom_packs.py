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
  changed is switched off again (`registry.seed_pack_registry`, `registry.runs_as_switched_on`).
* A file that does not validate is refused by `install_pack` with one plain sentence. At run time
  it is skipped and said once in the log, and the bundled packs keep working: one bad file in the
  owner's data directory must not take the fact layer down.

Nothing here is reachable by an app. There is no route, relay message or tool that adds a pack:
the only writer of that directory is the `packs add` command, which asks a person on a terminal.
"""

from __future__ import annotations

import logging
import os
import re
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

#: `custom.gardening`, `custom.garden.beds`. Also a pack id `topos-node packs backfill --pack` takes.
_PACK_ID_RE = re.compile(r"^custom(\.[a-z][a-z0-9_]{0,39}){1,3}$")
_PREDICATE_RE = re.compile(r"^custom(\.[a-z][a-z0-9_]{0,39}){1,4}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
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
        if spec["scale"] not in scales:
            _refuse(f"{where} names a scale Topos does not ship. Use one of: {', '.join(sorted(scales))}.")
        return
    _refuse(f"{where} must be a type name, a list of allowed values, {{enum: [...]}} or {{scale: name}}.")


def _check_predicate(pred: Any, seen: set, scales: Dict[str, List[str]]) -> None:
    if not isinstance(pred, dict) or not isinstance(pred.get("name"), str):
        _refuse("Each entry under `predicates` needs a `name`.")
    name = pred["name"]
    if not _PREDICATE_RE.match(name):
        _refuse(f"The predicate name {name!r} must start with `{NAMESPACE}.` and use lowercase letters, "
                "digits and underscores, like custom.plant.")
    if name in seen:
        _refuse(f"The predicate {name} is declared twice.")
    seen.add(name)
    if pred.get("cardinality") not in CARDINALITIES:
        _refuse(f"{name}: `cardinality` must be one of: {', '.join(CARDINALITIES)}.")
    if pred.get("temporal") not in TEMPORALS:
        _refuse(f"{name}: `temporal` must be one of: {', '.join(TEMPORALS)}.")
    if pred.get("altitude") != "stated":
        allowed = ", ".join(a for a in ALTITUDES if a != "stated")
        _refuse(f"{name}: `altitude` must be stated. A pack you add reads what a record says; "
                f"{allowed} facts are not supported in one yet.")
    if pred.get("sensitivity") is not None and pred.get("sensitivity") not in SENSITIVITY:
        _refuse(f"{name}: `sensitivity` must be one of: {', '.join(SENSITIVITY)}.")
    value_type = pred.get("value_type")
    if value_type not in _VALUE_TYPES:
        _refuse(f"{name}: `value_type` must be structured, enum or one of: {', '.join(sorted(KNOWN_VALUE_TYPES))}.")
    schema, values = pred.get("value_schema"), pred.get("values")
    if value_type == "structured":
        if not isinstance(schema, dict) or not 1 <= len(schema) <= MAX_FIELDS:
            _refuse(f"{name}: a structured predicate needs `value_schema` with 1 to {MAX_FIELDS} fields.")
        for field, spec in schema.items():
            if not isinstance(field, str) or not _FIELD_RE.match(field):
                _refuse(f"{name}: the field name {field!r} must use lowercase letters, digits and underscores.")
            _check_field(spec, f"{name}.{field}", scales)
    elif schema is not None:
        _refuse(f"{name}: `value_schema` goes with value_type: structured.")
    if value_type == "enum":
        _check_enum(values, f"{name}: `values`")
    elif values is not None:
        _refuse(f"{name}: `values` goes with value_type: enum.")


def check_definition(raw: Any, *, scales: Optional[Dict[str, List[str]]] = None) -> None:
    """The rules a custom pack is held to beyond the engine's own loader, each refusal one plain sentence.
    Raises :class:`CustomPackError`."""
    if not isinstance(raw, dict):
        _refuse("A pack file is a YAML mapping: `pack: custom.name`, `version: ...`, `predicates: ...`.")
    pid = raw.get("pack")
    if not is_custom_id(pid):
        _refuse(f"`pack` must be an id that starts with `{NAMESPACE}.`, like custom.gardening "
                "(lowercase letters, digits and underscores). That keeps it apart from the packs Topos ships.")
    if not isinstance(raw.get("version"), str) or not _VERSION_RE.match(raw["version"]):
        _refuse('`version` must be text like "1.0.0". Put it in quotes if it looks like a number.')
    if not _text(raw.get("title"), 1, 120):
        _refuse("`title` must be a short name for the pack (1 to 120 characters).")
    if raw.get("sensitivity_class") not in SENSITIVITY:
        _refuse(f"`sensitivity_class` must be one of: {', '.join(SENSITIVITY)}.")
    if raw.get("role_policy") not in ROLE_POLICIES:
        _refuse(f"`role_policy` must be one of: {', '.join(ROLE_POLICIES)}.")
    if str(raw.get("net_subject") or "deny").strip().lower() != "deny":
        _refuse("A pack you add can only describe you. Remove `net_subject: allow`.")
    if raw.get("disclosure_default") not in (None, "owner_only"):
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
    for pred in predicates:
        _check_predicate(pred, seen, scales)
    evaluation = raw.get("eval")
    for kind in ("gold", "negative_controls"):
        cases = evaluation.get(kind) if isinstance(evaluation, dict) else None
        if (not isinstance(cases, list) or not cases
                or not all(isinstance(case, dict) and _text(case.get("text"), 1, 4000) for case in cases)):
            _refuse(f"`eval.{kind}` must list at least one example, each with a `text`.")
    consumers = raw.get("consumers")
    if not isinstance(consumers, list) or not consumers or not all(_text(c, 1, 60) for c in consumers):
        _refuse("`consumers` must list at least one name, for example [assistant_context].")
    for key in ("routing", "guidance"):
        if raw.get(key) is not None and not isinstance(raw[key], dict):
            _refuse(f"`{key}` must be a mapping.")


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
    check_definition(raw)
    return raw


def _load_checked(path: Path) -> Pack:
    """One custom pack file through both checks: this module's and the engine loader's. The loader's own refusal
    (`pack <id>: ...`) is passed on as it reads."""
    content = path.read_bytes()
    parse_definition(content)
    try:
        pack = load_pack(path, trusted=False)
    except CustomPackError:
        raise
    except PackValidationError as exc:
        raise CustomPackError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- a shape the loader did not expect is still a refusal
        raise CustomPackError("The file is not a pack Topos can read.") from exc
    if pack.first_party or not is_custom_id(pack.pack):
        _refuse("A pack you add cannot be one Topos ships.")
    return pack


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
            problems[path.name] = str(exc)
            continue
        except OSError:
            problems[path.name] = "The file could not be read."
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


def read_candidate(source: Path) -> Tuple[Pack, bytes]:
    """The pack a file would add, fully checked, and the exact bytes that were checked. Writes nothing in the
    node's directory. Raises :class:`CustomPackError`."""
    source = Path(source)
    try:
        if not source.is_file():
            _refuse("That is not a file.")
        if source.stat().st_size > MAX_BYTES:
            _refuse(f"The file is larger than {MAX_BYTES // 1000} KB.")
        content = source.read_bytes()
    except OSError:
        _refuse("The file could not be read.")
    raw = parse_definition(content)
    # The loader reads a path, and decides first-party-ness from it: check a private copy of the bytes, so the
    # answer is about what will be written and not about where the owner kept the file.
    with tempfile.TemporaryDirectory(prefix="topos-pack-check-") as scratch:
        copy = Path(scratch) / f"{raw['pack']}.yaml"
        copy.write_bytes(content)
        pack = _load_checked(copy)
    ids, names = _bundled_names()
    if pack.pack in ids or set(pack.predicates) & names:
        _refuse("That pack uses a name Topos ships. Give it and its predicates names of their own.")
    return pack, content


def _others(pack_id: str) -> Dict[str, Pack]:
    return {pid: pack for pid, pack in _scan()[0].items() if pid != pack_id}


def install_pack(source: Path) -> Dict[str, Any]:
    """Check a pack file and put it in the node's directory, switched off. Returns its id, version and title, the
    version it replaced (None for a new pack) and whether it was already there unchanged.

    All or nothing: the bytes that were checked are written beside the destination and moved over it in one
    step, so a refusal leaves the directory as it was. Raises :class:`CustomPackError`."""
    pack, content = read_candidate(Path(source))
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
    replaced: Optional[str] = None
    if _is_plain_file(destination):
        try:
            old = _load_checked(destination)
        except (CustomPackError, OSError):
            old = None                              # an unreadable file there is simply replaced
        if old is not None:
            if definition_sha(old) == definition_sha(pack):
                return {"pack_id": pack.pack, "version": pack.version, "title": pack.title,
                        "replaced": old.version, "unchanged": True, "path": str(destination)}
            if old.version == pack.version:
                _refuse(f"{pack.pack} {pack.version} is already added and this file differs from it. "
                        "Give the changed pack a new `version`.")
            replaced = old.version
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".adding-", suffix=".tmp", dir=directory)
        staged = Path(name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
            os.replace(staged, destination)
        finally:
            staged.unlink(missing_ok=True)
    except OSError:
        _refuse("The pack could not be written to this node's pack directory.")
    return {"pack_id": pack.pack, "version": pack.version, "title": pack.title,
            "replaced": replaced, "unchanged": False, "path": str(destination)}


def remove_pack(pack_id: str) -> bool:
    """Take a custom pack's file out of the node's directory. True when a file was removed. The facts it wrote
    stay; the registry switches the pack off the next time it is seeded."""
    if not is_custom_id(pack_id):
        _refuse(f"Only a pack you added can be removed: its id starts with `{NAMESPACE}.`.")
    path = custom_pack_dir() / f"{pack_id}.yaml"
    try:
        if not path.exists() and not path.is_symlink():
            return False
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
