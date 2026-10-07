"""Before a real node takes 1.5.0: the exclude carry, run for real on a stopped COPY of its home (review R3-M3).

The upgrade step that carries the older per-person "exclude" choices into Off-limits is not done until the share
boundary can be built over what it wrote, and a boundary that cannot be built turns every share on the node off.
The step's own dry run cannot see that: it writes nothing, so there is nothing for the boundary to read. This
script is the rehearsal that can. On a COPY of a home it

  1. brings the copy's canonical database to this build's schema with the node's own migration runner. A real
     start writes a backup of the database first and needs free disk of twice its size for it. This run writes
     no backup (the copy is what a backup would be), so it needs no disk beyond the copy, and it does not show
     whether the real home has the room;
  2. runs the real step (`contact_excludes.carry_contact_excludes`), which writes the entries into the copy;
  3. builds the share boundary over the copy (`entity_boundary.EntityBoundary`), as every share read does;
  4. where the boundary refuses, finds which entry and which kind of value (`carry_diagnosis.unreadable`);
  5. runs the step a second time, which must add nothing.

It prints one JSON object of counts and fixed words. Never a name, a handle, an id, a path or a row: an entry is
given by its position in the Off-limits list of the copy (1 is the first the list shows), a value by its kind.

THE COPY IS CHANGED. Use it for nothing else afterwards. Take the counts of the upgrade census
(upgrade_census_diff.py collect) from the copy BEFORE this, or from another copy.

It refuses, with `{"refused": "<one fixed word>"}` on standard error and exit 2:
  not_told_it_is_a_copy    `--this-is-a-copy` was not given. It is never inferred.
  live_store_refused       the path is `~/.topos` or under it (by path and by inode), or the environment's are,
                           or the copy's database is another name for a file under it (a hard link).
  database_is_a_hard_link  the copy's database has more than one name. A write to it is a write to the other
                           name, whatever that is. Make the copy with `cp -R`, never with links;
                           `stat -f %l <copy>/database.db` prints 1 for a real copy.
  node_socket_present, node_lock_held, node_lock_unreadable
                           a node's socket is in the folder, or its sharing lock or a rebuild lock is held: a node
                           may be running there.
  copy_not_closed          a database has a write-ahead log or a journal beside it: the node was running, or the
                           copy was made before SQLite folded its log in (see upgrade_census_diff.py for the cure).
  source_root_missing, canonical_database_missing, source_escapes_root, node_config_unreadable
                           the folder is not a copy of a home, or a store in it is a link to somewhere else.
  scratch_environment_required
                           TOPOS_DATABASE_PATH and TOPOS_ENV_FILE are not both set to scratch paths.
  copy_is_from_a_newer_build
                           the copy's schema is newer than this build: nothing was written.
These checks are the upgrade census's own (`upgrade_census_diff.refuse_live_home`) and the one the upgrade matrix
and the fixture builder make too (`census_support.refuse_a_real_database`).

Output (exit 0 when `verdict` is "ready", else 1):
  {"schema": "carry-preflight/v1",
   "verdict": "ready" | "boundary_refuses" | "step_failed",
   "schema_version": {"before": N, "after": N},
   "step": {"explicit_excludes": N, "carried": N, "added_to_existing": N, "already_off_limits": N,
            "carried_before": N, "own_card_skipped": N, "failed": N, "waiting": N,
            "named_by": {"linked_entity": N, "name": N, "handle": N, "contact_id_only": N}},
   "boundary": "built" | "<the refusal's code>",
   "reaches_contacts": N,                          only when built: how many contacts the boundary reaches
   "refusal": {"entries": [{"position": N, "kinds": ["contact_usernames", ...]}, ...], "of": N,
               "kinds": [...], "removing_them_is_enough": true | false},      only when it refuses
   "second_run": {"carried": N, "added_to_existing": N, "failed": N}}
`kinds` are `carry_diagnosis.KINDS`. `removing_them_is_enough` is the boundary's own answer over the list without
those entries; false means the value is one the boundary reads for every contact (a saved name that is not text,
an entity's names), and the row itself has to be mended before the upgrade.

Run (zsh; every flag its own token). The two variables are required because engine code is imported:
  export TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env
  PYTHONPATH=$PWD <engine venv>/bin/python3 scripts/permissions_v2/carry_preflight.py \\
      --copy-root <stopped copy of the home> --this-is-a-copy
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import census_support as cs  # noqa: E402
import upgrade_census_diff as census  # noqa: E402

SCHEMA = "carry-preflight/v1"
STEP_COUNTS = ("carried", "added_to_existing", "already_off_limits", "carried_before", "own_card_skipped", "failed",
               "waiting")


def _count(value) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _code(value, allowed=None) -> str:
    """A node code as a fixed word: letters, digits and underscores only, so no stored string can pass as one."""
    text = str(value)
    if allowed is not None:
        return text if text in allowed else "other"
    return text if text.replace("_", "").isalnum() and text.isascii() and len(text) <= 64 else "other"


def preflight(copy_root: Path, *, this_is_a_copy: bool) -> dict:
    """The rehearsal. Raises `census_support.CensusRefused` with a fixed word; returns counts and fixed words."""
    if this_is_a_copy is not True:
        raise cs.CensusRefused("not_told_it_is_a_copy")
    cs.require_scratch_environment()
    root = census.refuse_live_home(Path(copy_root))
    canonical = cs.refuse_a_real_database(census._inside(root, census._stores(root)["canonical"]), closed=True)

    from topos.features.lifecycle import carry_diagnosis, contact_excludes
    from topos.permissions_v2.entity_boundary import EntityBoundary
    from topos.storage.db.migrations import DowngradeGuardError, ensure_migrations_applied, read_user_version

    conn = sqlite3.connect(str(canonical))
    try:
        before = read_user_version(conn)
        try:
            ensure_migrations_applied(conn, skip_backup=True)
        except DowngradeGuardError:
            raise cs.CensusRefused("copy_is_from_a_newer_build") from None
        out = contact_excludes.carry_contact_excludes(conn)
        boundary = contact_excludes._boundary_state(conn)
        report = {
            "schema": SCHEMA,
            "schema_version": {"before": _count(before), "after": _count(read_user_version(conn))},
            "step": {"explicit_excludes": _count(out["counts"].get("explicit_excludes")),
                     **{key: _count(out.get(key)) for key in STEP_COUNTS},
                     "named_by": {branch: _count(out["named_by"].get(branch)) for branch in census.NAMING_BRANCHES}},
            "boundary": _code(boundary),
        }
        if boundary == "built":
            report["reaches_contacts"] = len(EntityBoundary(conn).contacts)
        else:
            found = carry_diagnosis.unreadable(conn)
            report["refusal"] = {
                "entries": [{"position": _count(entry["position"]),
                             "kinds": [_code(kind, carry_diagnosis.KINDS) for kind in entry["kinds"]]}
                            for entry in found["entries"]],
                "of": _count(found["of"]),
                "kinds": [_code(kind, carry_diagnosis.KINDS) for kind in found["kinds"]],
                "removing_them_is_enough": found["enough"] is True,
            }
        again = contact_excludes.carry_contact_excludes(conn)
        report["second_run"] = {key: _count(again.get(key)) for key in ("carried", "added_to_existing", "failed")}
        report["verdict"] = ("step_failed" if report["step"]["failed"] or report["second_run"]["failed"]
                             else "ready" if boundary == "built" else "boundary_refuses")
        conn.commit()
        return report
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--copy-root", required=True, type=Path,
                        help="a STOPPED COPY of a node home: the folder that holds database.db and permissions-v2/")
    parser.add_argument("--this-is-a-copy", action="store_true",
                        help="say so: the step is run for real and the copy is changed")
    args = parser.parse_args(argv)
    try:
        report = preflight(args.copy_root, this_is_a_copy=args.this_is_a_copy)
    except cs.CensusRefused as refused:
        print(json.dumps({"refused": _code(refused.args[0] if refused.args else "refused")}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["verdict"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
