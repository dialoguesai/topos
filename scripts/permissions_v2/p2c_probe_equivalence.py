"""RD12: the native form census changes no decision. The probe at a base commit and now, on the same inputs.

The census adds count-only buckets to `probe_native_messages`: a first-failing-field split of
`native_message_form_unsupported`, and edit and retraction observations. This script loads the
module exactly as it was at `--base` (from git) beside the current one. It runs both over a
matrix of synthetic native and canonical databases and requires, for every case:
- the same refusal code, or the same success;
- the same rows handed to the capture (`_on_match`), field for field, which also means no
  observed column reaches a capture;
- the same value for every count the base emitted, with nothing new except `native_form_*` and
  `native_observed_*`;
- `native_form_*` summing to `native_message_form_unsupported`.

Synthetic only; nothing under the owner's home is read. Run from the engine root:
    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/p2c_probe_equivalence.py --base 8d64d5c1
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
MODULE = "topos/permissions_v2/native_imessage_probe.py"
NOW = datetime(2023, 3, 9, tzinfo=timezone.utc)
ARGS = dict(dataset_id="dataset-native", owner_id="owner-synthetic",
            starts_at="2023-03-01T00:00:00.000000+00:00", ends_at=NOW.isoformat(timespec="microseconds"), now=NOW)
OPTIONAL_TEXT = ("thread_originator_guid", "thread_originator_part", "quoted_message_guid", "forwarded_from", "reply_to_guid")
OPTIONAL_INT = ("is_deleted", "is_system_message", "is_service_message", "group_action_type", "is_forward",
                "is_forwarded", "is_spam", "date_edited", "date_retracted")
SINGLE = [("is_deleted", 1), ("is_spam", 1), ("is_system_message", 1), ("is_service_message", 1),
          ("group_action_type", 1), ("item_type", 1), ("associated_message_type", 2000),
          ("associated_message_guid", "p:0/synthetic"), ("is_forward", 1), ("is_forwarded", 1),
          ("forwarded_from", "synthetic"), ("quoted_message_guid", "synthetic"), ("thread_originator_guid", "synthetic"),
          ("thread_originator_part", "0:0:10"), ("reply_to_guid", "synthetic"), ("subject", "Synthetic subject"),
          ("cache_has_attachments", 1), ("date_edited", 1), ("date_retracted", 1), ("item_type", None),
          ("associated_message_type", None), ("is_spam", "1"), ("thread_originator_part", 0)]


def base_module(commit: str):
    source = subprocess.run(["git", "show", f"{commit}:{MODULE}"], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    module = types.ModuleType("topos.permissions_v2._probe_at_base")
    module.__package__ = "topos.permissions_v2"
    module.__file__ = f"<{commit}:{MODULE}>"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def cases():
    """(name, rows, full_columns, per-row native updates, canonical content overrides, probe overrides)."""
    from tests.fixtures.imessage.attributed_body_blobs import ATTRIBUTED_BODY_FIXTURES as blobs
    out = [("plain", 3, True, {}, {}), ("plain_old_schema", 3, False, {}, {})]
    for column, value in SINGLE:
        out.append((f"single_{column}_{value!r}", 3, True, {1: {column: value}}, {}))
    attachment = lambda text, body: {"cache_has_attachments": 1, "text": text, "attributedBody": body}
    out += [
        ("attachment_placeholder_only", 2, True, {1: attachment("￼", None)}, {}),
        ("attachment_caption_text", 2, True, {1: attachment("￼ look", None)}, {}),
        ("attachment_archive_only", 2, True, {1: attachment(None, blobs["typedstream_attachment"][0])}, {}),
        ("attachment_archive_caption", 2, True, {1: attachment(None, blobs["typedstream_mixed"][0])}, {}),
        ("attachment_archive_garbage", 2, True, {1: attachment(None, b"\x01\x02")}, {}),
        ("attachment_no_body", 2, True, {1: attachment(None, None)}, {}),
        ("archived_plain_text", 2, True, {1: {"text": None, "attributedBody": blobs["typedstream_plain"][0]}},
         {1: blobs["typedstream_plain"][1]}),
        ("archive_unsupported", 2, True, {1: {"text": None, "attributedBody": b"\x01\x02"}}, {}),
        ("representations_disagree", 2, True, {1: {"attributedBody": blobs["typedstream_plain"][0]}}, {}),
        ("empty_text", 2, True, {1: {"text": "   "}}, {}),
        ("body_mismatch", 2, True, {1: {"text": "changed natively"}}, {}),
        ("edited_body_mismatch", 2, True, {1: {"text": "changed natively", "date_edited": 5}}, {}),
        ("edited_match", 2, True, {1: {"date_edited": 5}, 2: {"date_retracted": 7}}, {}),
        ("time_mismatch", 2, True, {1: {"date": "date+1000000000"}}, {}),
        ("not_owner_sent", 3, True, {2: {"is_from_me": 0}}, {}),
        ("mixed_eight", 8, True, {1: {"date_edited": 1}, 2: {"subject": "s"}, 3: attachment(None, blobs["typedstream_mixed"][0]),
                                  4: {"associated_message_type": 3001}, 5: {"thread_originator_guid": "x", "cache_has_attachments": 1},
                                  6: {"text": "changed", "date_retracted": 1}, 7: {"is_spam": 1, "item_type": 3}}, {}),
        ("over_the_record_limit", 1001, True, {}, {}),
    ]
    garbage = b"\x01" * 65600  # counts toward the 4 MiB archive limit, then fails to decode
    out += [
        # 63 archives stay under the limit; a census body counted toward it would push it over.
        ("archive_limit_not_reached_by_census", 64, True,
         {1: attachment(None, garbage), **{row: {"text": None, "attributedBody": garbage} for row in range(2, 65)}}, {}),
        ("archive_limit", 64, True, {row: {"text": None, "attributedBody": garbage} for row in range(1, 65)}, {}),
        ("text_limit", 17, True, {row: {"text": "x" * 64000} for row in range(1, 18)}, {}),
        ("window_invalid", 2, True, {}, {}, {"starts_at": ARGS["ends_at"], "ends_at": ARGS["starts_at"]}),
        ("binding_invalid", 2, True, {}, {}, {"dataset_id": ""}),
        ("native_unavailable", 2, True, {}, {}, {"_native_path": "missing.db"}),
    ]
    return [case if len(case) == 6 else (*case, {}) for case in out]


def build(directory: Path, rows: int, full: bool, updates: dict, contents: dict):
    from tests.permissions_v2.test_imessage_reconciliation import snapshot
    from topos.permissions_v2.imessage_reconciliation import parse_reconciliation_snapshot

    def adapt(db):
        if full:
            for column in OPTIONAL_TEXT:
                db.execute(f'ALTER TABLE message ADD COLUMN "{column}" TEXT')
            for column in OPTIONAL_INT:
                db.execute(f'ALTER TABLE message ADD COLUMN "{column}" INTEGER DEFAULT 0')
        db.execute("UPDATE message SET is_from_me=1")
    clean = snapshot(count=rows, mutate=adapt)
    native = directory / "native.db"
    native.write_bytes(clean)
    canonical = directory / "canonical.db"
    records = parse_reconciliation_snapshot(clean, now=NOW) if rows <= 1000 else ()
    with sqlite3.connect(canonical) as db:
        db.execute("CREATE TABLE conversation_messages (message_id TEXT, source_record_id TEXT, source_id TEXT, dataset_id TEXT,"
                   " owner_user_id TEXT, conversation_id TEXT, content TEXT, event_at TEXT, is_from_self INTEGER, sender_id TEXT,"
                   " sender_type TEXT, actor_role TEXT, message_type TEXT, metadata_json TEXT)")
        for index, record in enumerate(records, 1):
            db.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                record.message_id, record.message_id, "imessage", "dataset-native", None, record.conversation_id,
                contents.get(index, record.content), record.event_at, 1, "self", "human", None, "message",
                json.dumps({"message_guid": record.message_guid, "chat_guid": record.chat_guid,
                            "chat_identifier": record.chat_identifier, "associated_message_type": 0})))
    with sqlite3.connect(native) as db:
        for rowid, changes in updates.items():
            for column, value in changes.items():
                if column == "date" and isinstance(value, str):
                    db.execute(f"UPDATE message SET date={value} WHERE ROWID=?", (rowid,))
                else:
                    db.execute(f'UPDATE message SET "{column}"=? WHERE ROWID=?', (value, rowid))
    return native, canonical


def observe(module, native: Path, canonical: Path, overrides: dict):
    from topos.permissions_v2.canonical import PolicyError
    matched = []
    kwargs = ARGS | {"_native_path": native} | overrides
    if "_native_path" in overrides:
        kwargs["_native_path"] = native.parent / overrides["_native_path"]
    conn = sqlite3.connect(canonical.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    try:
        result = module.probe_native_messages(conn, **kwargs,
                                              _on_match=lambda row, chat: matched.append((sorted(row.items()), tuple(chat))))
        return {"refusal": None, "crash": None, "counts": result["counts"], "matched": matched}
    except PolicyError as exc:
        return {"refusal": exc.code, "crash": None, "counts": None, "matched": matched}
    except Exception as exc:  # noqa: BLE001 -- two identical crashes are not an equivalence; reported as a difference
        return {"refusal": None, "crash": type(exc).__name__, "counts": None, "matched": matched}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="8d64d5c1")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    from topos.permissions_v2 import native_imessage_probe as current
    base = base_module(args.base)
    report = {"base": args.base, "cases": 0, "outcomes": {}, "differences": [], "new_keys_seen": {}}
    for name, rows, full, updates, contents, overrides in cases():
        with tempfile.TemporaryDirectory(prefix="p2c-probe-equivalence-") as scratch:
            native, canonical = build(Path(scratch), rows, full, updates, contents)
            before, after = observe(base, native, canonical, overrides), observe(current, native, canonical, overrides)
        report["cases"] += 1
        report["outcomes"][name] = (f"refused:{after['refusal']}" if after["refusal"] else
                                    f"crashed:{after['crash']}" if after["crash"] else f"matched:{len(after['matched'])}")
        problems = []
        if before["crash"] or after["crash"]:
            problems.append(f"crash {before['crash']} / {after['crash']}")
        if before["refusal"] != after["refusal"]:
            problems.append(f"refusal {before['refusal']} -> {after['refusal']}")
        if before["matched"] != after["matched"]:
            problems.append("captured rows differ")
        if before["counts"] is not None and after["counts"] is not None:
            new = {k: v for k, v in after["counts"].items() if k not in before["counts"]}
            if any(not k.startswith(("native_form_", "native_observed_")) for k in new):
                problems.append("unexpected new count keys")
            if {k: after["counts"].get(k) for k in before["counts"]} != before["counts"]:
                problems.append("an existing count changed")
            forms = sum(v for k, v in new.items() if k.startswith("native_form_"))
            if forms != after["counts"].get("native_message_form_unsupported", 0):
                problems.append("form buckets do not sum to native_message_form_unsupported")
            for key, value in new.items():
                report["new_keys_seen"][key] = report["new_keys_seen"].get(key, 0) + value
        if problems:
            report["differences"].append({"case": name, "problems": problems})
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    return 0 if not report["differences"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
