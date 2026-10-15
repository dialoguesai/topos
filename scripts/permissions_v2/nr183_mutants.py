"""Mutation run over NR183: O1, the owner lane of the control-plane client's inbound slots, and R10, the home chat's
schema step that takes the write gate only when something is missing. Every mutant weakens one decision and must be
killed by at least one real test.

O1, whose frame it is: whether the stamp is verified at all, whether a recipient door's type is excluded and its
spelling folded, which classes are named, whether the dispatcher's two naming rules are read, what an error means,
and whether the client asks the classifier at all. The slots: whether recipients can take a kept slot, whether an
owner frame takes a shared slot first, who goes first when a slot frees, whether the reserve leaves recipients a
slot, whether a cancelled frame holds a slot, whether the waiting places are kept, whether the slot comes back on an
exception, and whether R3a's check still runs, at the moment a slot is held. The switch: off unless set, and never
read while off. R10: whether the schema step skips the gate at all, whether it remembers instead of reading, and
whether its check reads the indexes, the added column, and what an error means.

Two decisions have no mutant of their own because no mutant of them can be told apart: the verified class check
after the claimed-class check (a verified stamp's class is the class it claims, both read one set), and the acquire
fast path's look at the queue (a slot is never free while a frame that may take it waits).

The runner is `n3c_mutants.py`'s (a scratch copy of the engine, one mutant at a time, the worktree never modified;
a mutant whose text does not match exactly once is a failure, not a pass; a hung run is HUNG, never killed).

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/nr183_mutants.py --out nr183-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import n3c_mutants as runner  # noqa: E402

LANE = "topos/permissions_v2/owner_lane.py"
CLIENT = "topos/control_plane_client.py"
SWITCHES = "topos/permissions_v2/switches.py"
SCHEMA = "topos/home_chat/schema.py"
LANE_TESTS = ["tests/topos/test_nr183_owner_lane.py"]
SCHEMA_TESTS = ["tests/topos/test_nr183_home_chat_schema_gate.py"]
TESTS = LANE_TESTS + SCHEMA_TESTS

IN_LANE = ("        slot = await lane.acquire(owner)\n        try:\n"
           "            if arrived is not None and await self._shed_at_dequeue(ws, data, *arrived):\n"
           "                return\n            await self._handle_message(ws, data)\n"
           "        finally:\n            lane.release(slot)\n")
SCHEMA_SKIP = "        if schema_in_place(conn):\n            return\n"

MUTANTS = [
    # -- whose frame it is ------------------------------------------------------------------------------------------
    ("stamp_not_verified_the_claimed_class_trusted", [
        (LANE, "        principal, reason, _offset = check_relay_stamp(message)\n",
               "        from ..principal import Principal\n"
               "        principal, reason, _offset = Principal(cls=str(stamp.get('cls')), channel='cp_relay',"
               " acting_user=str(stamp.get('acting_user') or '')), VERIFIED, None\n")], LANE_TESTS),
    ("recipient_doors_not_excluded", [
        (LANE, '.strip().lower() in NON_OWNER_RELAY_TYPES:', '.strip().lower() in ():')], LANE_TESTS),
    ("recipient_door_type_not_folded", [
        (LANE, 'if str(message.get("type") or "").strip().lower() in NON_OWNER_RELAY_TYPES:',
               'if str(message.get("type") or "") in NON_OWNER_RELAY_TYPES:')], LANE_TESTS),
    ("every_owner_side_class_named", [
        (LANE, "OWNER_LANE_CLASSES = frozenset({OWNER_APP})\n",
               "OWNER_LANE_CLASSES = frozenset({OWNER_APP, 'owner_automation', 'owner_agent'})\n")], LANE_TESTS),
    ("an_unknown_class_named", [
        (LANE, "OWNER_LANE_CLASSES = frozenset({OWNER_APP})\n",
               "OWNER_LANE_CLASSES = frozenset({OWNER_APP, 'owner_agent'})\n")], LANE_TESTS),
    ("naming_rule_a_not_read", [
        (LANE, "        return _non_owner_relay_refusal(message, principal) is None\n", "        return True\n")],
     LANE_TESTS),
    ("naming_rule_b_not_read", [
        (LANE, "        if not principal.acting_user and _unstamped_naming_refusal(message) is not None:\n",
               "        if False:\n")], LANE_TESTS),
    ("an_error_is_the_owners", [
        (LANE, "    except Exception:  # noqa: BLE001 -- what cannot be read is the shared class\n        return False\n",
               "    except Exception:  # noqa: BLE001 -- what cannot be read is the shared class\n        return True\n")],
     LANE_TESTS),
    ("anything_truthy_is_the_owners", [
        (LANE, "            return is_owner_frame(message) is True\n", "            return bool(is_owner_frame(message))\n")],
     LANE_TESTS),
    ("the_client_ignores_the_class", [
        (CLIENT, "        owner = lane is not None and lane.owner_frame(data)\n", "        owner = lane is not None\n")],
     LANE_TESTS),
    # -- the slots ---------------------------------------------------------------------------------------------------
    ("recipients_take_kept_slots", [
        (LANE, "        if owner is True and self._used[RESERVED] < self.reserved:\n",
               "        if self._used[RESERVED] < self.reserved:\n")], LANE_TESTS),
    ("owner_takes_a_kept_slot_before_a_shared_one", [
        (LANE, "        if self._used[GENERAL] < self.general:\n            return GENERAL\n"
               "        if owner is True and self._used[RESERVED] < self.reserved:\n            return RESERVED\n",
               "        if owner is True and self._used[RESERVED] < self.reserved:\n            return RESERVED\n"
               "        if self._used[GENERAL] < self.general:\n            return GENERAL\n")], LANE_TESTS),
    ("no_owner_first_when_a_slot_frees", [
        (LANE, "        for owner in (True, False):   # D3: a waiting owner frame first\n",
               "        for owner in (False, True):   # D3: a waiting owner frame first\n")], LANE_TESTS),
    ("the_reserve_may_take_every_slot", [
        (LANE, "        self.reserved = max(0, min(int(reserved), self.limit - 1))\n",
               "        self.reserved = max(0, int(reserved))\n")], LANE_TESTS),
    ("a_frame_cancelled_while_waiting_is_handed_a_slot", [
        (LANE, "                with contextlib.suppress(ValueError):\n                    queue.remove(future)\n",
               "                pass\n"),
        (LANE, "                if queue[0].done():   # cancelled while it waited\n", "                if False:\n")],
     LANE_TESTS),
    ("a_slot_handed_then_cancelled_is_kept", [
        (LANE, "                self.release(future.result())   # handed a slot, then cancelled before it could run\n",
               "                pass\n")], LANE_TESTS),
    ("the_owners_waiting_places_not_kept", [
        (LANE, "        return int(max_pending) if owner is True else max(1, int(max_pending) - self.reserved)\n",
               "        return int(max_pending)\n")], LANE_TESTS),
    ("slot_not_given_back_on_an_exception", [
        (CLIENT, IN_LANE,
         "        slot = await lane.acquire(owner)\n"
         "        if arrived is not None and await self._shed_at_dequeue(ws, data, *arrived):\n"
         "            lane.release(slot)\n            return\n        await self._handle_message(ws, data)\n"
         "        lane.release(slot)\n")], LANE_TESTS),
    ("shed_check_not_run_in_the_lane", [
        (CLIENT, IN_LANE,
         "        slot = await lane.acquire(owner)\n        try:\n            await self._handle_message(ws, data)\n"
         "        finally:\n            lane.release(slot)\n")], LANE_TESTS),
    ("shed_check_run_before_the_slot_is_held", [
        (CLIENT, IN_LANE,
         "        if arrived is not None and await self._shed_at_dequeue(ws, data, *arrived):\n            return\n"
         "        slot = await lane.acquire(owner)\n        try:\n            await self._handle_message(ws, data)\n"
         "        finally:\n            lane.release(slot)\n")], LANE_TESTS),
    # -- the switch --------------------------------------------------------------------------------------------------
    ("the_lane_on_by_default", [
        (SWITCHES, '    "OWNER_LANE", "bool", False, False,\n', '    "OWNER_LANE", "bool", True, True,\n')],
     LANE_TESTS + ["tests/permissions_v2/test_switches.py"]),
    ("the_classifier_read_while_off", [
        (CLIENT, "        owner = lane is not None and lane.owner_frame(data)\n",
                 "        owner = __import__('topos.permissions_v2.owner_lane', fromlist=['InboundSlots'])"
                 ".InboundSlots.owner_frame(data) and lane is not None\n")], LANE_TESTS),
    # -- R10: the home chat's schema step ----------------------------------------------------------------------------
    ("schema_step_takes_the_gate_every_call", [(SCHEMA, SCHEMA_SKIP, "        if False:\n            return\n")],
     SCHEMA_TESTS),
    ("schema_remembered_by_path_not_read", [
        (SCHEMA, 'logger = logging.getLogger("topos.home_chat.schema")\n',
                 'logger = logging.getLogger("topos.home_chat.schema")\n_DONE: set = set()\n'),
        (SCHEMA, SCHEMA_SKIP,
         "        _path = conn.execute('PRAGMA database_list').fetchone()[2]\n"
         "        if _path in _DONE:\n            return\n"),
        (SCHEMA, "            commit_connection(conn)\n", "            commit_connection(conn)\n            _DONE.add(_path)\n")],
     SCHEMA_TESTS),
    ("schema_check_reads_the_tables_only", [
        (SCHEMA, "        if not SCHEMA_OBJECTS <= {",
                 "        if not {item for item in SCHEMA_OBJECTS if item[0] == 'table'} <= {")], SCHEMA_TESTS),
    ("schema_check_skips_the_added_column", [
        (SCHEMA, '        return "participants_json" in columns\n', "        return True\n")], SCHEMA_TESTS),
    ("schema_check_error_reads_as_in_place", [
        (SCHEMA, '    except Exception:  # noqa: BLE001 -- unknown is "not in place": the gated path decides\n'
                 '        return False\n',
                 '    except Exception:  # noqa: BLE001 -- unknown is "not in place": the gated path decides\n'
                 '        return True\n')], SCHEMA_TESTS),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
