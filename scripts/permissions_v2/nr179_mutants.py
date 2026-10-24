"""Mutation run over NR179: an answer job's time under the node's own load (G1 background holds, G2 judge reserve).

Every mutant weakens one decision and must be killed by at least one real test. G1: whether the switch is read (and
off means no wait), whether the owner's own work, the answer job itself, an event loop's thread, a held write gate
and a start's own `only_if` each mean no wait, whether the wait is bounded, whether the answer job marks itself, and
whether each start site holds at all, outside the build slot, before its own checks, and never for an ended share's
removal. G2: whether the reserve is read (and 0 means none), checked before each call with the margin's floor,
reported as out of time (not a plain "no"), whether each call is told and held to the deadline less the margin,
whether a cut-off call is out of time too, and whether the unjudged sentences are all dropped, all counted, carried
to the body and onto the receipt only while the judge and the reserve are on; and (review) whether a set reserve is
read within [6, 60] and 0 stays off. Since WS0's review (15 Oct) G1 also checks that a hold ends and wakes at the
node's stop and that both upgrade steps hold; the review's own mutants carry its `rv_` names.

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
UPGRADES = "topos/upgrades/runner.py"
GENERATION = "topos/permissions_v2/answer_generation.py"
G1_TESTS = ["tests/permissions_v2/test_nr179_answer_holds_background.py"]
G2_TESTS = ["tests/permissions_v2/test_nr179_judge_reserve.py"]
TESTS = G1_TESTS + G2_TESTS

RESERVE_CHECK = ("                    if reserve and deadline - self.clock() < reserve:\n"
                 "                        raise JudgeOutOfTime()\n")
CLAMP = ("    return 0 if seconds <= 0 else min(max(seconds, JUDGE_RESERVE_MIN_SECONDS), JUDGE_RESERVE_MAX_SECONDS)\n")
RECEIPT_IF = "                   if switches.on(switches.ANSWER_SUPPORT_JUDGE) and judge_reserve() else {}),\n"
HOLD_EXEMPT = ("    if _background.get() is None or _answer_job.get() is True or _on_a_loop() or "
               "_holds_the_write_gate():\n")
QUEUE_HOLD = "        answer_gate.hold_background_start(only_if=lambda: self._still_shared(grant_id))\n"
LOOP_PASS_HOLD = ("        answer_gate.hold_background_start(\n"
                  "            only_if=lambda: any(self._policy_hash(grant_id, int(self.clock())) is not None "
                  "for grant_id in due))\n")
LOOP_BUILD_HOLD = ("            answer_gate.hold_background_start(\n"
                   "                only_if=lambda: self._policy_hash(grant_id, int(self.clock())) is not None)\n")
GRAPH_HOLD = "    answer_gate.hold_background_start()\n"
SLOT = "                with BUILD_SLOT, node_principal(self.owner_id):\n"
GRAPH_CHECK = ('    if is_derivation_in_flight():\n        # Checked again: a batch may have started while the inputs were '
               'read.\n        raise WriteGateDeferred("derivation batch in flight")\n')

MUTANTS = [
    # -- G1: the hold itself ------------------------------------------------------------------------------------------
    ("g1_switch_off_still_waits", [
        # OFF2N moved the switch's read into the hold's own line (the owner's part does not read it).
        (GATE, "    answers = holds_background()\n", "    answers = True\n")], G1_TESTS),
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
        (GATE, "    while (whom.saw(answers and active(), _owner_holds()) and clock() - started < limit\n"
               "           and not run.is_stop_requested()):\n",
               "    while whom.saw(answers and active(), _owner_holds()) and not run.is_stop_requested():\n")],
     G1_TESTS),
    ("g1_a_stop_does_not_end_the_wait", [
        (GATE, "    while (whom.saw(answers and active(), _owner_holds()) and clock() - started < limit\n"
               "           and not run.is_stop_requested()):\n",
               "    while whom.saw(answers and active(), _owner_holds()) and clock() - started < limit:\n")],
     G1_TESTS),
    ("g1_a_stop_does_not_wake_the_sleep", [
        (GATE, "    sleep = run.wait if sleep is None else sleep\n",
               "    sleep = time.sleep if sleep is None else sleep\n")], G1_TESTS),
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
    ("g1_upgrade_endpoint_step_never_holds", [
        (UPGRADES, "        answer_gate.hold_background_start()\n        return dict(run_graph_rebuild(conn))\n",
                   "        return dict(run_graph_rebuild(conn))\n")], G1_TESTS),
    ("g1_upgrade_derived_step_never_holds", [
        (UPGRADES, "                answer_gate.hold_background_start()   # NR179 G1, as the engine-endpoint step above\n",
                   "")], G1_TESTS),
    # -- WS0's review mutants (15 Oct; `rv_`, the review's own names and edits) -------------------------------------
    ("rv_g1_wait_never_times_out", [
        (GATE, "    limit = LIMIT_SECONDS if limit is None else limit\n",
               "    limit = float('inf') if limit is None else limit\n")], G1_TESTS),
    ("rv_g1_restore_build_holds_inside_the_build_slot", [
        (LOOP, LOOP_BUILD_HOLD, ""),
        (LOOP, SLOT, SLOT + LOOP_BUILD_HOLD.replace("            ", "                    ", 1)
                                           .replace("                only_if", "                    only_if"))], G1_TESTS),
    ("rv_g1_background_marked_work_never_holds", [
        (GATE, HOLD_EXEMPT, HOLD_EXEMPT.replace("_background.get() is None", "_background.get() is not False"))],
     G1_TESTS),
    ("rv_g1_queue_hold_taken_under_the_write_gate", [
        (QUEUE, QUEUE_HOLD,
         "        from topos.storage.db.write_gate import with_db_write as _gate\n"
         "        with _gate():\n    " + QUEUE_HOLD)], G1_TESTS),
    ("rv_g1_restore_pass_holds_only_if_every_share_is_live", [
        (LOOP, LOOP_PASS_HOLD, LOOP_PASS_HOLD.replace("only_if=lambda: any(", "only_if=lambda: all("))], G1_TESTS),
    # -- G2: the judge's time reserve ---------------------------------------------------------------------------------
    ("g2_switch_off_still_reserves", [
        (RELEASE, "                reserve = judge_reserve()\n", "                reserve = judge_reserve() or 35\n")], G2_TESTS),
    ("g2_switch_never_read", [(RELEASE, "                reserve = judge_reserve()\n", "                reserve = 0\n")],
     G2_TESTS),
    ("g2_zero_read_as_the_floor", [
        (RELEASE, CLAMP, CLAMP.replace("return 0 if seconds <= 0 else ", "return "))], G2_TESTS),
    ("g2_the_clamp_has_no_ceiling", [
        (RELEASE, CLAMP, CLAMP.replace("min(max(seconds, JUDGE_RESERVE_MIN_SECONDS), JUDGE_RESERVE_MAX_SECONDS)",
                                       "max(seconds, JUDGE_RESERVE_MIN_SECONDS)"))], G2_TESTS),
    ("g2_no_reserve_check_before_a_call", [(RELEASE, RESERVE_CHECK, "")], G2_TESTS),
    ("g2_the_reserve_alone_without_the_margin_floor", [
        (RELEASE, CLAMP, CLAMP.replace("max(seconds, JUDGE_RESERVE_MIN_SECONDS)", "seconds"))], G2_TESTS),
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
        (RELEASE, RECEIPT_IF, "                   if switches.on(switches.ANSWER_SUPPORT_JUDGE) else {}),\n")], G2_TESTS),
    # -- WS0's review mutants for G2 (`rv_`; the two on changed lines are re-anchored to the clamped check) ---------
    ("rv_g2_out_of_time_also_drops_the_kept_sentences", [
        (GENERATION, "            unjudged = len(sieved.sentences) - position\n            break\n",
                     "            unjudged = len(sieved.sentences) - position\n            kept = []\n            break\n")],
     G2_TESTS),
    ("rv_g2_call_cut_at_the_job_deadline_not_the_margin", [
        (RELEASE, "                            call = within(call, end_by - self.clock())\n",
                  "                            call = within(call, deadline - self.clock())\n")], G2_TESTS),
    ("rv_g2_receipt_counts_unjudged_with_the_judge_off", [
        (RELEASE, RECEIPT_IF, "                   if judge_reserve() else {}),\n")], G2_TESTS),
    ("rv_g2_reserve_floor_off_by_one", [
        (RELEASE, "deadline - self.clock() < reserve:", "deadline - self.clock() <= reserve:")], G2_TESTS),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
