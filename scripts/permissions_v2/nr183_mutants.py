"""Mutation run over NR183: O1, the owner lane of the control-plane client's inbound slots, and R10, the home chat's
schema step that takes the write gate only when something is missing. Every mutant weakens one decision and must be
killed by at least one real test.

O1, whose frame it is: whether the stamp is verified at all, whether a recipient door's type is excluded and its
spelling folded, which classes are named, whether the dispatcher's two naming rules are read, what an error means,
and whether the client asks the classifier at all. The slots: whether recipients can take a kept slot, whether an
owner frame takes a shared slot first, who goes first when a slot frees, whether the reserve leaves recipients a
slot, whether a cancelled frame holds a slot, whether the waiting places are kept, whether the slot comes back on an
exception, and whether R3a's check still runs, at the moment a slot is held. After WS0's review and DR-04: the owner's kept slots
first, borrowing capped (and the cap clamped and read), each class counting its own waiting places (and counting a
frame out however it ends), only interactive owner types in the lane (the list folded and read). The switch: off
unless set, and never read while off. R10: whether the schema step skips the gate at all, whether it remembers instead of reading, and
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

HAND = "                queue.popleft().set_result(self._take(owner, slot))\n"


def newest_first(which: str) -> str:
    """WS0's review: serve the newest waiting frame of a class instead of the oldest."""
    return ("                if " + which + ":\n"
            "                    live = [f for f in queue if not f.done()]\n"
            "                    queue.clear()\n                    queue.extend(live)\n"
            "                    queue.pop().set_result(self._take(owner, slot))\n                    continue\n" + HAND)


IN_LANE = ("        slot = await lane.acquire(owner)\n        try:\n"
           "            if arrived is not None and await self._shed_at_dequeue(ws, data, *arrived):\n"
           "                return\n            await self._handle_message(ws, data)\n"
           "        finally:\n            lane.release(slot)\n")
SCHEMA_SKIP = "        if schema_in_place(conn):\n            return\n"
SHARED_TAIL = "        if self._used[GENERAL] < self.general:\n            return GENERAL\n        return None\n"
OWNER_ORDER = ("            if self._used[RESERVED] < self.reserved:\n"
               "                return RESERVED   # DR-04: the owner's own slots first, so recipients keep all theirs\n"
               "            if self._borrowed < self.shared_cap and self._used[GENERAL] < self.general:\n"
               "                return BORROWED   # then a shared one, never more than shared_cap at once\n")

