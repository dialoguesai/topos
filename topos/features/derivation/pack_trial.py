"""Trying a pack file's own examples on the local model (`topos-node packs try FILE`).

A pack file carries examples of its own under `eval`: `gold` (a text and the facts the pack should find in it)
and `negative_controls` (a text it should find nothing in). They are invented text in the file. This module runs
them, and only them, through the extraction prompt (`template.build_prompt`) and the parser
(`template.parse_output`) a real run uses, on the node's local extraction model, and says for each what was
expected, what came back, and whether that passes.

What it is for: someone drafting a pack (often a coding agent, for its owner) can see whether the wording finds
what the examples say it should, before the owner is asked to add anything.

What holds, all of it decided here:

* It reads no record of the owner's. The only text a prompt carries is an example from the file.
* It writes nothing: no fact, no ledger row, no measurement row, no registry row, no file in the pack directory.
  The file's bytes are checked on a private temporary copy (`custom_packs.check_content`), as `packs add --check`
  does, and the copy is gone before the first model call.
* It is capped: at most `MAX_EXAMPLES` examples per call, each at most `MAX_TEXT_CHARS` characters (the length a
  real run reads of a record), one trial at a time on a node, and `TIME_LIMIT_S` in all.
* It runs inside the node, on a worker thread, as background work (`answer_gate.background_work`): every call goes
  through the node's own admission for its model host (`local_serving.request`, taken by the adapter), so it waits
  its turn behind the owner's own requests and behind whatever else the node is running on that model. Nothing
  here loads a model ahead of a call, sets how long one stays loaded, or keeps one: a call is the node's ordinary
  on-demand call (`processing.generate`), and the model is the one the owner picked for facts.
* The fact check a real run makes on each fact found (`verify`) is NOT run: it is a second model, and a pass here
  says nothing about it. Nor are the writer's own guards, which need the owner's records to mean anything.

How an example is judged (`judge`):

* a `gold` example passes when every fact it expects came back: an assertion the parser kept, about the owner,
  with the same predicate, whose value holds every field the example names with an equal value. Equal is the
  writer's own equality (`writer._canon_value`: case and spacing do not count). A field the example does not name
  is not compared, and an assertion the example does not name does not fail it (it is counted, `extra`).
  A gold example that names no expected fact cannot be judged and is reported `unjudged`;
* a `negative_controls` example passes when the parser kept no assertion at all.

`routed` says whether the pack's own prefilter would send that text to the model in a real run
(`prefilter.PackPrefilter`). A negative control that is not routed never reaches the model there; here it is run
anyway, because the question a drafter has is what the prompt does with it.
"""
from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: The most examples one trial runs. A file with more is refused, not cut: which ones ran would be a guess.
MAX_EXAMPLES = 20
#: The most characters of an example a prompt carries: what `build_prompt` reads of any record.
MAX_TEXT_CHARS = 4000
#: The longest one trial may work. An example not started by then is reported `skipped`.
TIME_LIMIT_S = 900.0
#: This many model calls failing in a row end the trial: the model is down, and each further call waits out its
#: own timeout.
MODEL_ERROR_STREAK = 2

GOLD = "gold"
NEGATIVE = "negative_control"

_one_at_a_time = threading.Lock()


