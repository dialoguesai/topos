"""Mutation run over NR185: a rubric mismatch (the labeler's `Unresolved` family) raised by an answer retrieval ends
exactly as a final PolicyError with its own code. Every mutant weakens one decision and must be killed by at least
one real test.

Whether the job's catch and the fetch's catch are there at all; whether either is widened past the family so that a
real bug (any other exception) is swallowed as a refusal; whether the recipient gets the one no-answer body, and the
fetch the one refusal (not a pending body that keeps the job); whether the counts-only receipt is still filed;
whether the receipt word is in the closed vocabulary, final (never "not served now"), and the exception's code rather
than its text.

The runner is `n3c_mutants.py`'s (a scratch copy of the engine, one mutant at a time, the worktree never modified;
a mutant whose text does not match exactly once is a failure, not a pass; a hung run is HUNG, never killed).

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/nr185_mutants.py --out nr185-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import n3c_mutants as runner  # noqa: E402

SERVICE = "topos/permissions_v2/answer_release.py"
TESTS = ["tests/permissions_v2/test_nr185_rubric_mismatch_in_answers.py"]
JOB_CATCH = ("        except Unresolved as exc:\n"
             "            # NR185: the final PolicyError it is (its code is neither \"not served now\" nor "
             "`answer_evidence_moved`).\n"
             "            return None, receipt_reason(unresolved_refusal(exc).code)\n")
FETCH_CATCH = "        except Unresolved as exc:\n            raise unresolved_refusal(exc) from None\n"
FETCH_WRAP = ("    _still_supported = _unresolved_refuses(_still_supported)   # NR185: final, as any PolicyError not "
              "\"not served now\"\n")
NO_ANSWER_BODY = '            body = checked.body if checked is not None else NoAnswer(version=VERSION, outcome="no_answer")\n'
FILED = ("            self.runtime.protocol.ledger.checkpoint_answer_receipt(job.request_id, receipt,\n"
         "                decision=job.set_decision, now=ended)\n")

MUTANTS = [
    # The catches are there.
    ("the_job_catch_removed", [(SERVICE, JOB_CATCH, "")]),
    ("the_fetch_catch_removed", [(SERVICE, FETCH_WRAP, "")]),
    # Only the family: widened past it, a real bug is swallowed as a refusal (a PolicyError keeps its own code).
    ("the_job_catch_widened_to_exception", [
        (SERVICE, JOB_CATCH, JOB_CATCH.replace("except Unresolved as exc:", "except Exception as exc:").replace(
            "receipt_reason(unresolved_refusal(exc).code)", "receipt_reason(getattr(exc, 'reason', None) or 'refused')"))]),
    ("the_fetch_catch_widened_to_exception", [
        (SERVICE, FETCH_CATCH, "        except Exception as exc:\n"
                               "            raise PolicyError(getattr(exc, 'code', None) or getattr(exc, 'reason', None)"
                               " or 'refused') from None\n")]),
    # The body: the one no-answer from the job, the one refusal at the fetch.
    ("a_wrong_body_from_the_job", [
        (SERVICE, NO_ANSWER_BODY, NO_ANSWER_BODY.replace(
            'NoAnswer(version=VERSION, outcome="no_answer")\n',
            '(AnswerPending(version=VERSION, state="pending", answer_id=job.answer_id) '
            'if reason == "labeler_rubric_mismatch" else NoAnswer(version=VERSION, outcome="no_answer"))\n'))]),
    ("a_pending_body_at_the_fetch", [
        (SERVICE, FETCH_CATCH, FETCH_CATCH.replace("unresolved_refusal(exc)", "PolicyError('search_index_unavailable')"))]),
    # The receipt is filed before a body can be fetched.
    ("a_missing_receipt", [
        (SERVICE, FILED, '            if reason != "labeler_rubric_mismatch":\n'
                         + "".join("    " + line + "\n" for line in FILED.splitlines()))]),
    # The receipt word: in the vocabulary, final, and the code rather than the text.
    ("the_word_left_out_of_the_vocabulary", [(SERVICE, '    "labeler_rubric_mismatch",\n', "")]),
    ("the_word_made_not_served_now", [
        (SERVICE, '                            "release_snapshot_moved"})\n',
                  '                            "release_snapshot_moved", "labeler_rubric_mismatch"})\n')]),
    ("the_code_taken_from_the_exception_text", [
        (SERVICE, "    return PolicyError(exc.reason)\n", "    return PolicyError(str(exc))\n")]),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
