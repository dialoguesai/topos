"""Mutation run over NR179: an answer job's time under the node's own load (G1 background holds, G2 judge reserve).

Every mutant weakens one decision and must be killed by at least one real test. G1: whether the switch is read (and
off means no wait), whether the owner's own work, the answer job itself, an event loop's thread, a held write gate
and a start's own `only_if` each mean no wait, whether the wait is bounded, whether the answer job marks itself, and
whether each start site holds at all, outside the build slot, before its own checks, and never for an ended share's
removal. G2: whether the reserve is read (and 0 means none), checked before each call with the margin's floor,
reported as out of time (not a plain "no"), whether each call is told and held to the deadline less the margin,
whether a cut-off call is out of time too, and whether the unjudged sentences are all dropped, all counted, carried
to the body and onto the receipt only while the reserve is set.

The runner is `n3c_mutants.py`'s (a scratch copy of the engine, one mutant at a time, the worktree never modified;
a mutant whose text does not match exactly once is a failure, not a pass; a hung run is HUNG, never killed).

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/nr179_mutants.py --out nr179-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import n3c_mutants as runner  # noqa: E402

GATE = "topos/permissions_v2/answer_gate.py"
RELEASE = "topos/permissions_v2/answer_release.py"
QUEUE = "topos/permissions_v2/index_rebuilds.py"
LOOP = "topos/permissions_v2/refresh_loop.py"
GRAPH = "topos/features/entities/graph_refresh.py"
GENERATION = "topos/permissions_v2/answer_generation.py"
G1_TESTS = ["tests/permissions_v2/test_nr179_answer_holds_background.py"]
G2_TESTS = ["tests/permissions_v2/test_nr179_judge_reserve.py"]
TESTS = G1_TESTS + G2_TESTS

RESERVE_CHECK = ("                    if reserve and deadline - self.clock() < max(reserve, JUDGE_RELEASE_MARGIN_SECONDS + 1):\n"
                 "                        raise JudgeOutOfTime()\n")
HOLD_EXEMPT = ("    if _background.get() is None or _answer_job.get() is True or _on_a_loop() or "
               "_holds_the_write_gate():\n")
QUEUE_HOLD = "        answer_gate.hold_background_start(only_if=lambda: self._still_shared(grant_id))\n"
LOOP_PASS_HOLD = ("        answer_gate.hold_background_start(\n"
                  "            only_if=lambda: any(self._policy_hash(grant_id, int(self.clock())) is not None "
                  "for grant_id in due))\n")
LOOP_BUILD_HOLD = ("            answer_gate.hold_background_start(\n"
                   "                only_if=lambda: self._policy_hash(grant_id, int(self.clock())) is not None)\n")
GRAPH_HOLD = "    answer_gate.hold_background_start()\n"
GRAPH_CHECK = ('    if is_derivation_in_flight():\n        # Checked again: a batch may have started while the inputs were '
               'read.\n        raise WriteGateDeferred("derivation batch in flight")\n')

MUTANTS = [
    # -- G1: the hold itself ------------------------------------------------------------------------------------------
    ("g1_switch_off_still_waits", [
        (GATE, "    if not holds_background() or not active():\n        return 0.0\n",
               "    if not active():\n        return 0.0\n")], G1_TESTS),
    ("g1_switch_read_as_on", [
        (GATE, "    return switches.on(switches.ANSWER_HOLDS_BACKGROUND)\n", "    return True\n")], G1_TESTS),
    ("g1_owners_own_work_waits", [
        (GATE, HOLD_EXEMPT, HOLD_EXEMPT.replace("_background.get() is None or ", ""))], G1_TESTS),
    ("g1_inside_an_answer_job_waits", [
        (GATE, HOLD_EXEMPT, HOLD_EXEMPT.replace(" or _answer_job.get() is True", ""))], G1_TESTS),
    ("g1_waits_on_an_event_loops_thread", [
        (GATE, HOLD_EXEMPT, HOLD_EXEMPT.replace(" or _on_a_loop()", ""))], G1_TESTS),
    ("g1_waits_holding_the_write_gate", [
        (GATE, HOLD_EXEMPT, HOLD_EXEMPT.replace(" or _holds_the_write_gate()", ""))], G1_TESTS),
    ("g1_an_unreadable_gate_counts_as_free", [
        (GATE, "        return bool(db_write_lock()._is_owned())\n    except Exception:  # noqa: BLE001\n        return True\n",
               "        return bool(db_write_lock()._is_owned())\n    except Exception:  # noqa: BLE001\n        return False\n")],
     G1_TESTS),
    ("g1_only_if_ignored", [
        (GATE, "        if only_if is not None and not only_if():\n            return 0.0\n",
               "        if False:\n            return 0.0\n")], G1_TESTS),
    ("g1_only_if_that_cannot_say_waits", [
        (GATE, "    except Exception:  # noqa: BLE001 -- a start that cannot say whether it waits does not wait\n"
               "        return 0.0\n",
               "    except Exception:  # noqa: BLE001 -- a start that cannot say whether it waits does not wait\n"
               "        pass\n")], G1_TESTS),
    ("g1_the_wait_is_unbounded", [
        (GATE, "    started = clock()\n    while active() and clock() - started < limit:\n        sleep(poll)\n"
               "    return _note(clock() - started, limit, \"background build\")\n",
               "    started = clock()\n    while active():\n        sleep(poll)\n"
               "    return _note(clock() - started, limit, \"background build\")\n")], G1_TESTS),
    ("g1_the_answer_job_mark_never_set", [
        (GATE, "    token = _answer_job.set(True)\n", "    token = _answer_job.set(False)\n")], G1_TESTS),
    ("g1_the_answer_job_does_not_mark_itself", [
        (RELEASE, "                    with answer_gate.answer_job():\n                        checked, reason = self._compute(job)\n",
                  "                    if True:\n                        checked, reason = self._compute(job)\n")], G1_TESTS),
    # -- G1: the start sites ------------------------------------------------------------------------------------------
    ("g1_queue_never_holds", [(QUEUE, QUEUE_HOLD, "")], G1_TESTS),
    ("g1_queue_holds_inside_the_build_slot", [
        (QUEUE, QUEUE_HOLD, ""),
        (QUEUE, "        with BUILD_SLOT:\n            if self._sync_protection is not None:\n",
                "        with BUILD_SLOT:\n" + QUEUE_HOLD.replace("        ", "            ", 1)
                + "            if self._sync_protection is not None:\n")], G1_TESTS),
    ("g1_queue_holds_an_ended_shares_removal", [
        (QUEUE, QUEUE_HOLD, QUEUE_HOLD.replace("self._still_shared(grant_id)", "True"))], G1_TESTS),
    ("g1_queue_reads_every_share_as_ended", [
        (QUEUE, "            return True\n        except PolicyError:\n            return False\n",
                "            return False\n        except PolicyError:\n            return False\n")], G1_TESTS),
    ("g1_restore_pass_never_holds", [(LOOP, LOOP_PASS_HOLD, "")], G1_TESTS),
    ("g1_restore_build_never_holds", [(LOOP, LOOP_BUILD_HOLD, "")], G1_TESTS),
    ("g1_restore_pass_holds_for_ended_shares", [
        (LOOP, LOOP_PASS_HOLD, "        answer_gate.hold_background_start(only_if=lambda: True)\n")], G1_TESTS),
    ("g1_restore_build_holds_for_an_ended_share", [
        (LOOP, LOOP_BUILD_HOLD, "            answer_gate.hold_background_start(only_if=lambda: True)\n")], G1_TESTS),
    ("g1_graph_never_holds", [(GRAPH, GRAPH_HOLD, "")], G1_TESTS),
    ("g1_graph_holds_after_its_derivation_check", [
        (GRAPH, GRAPH_HOLD, ""), (GRAPH, GRAPH_CHECK, GRAPH_CHECK + GRAPH_HOLD)], G1_TESTS),
    # -- G2: the judge's time reserve ---------------------------------------------------------------------------------
    ("g2_switch_off_still_reserves", [
        (RELEASE, "                reserve = switches.number(switches.ANSWER_JUDGE_RESERVE_SECONDS)\n",
                  "                reserve = switches.number(switches.ANSWER_JUDGE_RESERVE_SECONDS) or 35\n")], G2_TESTS),
    ("g2_switch_never_read", [
        (RELEASE, "                reserve = switches.number(switches.ANSWER_JUDGE_RESERVE_SECONDS)\n",
                  "                reserve = 0\n")], G2_TESTS),
    ("g2_no_reserve_check_before_a_call", [(RELEASE, RESERVE_CHECK, "")], G2_TESTS),
    ("g2_the_reserve_alone_without_the_margin_floor", [
        (RELEASE, RESERVE_CHECK, RESERVE_CHECK.replace("max(reserve, JUDGE_RELEASE_MARGIN_SECONDS + 1)", "reserve"))],
     G2_TESTS),
    ("g2_out_of_time_is_a_plain_no", [
        (RELEASE, RESERVE_CHECK, RESERVE_CHECK.replace("raise JudgeOutOfTime()", "return False"))], G2_TESTS),
    ("g2_the_call_is_told_the_job_deadline", [
        (RELEASE, "                        call = self.judge(sentence, items, deadline=end_by, question=asked, **extra)\n",
                  "                        call = self.judge(sentence, items, deadline=deadline, question=asked, **extra)\n")],
     G2_TESTS),
    ("g2_no_margin_before_the_deadline", [
        (RELEASE, "                end_by = deadline - JUDGE_RELEASE_MARGIN_SECONDS if reserve else deadline\n",
                  "                end_by = deadline\n")], G2_TESTS),
    ("g2_the_call_is_never_cut_off", [
        (RELEASE, "                        if reserve:\n                            call = within(call, end_by - self.clock())\n",
                  "")], G2_TESTS),
    ("g2_a_cut_off_call_is_a_plain_no", [
        (RELEASE, "                        except BaseException:  # noqa: BLE001 -- the call is over either way\n"
                  "                            pass\n                        raise JudgeOutOfTime()\n",
                  "                        except BaseException:  # noqa: BLE001 -- the call is over either way\n"
                  "                            pass\n                        return False\n")], G2_TESTS),
    ("g2_out_of_time_swallowed_by_the_closure", [
        (RELEASE, "                    except JudgeOutOfTime:\n                        raise\n", "")], G2_TESTS),
    ("g2_out_of_time_counted_as_support_drops", [
        (GENERATION, "        except JudgeOutOfTime:\n            # NR179 G2: no time left to judge; this sentence and the rest "
                     "are dropped, never kept unjudged.\n            unjudged = len(sieved.sentences) - position\n"
                     "            break\n", "")], G2_TESTS),
    ("g2_out_of_time_keeps_the_rest_unjudged", [
        (GENERATION, "            unjudged = len(sieved.sentences) - position\n            break\n",
                     "            unjudged = len(sieved.sentences) - position\n            kept.extend(sieved.sentences[position:])\n"
                     "            break\n")], G2_TESTS),
    ("g2_only_the_next_sentence_counted", [
        (GENERATION, "            unjudged = len(sieved.sentences) - position\n", "            unjudged = 1\n")], G2_TESTS),
    ("g2_the_count_lost_by_the_judge_phase", [
        (GENERATION, "    return dataclasses.replace(sieved, sentences=tuple(kept), dropped_unjudged=unjudged), drops\n",
                     "    return dataclasses.replace(sieved, sentences=tuple(kept)), drops\n")], G2_TESTS),
    ("g2_the_count_lost_by_the_body_close", [
        (GENERATION, ",\n             \"dropped_unjudged\": sieved.dropped_unjudged}\n", "}\n")], G2_TESTS),
    ("g2_the_receipt_never_counts", [
        (RELEASE, "                **({\"dropped_unjudged\": 0 if checked is None else checked.dropped_unjudged}\n",
                  "                **({\"dropped_unjudged\": 0}\n")], G2_TESTS),
    ("g2_the_receipt_counts_when_off", [
        (RELEASE, "                   if switches.on(switches.ANSWER_SUPPORT_JUDGE)\n"
                  "                   and switches.number(switches.ANSWER_JUDGE_RESERVE_SECONDS) else {}),\n",
                  "                   if switches.on(switches.ANSWER_SUPPORT_JUDGE) else {}),\n")], G2_TESTS),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
