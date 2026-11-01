"""The derivation job's three paths (ingest batch, drip catch-up, the enable-offer trial) read
an owner-authored direct message with the recipient the record already carries, and the parser
handles two shapes the extraction model produces: a one-person field as a one-element array
(unwrapped, not rejected) and a diarisation label as a person (refused).

"I'll send it after lunch" is a promise to the person being texted, whose name is not in the
words; without the recipient the model has no counterparty to give, and a placeholder such as
`<recipient>` is rejected by the grounding guard. Other packs given the same label bind the
person being texted as the subject of facts that are not about them, so only the packs in
`template.RECIPIENT_LABEL_PACKS` read it.
"""
import json
import sqlite3

import pytest

from topos.enrichment.jobs.canonical.derivation_job import (
    _attach_dm_recipients, _iter_history, _recipient_parse_args, run_derivation_batch,
)
from topos.features.derivation.commitments import UNNAMED_RECIPIENT
from topos.storage.db.migrations import apply_all_migrations

PARTNER = "+15125550199"
PROMISE = "Sure, I'll send you the deck tonight after lunch"


@pytest.fixture
def node_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "node.db")
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner','person','Owner','owner','[]',1)")
    conn.execute("CREATE TABLE IF NOT EXISTS conversation_messages (message_id TEXT PRIMARY KEY,"
                 " conversation_id TEXT, content TEXT, event_at TEXT, actor_role TEXT, is_from_self INTEGER,"
                 " sender_id TEXT, sender_type TEXT, source_id TEXT, dataset_id TEXT)")
    rows = [
        # a direct message: one other sender
        ("p0", "dm1", "are you around later today?", 0, PARTNER),
        ("m1", "dm1", PROMISE, 1, "self"),
        # a group: two other senders
        ("g0", "grp", "who is bringing the slides?", 0, "+15550002222"),
        ("g1", "grp", "not me, I have the projector", 0, "+15550003333"),
        ("m2", "grp", PROMISE, 1, "self"),
        # a recording: one diarised voice is not someone the owner messaged
        ("v0", "vox", "so what is the plan for the deck", 0, "Speaker 1"),
        ("m3", "vox", PROMISE, 1, "self"),
    ]
    for mid, conv, text, is_self, sender in rows:
        conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,datetime('now'),?,?,?,'human','imessage','d')",
                     (mid, conv, text, "authored" if is_self else "observed", is_self, sender))
    conn.commit()
    return conn


def _only_commitments(conn, enabled=True, also=()):
    """Every other pack off, so the catch-up budget and the trial see this pack alone."""
    from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
    seed_pack_registry(conn, bundled_pack_dir())
    conn.execute("UPDATE pack_registry SET enabled=0")
    conn.execute("UPDATE pack_registry SET enabled=? WHERE pack_id='obligations.commitments'", (1 if enabled else 0,))
    for pack_id in also:
        conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id=?", (pack_id,))
    conn.commit()


def _pack(pack_id):
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    return load_packs(bundled_pack_dir(), only=[pack_id])[pack_id]


def _ingest_only(monkeypatch):
    from topos.enrichment.jobs.canonical import derivation_job
    monkeypatch.setattr(derivation_job, "_drip_catchup", lambda *a, **kw: None)
    monkeypatch.setattr(derivation_job, "_self_gate", lambda *a, **kw: None)


def _recording_llm(monkeypatch, extract_reply, social_reply='{"assertions": []}'):
    """Stub the model. Every extraction and verifier prompt is kept for inspection."""
    from topos.engine.backends import ollama
    seen = {"extract": [], "verify": []}
    verdict = json.dumps({"supported": True, "about": "owner", "fields_ok": True, "reason": "stated"})

    def fake(self, model, prompt, **kw):
        if "Respond with exactly" in prompt:
            return {"text": "ok"}
        if "strict fact-checker" in prompt:
            seen["verify"].append(prompt)
            return {"text": verdict}
        seen["extract"].append(prompt)
        return {"text": social_reply if "Lens: Relationships." in prompt else extract_reply}

    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", fake)
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c: "stub-model")
    return seen