MUTANTS = [
    # -- whose frame it is ------------------------------------------------------------------------------------------
    ("stamp_not_verified_the_claimed_class_trusted", [
        (LANE, "        principal, reason, _offset = check_relay_stamp(message)\n",
               "        from ..principal import Principal\n"
               "        principal, reason, _offset = Principal(cls=str(stamp.get('cls')), channel='cp_relay',"
               " acting_user=str(stamp.get('acting_user') or '')), VERIFIED, None\n")], LANE_TESTS),
    ("recipient_doors_not_excluded", [(LANE, '        if kind in NON_OWNER_RELAY_TYPES:\n', '        if kind in ():\n')],
     LANE_TESTS),
    ("frame_type_not_folded", [
        (LANE, '        kind = str(message.get("type") or "").strip().lower()\n',
               '        kind = str(message.get("type") or "")\n')], LANE_TESTS),
    ("background_owner_types_in_the_lane", [
        (LANE, "        if kind not in {str(name).strip().lower() for name in interactive}:\n", "        if False:\n")],
     LANE_TESTS),
    ("interactive_list_not_folded", [
        (LANE, "{str(name).strip().lower() for name in interactive}", "{str(name) for name in interactive}")],
     LANE_TESTS),
    ("the_switchs_type_list_not_read", [
        (LANE, "                            types=switches.entries(switches.OWNER_LANE_TYPES))\n", "                            )\n")],
     LANE_TESTS),
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
        (LANE, "            return is_owner_frame(message, self.types) is True\n",
               "            return bool(is_owner_frame(message, self.types))\n")], LANE_TESTS),
    ("the_client_ignores_the_class", [
        (CLIENT, "        owner = lane is not None and lane.owner_frame(data)\n", "        owner = lane is not None\n")],
     LANE_TESTS),
    # -- the slots ---------------------------------------------------------------------------------------------------
    ("recipients_take_kept_slots", [
        (LANE, SHARED_TAIL, SHARED_TAIL.replace("        return None\n",
         "        if self._used[RESERVED] < self.reserved:\n            return RESERVED\n        return None\n"))],
     LANE_TESTS),
    ("owner_takes_a_shared_slot_before_its_kept_one", [
        (LANE, OWNER_ORDER,
         "            if self._borrowed < self.shared_cap and self._used[GENERAL] < self.general:\n"
         "                return BORROWED\n"
         "            if self._used[RESERVED] < self.reserved:\n                return RESERVED\n")], LANE_TESTS),
    ("owner_borrowing_uncapped", [
        (LANE, "            if self._borrowed < self.shared_cap and self._used[GENERAL] < self.general:\n",
               "            if self._used[GENERAL] < self.general:\n")], LANE_TESTS),
    ("the_cap_may_take_every_shared_slot", [
        (LANE, "max(0, min(int(shared_cap), self.general - 1))", "max(0, int(shared_cap))")], LANE_TESTS),
    ("no_slot_kept_leaves_owners_none", [
        (LANE, "        self.shared_cap = self.general if self.reserved == 0 else",
               "        self.shared_cap = 0 if self.reserved == 0 else")], LANE_TESTS),
    ("the_switchs_cap_not_read", [
        (LANE, "                            shared_cap=switches.number(switches.OWNER_LANE_SHARED_MAX),\n", "")],
     LANE_TESTS),
    ("a_borrowed_slot_not_counted_back", [
        (LANE, "        if slot == BORROWED:\n            self._borrowed -= 1\n", "        if False:\n            self._borrowed -= 1\n")],
     LANE_TESTS),
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
    # -- DR-04: each class counts its own waiting places ---------------------------------------------------------------
    ("recipients_counted_with_owner_frames", [
        (LANE, "        if self._in_hand[owner] >= limit:\n",
               "        if (self._in_hand[owner] if owner else self._in_hand[True] + self._in_hand[False]) >= limit:\n")],
     LANE_TESTS),
    ("recipients_given_every_waiting_place", [
        (LANE, "max(1, int(max_pending) - self.reserved)", "int(max_pending)")], LANE_TESTS),
    ("owner_waiting_places_unbounded", [
        (LANE, "        limit = OWNER_LANE_IN_HAND if owner else", "        limit = 10 ** 9 if owner else")], LANE_TESTS),
    ("a_frame_never_counted_out", [
        (CLIENT, "            if lane is not None:\n                task.add_done_callback(lambda _done: lane.left(owner))\n",
                 "            if False:\n                task.add_done_callback(lambda _done: lane.left(owner))\n")],
     LANE_TESTS),
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
                 "        owner = __import__('topos.permissions_v2.owner_lane', fromlist=['is_owner_frame'])"
                 ".is_owner_frame(data) and lane is not None\n")], LANE_TESTS),
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
    # -- WS0's independent review (15 Oct): nine more, five of which the lane's tests did not kill until its patch ----
    ("rv_shared_waiters_served_newest_first", [(LANE, HAND, newest_first("not owner"))], LANE_TESTS),
    ("rv_owner_waiters_served_newest_first", [(LANE, HAND, newest_first("owner"))], LANE_TESTS),
    ("rv_kept_slot_release_wakes_nobody", [
        (LANE, "        self._used[kind] -= 1\n        self._wake()\n",
               "        self._used[kind] -= 1\n        if slot == GENERAL:\n            self._wake()\n")], LANE_TESTS),
    ("rv_kept_slot_borrowed_when_no_owner_waits", [
        (LANE, SHARED_TAIL, SHARED_TAIL.replace("        return None\n",
         "        if not any(not f.done() for f in self._waiting[True]) and self._used[RESERVED] < self.reserved:\n"
         "            return RESERVED\n        return None\n")),
        (LANE, "        self.granted = {(True, GENERAL): 0, (True, RESERVED): 0, (False, GENERAL): 0}\n",
               "        self.granted = {(True, GENERAL): 0, (True, RESERVED): 0, (False, GENERAL): 0, (False, RESERVED): 0}\n")],
     LANE_TESTS),
    ("rv_slot_leaked_on_the_shed_path", [
        (CLIENT, IN_LANE,
         "        slot = await lane.acquire(owner)\n        try:\n"
         "            if arrived is not None and await self._shed_at_dequeue(ws, data, *arrived):\n"
         "                slot = None\n                return\n            await self._handle_message(ws, data)\n"
         "        finally:\n            if slot is not None:\n                lane.release(slot)\n")], LANE_TESTS),
    ("rv_slot_leaked_on_a_timeout", [
        (CLIENT, IN_LANE,
         "        slot = await lane.acquire(owner)\n        try:\n"
         "            if arrived is not None and await self._shed_at_dequeue(ws, data, *arrived):\n"
         "                return\n            await self._handle_message(ws, data)\n"
         "        except (asyncio.TimeoutError, TimeoutError):\n            slot = None\n            raise\n"
         "        finally:\n            if slot is not None:\n                lane.release(slot)\n")], LANE_TESTS),
    ("rv_unstamped_owner_type_by_type_alone", [
        (LANE, '        stamp = message.get("principal_stamp")\n',
               '        stamp = message.get("principal_stamp")\n'
               '        if stamp is None and str(message.get("type") or "") in {"llm_generation", "query",'
               ' "upsert_home_chat_session"}:\n            return True\n')], LANE_TESTS),
    ("rv_schema_objects_missing_an_index", [
        (SCHEMA, '    ("table", "home_chat_memories"), ("index", "idx_home_chat_memories_owner"),\n',
                 '    ("table", "home_chat_memories"),\n')], SCHEMA_TESTS),
    ("rv_schema_column_check_on_the_wrong_table", [
        (SCHEMA, '        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(home_chat_sessions)").fetchall()}\n'
                 '        return "participants_json" in columns\n',
                 '        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(home_chat_sessions)").fetchall()}\n'
                 '        return "participants_json" in columns or "history_json" in columns\n')], SCHEMA_TESTS),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