class TrialRefused(ValueError):
    """Why a trial did not run. `code` is a fixed word; the message is one plain sentence."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def examples_of(pack: Any) -> List[Dict[str, Any]]:
    """The file's own examples, gold first, in the file's order: `{kind, index, text, role, date, expect}`.
    `expect` is the list the example gives, or None when it gives none that can be read."""
    evaluation = (getattr(pack, "raw", None) or {}).get("eval") or {}
    out: List[Dict[str, Any]] = []
    for kind, key in ((GOLD, "gold"), (NEGATIVE, "negative_controls")):
        for index, case in enumerate(evaluation.get(key) or [], 1):
            if not isinstance(case, dict):
                continue
            expect = case.get("expect")
            if not (isinstance(expect, list) and all(isinstance(e, dict) and isinstance(e.get("predicate"), str)
                                                     for e in expect)):
                expect = None
            role = case.get("role")
            date = case.get("date")
            out.append({"kind": kind, "index": index, "text": str(case.get("text") or ""),
                        "role": role if isinstance(role, str) and role else "authored",
                        "date": date.isoformat() if isinstance(date, datetime.date) else
                        (date if isinstance(date, str) else None),
                        "expect": expect})
    return out


def _same(expected: Any, returned: Any) -> bool:
    """One expected value against one that came back, by the writer's equality. A mapping is compared field by
    field, over the fields the example names."""
    from .writer import _canon_value

    if isinstance(expected, dict):
        return isinstance(returned, dict) and all(
            field in returned and _same(value, returned[field]) for field, value in expected.items())
    if isinstance(expected, list):
        return isinstance(returned, list) and sorted(_canon_value(v) for v in expected) == sorted(
            _canon_value(v) for v in returned)
    return not isinstance(returned, (dict, list)) and _canon_value(expected) == _canon_value(returned)


def _found(expected: Dict[str, Any], returned: List[Dict[str, Any]]) -> bool:
    for assertion in returned:
        if assertion.get("predicate") != expected.get("predicate"):
            continue
        if str(assertion.get("about") or "owner") != str(expected.get("about") or "owner"):
            continue
        if "value" not in expected or _same(expected["value"], assertion.get("value")):
            return True
    return False


def judge(example: Dict[str, Any], returned: List[Dict[str, Any]]) -> Dict[str, Any]:
    """`{outcome, reason, missing, extra}` for one example and the assertions the parser kept for it."""
    if example["kind"] == NEGATIVE:
        if returned:
            return {"outcome": "fail", "reason": "assertion_returned", "missing": 0, "extra": len(returned)}
        return {"outcome": "pass", "reason": None, "missing": 0, "extra": 0}
    expect = example["expect"]
    if not expect:
        return {"outcome": "unjudged", "reason": "no_expected_fact", "missing": 0, "extra": len(returned)}
    missing = sum(1 for wanted in expect if not _found(wanted, returned))
    extra = max(0, len(returned) - (len(expect) - missing))
    if missing:
        return {"outcome": "fail", "reason": "expected_fact_missing", "missing": missing, "extra": extra}
    return {"outcome": "pass", "reason": None, "missing": 0, "extra": extra}


def _projection(assertion: Dict[str, Any]) -> Dict[str, Any]:
    return {"predicate": assertion.get("predicate"), "value": assertion.get("value"),
            "about": assertion.get("about"), "confidence": assertion.get("confidence"),
            "quote": assertion.get("quote")}


def extraction_model(conn: Any) -> str:
    """The local model a pack the owner added is read by: the one picked for facts. "" when none is."""
    from ...config.settings import settings as _settings
    from ...features.facts.llm_extract import _resolved_extraction_model  # type: ignore

    return str(_resolved_extraction_model(_settings, conn) or "").strip()


def model_status(conn: Any, *, timeout_s: float = 2.0) -> Dict[str, Any]:
    """Whether a trial could find its model, asked without loading or calling one: `{model, available}`.

    `available` is `no` when no local extraction model is picked, or the model host answers and does not hold the
    one that is; `yes` when the host lists it; `unknown` when the host does not answer (the node starts it on
    demand, so that is not a no). The one request is the host's own list of what is on disk. It starts no host and
    loads nothing."""
    import json
    import urllib.request

    from ... import local_serving
    from ...engine.backends.ollama import OllamaAdapter

    model = extraction_model(conn)
    if not model:
        return {"model": None, "available": "no"}
    try:
        request = urllib.request.Request(f"{OllamaAdapter()._base_url}/api/tags", method="GET")
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            listed = json.loads(response.read().decode("utf-8")).get("models") or []
        names = {local_serving.model_key(str(row.get("name") or "")) for row in listed if isinstance(row, dict)}
    except Exception:  # noqa: BLE001 -- a host that does not answer, or answers something else, is not a no
        return {"model": model, "available": "unknown"}
    return {"model": model, "available": "yes" if local_serving.model_key(model) in names else "no"}


def _totals(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    def count(kind: Optional[str], outcome: str) -> int:
        return sum(1 for row in rows if row["outcome"] == outcome and (kind is None or row["kind"] == kind))

    def part(kind: str) -> Dict[str, int]:
        return {"examples": sum(1 for row in rows if row["kind"] == kind), "passed": count(kind, "pass"),
                "failed": count(kind, "fail")}

    return {"examples": len(rows), "passed": count(None, "pass"), "failed": count(None, "fail"),
            "unjudged": count(None, "unjudged"), "errors": count(None, "error"), "skipped": count(None, "skipped"),
            "gold": part(GOLD), "negative_controls": part(NEGATIVE)}


def run_trial(content: bytes, conn: Any, *, clock=time.monotonic) -> Dict[str, Any]:
    """Run a pack file's own examples; the report `packs try` prints. Raises :class:`TrialRefused`.

    Blocking: call it on a worker thread, never on the event loop (a model call on a loop thread is not held by
    the node's admission). `conn` is read once, for which model the owner picked; nothing is written to it."""
    from ...core import owner_requests
    from ...engine.backends.ollama import OllamaAdapter
    from ...permissions_v2 import answer_gate
    from . import template as _T
    from .custom_packs import CustomPackError, check_content
    from .prefilter import PackPrefilter
    from .processing import generate
    from .registry import bundled_pack_dir

    if not isinstance(content, (bytes, bytearray)):
        raise TrialRefused("pack_invalid", "The file is not a pack Topos can read.")
    try:
        pack = check_content(bytes(content))
    except CustomPackError as exc:
        raise TrialRefused("pack_invalid", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- a file from outside never ends a trial in a raw error
        raise TrialRefused("pack_invalid", "The file is not a pack Topos can read.") from exc
    examples = examples_of(pack)
    if len(examples) > MAX_EXAMPLES:
        raise TrialRefused("too_many_examples",
                           f"The file holds {len(examples)} examples and one try runs at most {MAX_EXAMPLES}. "
                           "Try it with fewer.")
    model = extraction_model(conn)
    if not model:
        raise TrialRefused("no_local_model", "This Topos has no local model picked for facts, so there is nothing "
                                             "to try the pack on.")
    if not _one_at_a_time.acquire(blocking=False):
        raise TrialRefused("trial_already_running", "Another pack is being tried on this Topos. Wait for it.")
    try:
        _T.set_pack_dir(bundled_pack_dir())
        adapter = OllamaAdapter()
        prefilter = PackPrefilter(pack)
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        began = clock()
        rows: List[Dict[str, Any]] = []
        streak = 0
        stopped: Optional[str] = None
        with answer_gate.background_work():
            for example in examples:
                text = example["text"][:MAX_TEXT_CHARS]
                row: Dict[str, Any] = {
                    "kind": example["kind"], "index": example["index"], "text": text, "role": example["role"],
                    "routed": bool(prefilter.matches(text.lower(), _tokens(text))),
                    "expected": example["expect"] if example["expect"] is not None else [],
                    "returned": [], "rejected": 0, "seconds": 0.0}
                rows.append(row)
                if stopped is None and clock() - began >= TIME_LIMIT_S:
                    stopped = "time_limit"
                if stopped is not None:
                    row.update(outcome="skipped", reason=stopped, missing=0, extra=0)
                    continue
                if example["role"] not in pack.allowed_roles():
                    # A real run never reads a record of this role for this pack, so no call is made for it.
                    row.update({**judge(example, []), "reason": "role_not_read"})
                    continue
                owner_requests.pace()
                started = clock()
                try:
                    out = generate(adapter, model, _T.build_prompt(pack, text, example["date"] or today,
                                                                   example["role"]))
                    failed = bool(isinstance(out, dict) and (out.get("error") or out.get("status") == "deferred"))
                    raw = str(out.get("text") or "") if isinstance(out, dict) else str(out or "")
                except Exception as exc:  # noqa: BLE001 -- a model call that fails is reported, never raised
                    logger.info("pack trial: a model call failed (%s)", type(exc).__name__)
                    failed, raw = True, ""
                row["seconds"] = round(clock() - started, 1)
                if failed:
                    streak += 1
                    row.update(outcome="error", reason="model_call_failed", missing=0, extra=0)
                    if streak >= MODEL_ERROR_STREAK:
                        stopped = "model_unavailable"
                    continue
                streak = 0
                valid, rejected = _T.parse_output(raw, pack, record_text=text)
                row["returned"] = [_projection(a) for a in valid]
                row["rejected"] = int(rejected)
                row.update(judge(example, valid))
        return {"pack_id": pack.pack, "version": pack.version, "model": model,
                "template": _T.TEMPLATE_VERSION, "fact_check": "not_run", "stopped": stopped,
                "seconds": round(clock() - began, 1), "examples": rows, "totals": _totals(rows)}
    finally:
        _one_at_a_time.release()


def _tokens(text: str) -> set:
    from .prefilter import _tokens as tokens

    return tokens(text.lower())