def _promise(counterparty):
    return json.dumps({"assertions": [{
        "predicate": "commit.made",
        "value": {"counterparty": counterparty, "direction": "owed_by_owner",
                  "description": "send the deck", "status": "open"},
        "about": "owner", "occurrence_date": None, "confidence": 0.9, "quote": "I'll send you the deck"}]})


def _commitment_prompts(prompts, record_text):
    return [p for p in prompts if "Promises made and owed" in p and record_text in p]


def _row(mid, conv="dm1"):
    return {"message_id": mid, "conversation_id": conv, "content": PROMISE, "_table": "conversation_messages",
            "actor_role": "authored", "event_at": "2026-10-20", "source_id": "imessage"}


def test_only_an_owner_dm_gets_a_recipient_never_a_group_a_recording_or_their_message(node_db):
    recs = [{"table": "conversation_messages", "record_id": mid, "role": role}
            for mid, role in (("m1", "authored"), ("m2", "authored"), ("m3", "authored"), ("p0", "observed"))]
    recs.append({"table": "journal_entries", "record_id": "j1", "role": "authored"})
    _attach_dm_recipients(node_db, recs)
    by_id = {r["record_id"]: r for r in recs}
    assert by_id["m1"]["recipient_key"] == PARTNER and by_id["m1"]["recipient"] == UNNAMED_RECIPIENT
    assert by_id["m1"]["conversation_id"] == "dm1", "the stored conversation, read by message id"
    for rid in ("m2", "m3", "p0", "j1"):
        assert "recipient" not in by_id[rid], rid


def test_ingest_batch_prompt_parser_and_verifier_all_get_the_recipient(node_db, monkeypatch):
    """Asked for `key:+1512…`, the extraction model often writes the bare handle. It is
    accepted only when the runner's label reached the parser."""
    _only_commitments(node_db)
    _ingest_only(monkeypatch)
    seen = _recording_llm(monkeypatch, _promise(PARTNER))
    stats = {}
    run_derivation_batch(node_db, [_row("m1")], stats=stats)
    (prompt,) = _commitment_prompts(seen["extract"], PROMISE)
    assert f'exactly "key:{PARTNER}"' in prompt and f"sent to {UNNAMED_RECIPIENT}" in prompt
    assert stats.get("parse_rejects", 0) == 0 and stats.get("v_accepted") == 1
    (verify,) = [p for p in seen["verify"] if PROMISE in p]
    assert f"sent to {UNNAMED_RECIPIENT} = key:{PARTNER}" in verify
    assert f'"counterparty": "key:{PARTNER}"' in verify, "the bare handle became the runner's label"


def test_ingest_batch_group_message_is_read_as_today(node_db, monkeypatch):
    _only_commitments(node_db)
    _ingest_only(monkeypatch)
    seen = _recording_llm(monkeypatch, _promise(PARTNER))
    stats = {}
    run_derivation_batch(node_db, [_row("m2", "grp")], stats=stats)
    (prompt,) = _commitment_prompts(seen["extract"], PROMISE)
    assert "sent to" not in prompt and "key:" not in prompt
    assert stats.get("parse_rejects") == 1, "with no label the bare handle is still a name nobody wrote"
    assert not seen["verify"]


def test_history_rows_carry_the_recipient_and_drip_catchup_passes_it(node_db, monkeypatch):
    rows = {r["record_id"]: r for r in _iter_history(node_db)}
    assert rows["m1"]["recipient_key"] == PARTNER and rows["m1"]["conversation_id"] == "dm1"
    assert "recipient" not in rows["m2"] and "recipient" not in rows["m3"]

    _only_commitments(node_db)
    seen = _recording_llm(monkeypatch, _promise(PARTNER))
    run_derivation_batch(node_db, [], stats={})        # no new rows: only catch-up reads history
    prompts = _commitment_prompts(seen["extract"], PROMISE)
    labelled = [p for p in prompts if f'exactly "key:{PARTNER}"' in p]
    assert len(prompts) == 3 and len(labelled) == 1, "the DM, not the group or the recording"
    ledger = node_db.execute("SELECT record_id, vstatus, value_json FROM derivation_training_ledger"
                             " WHERE stage='catchup' AND pack_id='obligations.commitments'").fetchall()
    assert [(rid, json.loads(v)["counterparty"]) for rid, _s, v in ledger] == [("m1", f"key:{PARTNER}")]


def test_enable_offer_trial_reads_records_with_the_recipient(node_db, monkeypatch):
    """The trial's accept rate is what the owner is offered; it has to measure the prompt the
    pack will run with once enabled."""
    _only_commitments(node_db, enabled=False)
    node_db.execute("INSERT OR IGNORE INTO pack_yield (pack_id, day, prefilter_hits)"
                    " VALUES ('obligations.commitments', date('now'), 40)")
    for i in range(5):
        node_db.execute("INSERT INTO conversation_messages VALUES (?,?,?,datetime('now'),'authored',1,'self',"
                        "'human','imessage','d')", (f"t{i}", "dm1", f"{PROMISE} ({i})"))
    node_db.commit()
    seen = _recording_llm(monkeypatch, _promise(PARTNER))
    stats = {}
    run_derivation_batch(node_db, [], stats=stats)
    # sampled: the five new DM lines, the earlier DM promise, the group's and the recording's
    assert stats.get("trial_obligations.commitments") == {"sampled": 8, "assertions": 6, "accepted": 6}
    prompts = _commitment_prompts(seen["extract"], PROMISE)
    labelled = [p for p in prompts if f'exactly "key:{PARTNER}"' in p]
    assert len(prompts) == 8 and len(labelled) == 6


def test_the_stand_in_name_for_an_unnamed_partner_is_never_exempt_from_the_person_checks():
    commitments = _pack("obligations.commitments")
    args = _recipient_parse_args(commitments, {"recipient": UNNAMED_RECIPIENT, "recipient_key": PARTNER})
    assert args == {"runner_labels": [f"key:{PARTNER}"], "grounded_exempt": ()}
    named = {"recipient": "Priya Anand", "recipient_entity_id": "ent_priya", "recipient_key": PARTNER}
    assert _recipient_parse_args(commitments, named) == {
        "runner_labels": ["id:ent_priya", f"key:{PARTNER}"], "grounded_exempt": ("Priya Anand",)}
    assert _recipient_parse_args(commitments, {}) == {"runner_labels": [], "grounded_exempt": ()}
    assert _recipient_parse_args(_pack("relationships.social"), named) == {"runner_labels": [], "grounded_exempt": ()}


def test_a_pack_outside_the_label_set_reads_the_dm_as_today(node_db, monkeypatch):
    """Given the label, the social pack binds the person being texted as the subject of a
    relationship fact ("Thank you!!" read as reconnected). Same record, same batch: commitments
    reads the recipient; the social pack's prompt, parser and verifier do not."""
    _only_commitments(node_db, also=("relationships.social",))
    _ingest_only(monkeypatch)
    social = json.dumps({"assertions": [{
        "predicate": "rel.relationship", "value": {"person": "Mom", "role": "parent"},
        "about": "owner", "occurrence_date": None, "confidence": 0.9, "quote": "send you the deck"}]})
    seen = _recording_llm(monkeypatch, _promise(PARTNER), social_reply=social)
    run_derivation_batch(node_db, [_row("m1")], stats={})
    (theirs,) = [p for p in seen["extract"] if "Lens: Relationships." in p]
    (ours,) = _commitment_prompts(seen["extract"], PROMISE)
    assert "sent to" not in theirs and PARTNER not in theirs
    assert f'exactly "key:{PARTNER}"' in ours
    (social_verify,) = [p for p in seen["verify"] if "rel.relationship" in p]
    (promise_verify,) = [p for p in seen["verify"] if "commit.made" in p]
    assert "Addressing" not in social_verify and f"key:{PARTNER}" in promise_verify


def test_the_owners_backfill_control_scopes_the_label_the_same_way(node_db, monkeypatch):
    """`run_pack_backfill` walks `_iter_history`, which now carries the recipient; it reads
    the record through the same per-pack gate."""
    from topos.features.derivation.surfaces import run_pack_backfill
    _only_commitments(node_db, also=("relationships.social",))
    monkeypatch.setattr("topos.features.signal.derived_index.index_derived_objects", lambda *a, **kw: {})
    seen = _recording_llm(monkeypatch, _promise(PARTNER))
    run_pack_backfill(node_db, "relationships.social", use_prefilter=False)
    social = [p for p in seen["extract"] if PROMISE in p]
    assert social and not any("sent to" in p for p in social)
    seen["extract"].clear()
    run_pack_backfill(node_db, "obligations.commitments", use_prefilter=False)
    labelled = [p for p in _commitment_prompts(seen["extract"], PROMISE) if f'exactly "key:{PARTNER}"' in p]
    assert len(labelled) == 1, "the DM only"


# --- parser: a one-person array, and a diarisation label -----------------------------------------

def _joy():
    return _pack("moments.joy")


def _gratitude(person):
    return json.dumps({"assertions": [{
        "predicate": "moment.gratitude", "value": {"person": person, "what": "drove out", "acknowledged": True},
        "about": "owner", "occurrence_date": None, "confidence": 0.9, "quote": "thank you for driving out"}]})


THANKS = "Maren, thank you for driving out last night. It meant more than you know."


def test_a_one_person_field_written_as_a_one_element_array_is_the_name():
    from topos.features.derivation.template import parse_output
    pack = _joy()
    valid, rejects = parse_output(_gratitude(["Maren"]), pack, record_text=THANKS)
    assert rejects == 0 and valid[0]["value"]["person"] == "Maren"
    valid, rejects = parse_output(_gratitude(["NEW:Maren"]), pack, record_text=THANKS)
    assert rejects == 0 and valid[0]["value"]["person"] == "Maren" and valid[0]["new_person"]


def test_the_unwrapped_name_still_meets_every_person_check():
    from topos.features.derivation.template import parse_output
    pack = _joy()
    assert parse_output(_gratitude(["Tomas"]), pack, record_text=THANKS) == ([], 1), "ungrounded"
    assert parse_output(_gratitude(["him"]), pack, record_text=THANKS) == ([], 1), "a pronoun"
    assert parse_output(_gratitude(["Maren", "Tomas"]), pack, record_text=THANKS) == ([], 1), "two in a one-person field"
    assert parse_output(_gratitude([]), pack, record_text=THANKS) == ([], 1), "no one, on a person-keyed predicate"


def test_a_diarisation_label_is_never_a_person():
    from topos.features.derivation.template import is_diarisation_label, parse_output, person_field_ok
    for label in ("speaker 1", "Speaker 2", "SPEAKER_00", "spk_1", "Speaker A", "speaker#3", " Speaker  12 "):
        assert is_diarisation_label(label) and not person_field_ok(label), label
    for name in ("Speakerman", "Spencer 2", "speakers", "Speaker", "Maren", "Dr. Speaker"):
        assert not is_diarisation_label(name), name
    pack = _joy()
    transcript = "Speaker 2: thank you for driving out last night, it meant a lot"
    assert parse_output(_gratitude("Speaker 2"), pack, record_text=transcript) == ([], 1)
    peak = json.dumps({"assertions": [{
        "predicate": "moment.peak", "value": {"kind": "connected", "description": "the drive", "with": ["Speaker 2"]},
        "about": "owner", "occurrence_date": None, "confidence": 0.9, "quote": "it meant a lot"}]})
    assert parse_output(peak, pack, record_text=transcript) == ([], 1), "nor inside a list of people"
