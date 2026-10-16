"""Mutation run over NR181: what the node's log may say about a relay frame, a handler's error and a message's content.

Every mutant puts one piece of content back into a log record, or weakens the keyed digest, and must be killed by a
real test: the invalid frame quoted, or its head in a second line, or carried whole in the JSON formatter's extras;
the handler's error text, its prefix or its traceback text (`exc_info`) logged; ingestion's preview keeping a head or
going back to the 100-character prefix; the digest unkeyed, keyed with a fixed key, or measured in characters.
The first four marked `rv_` are WS0's review mutants (15 Oct), which survived the first tests.

The runner is `n3c_mutants.py`'s (a scratch copy of the engine, one mutant at a time, the worktree never modified;
a mutant whose text does not match exactly once is a failure, not a pass; a hung run is HUNG, never killed).

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/nr181_mutants.py --out nr181-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import n3c_mutants as runner  # noqa: E402

CLIENT = "topos/control_plane_client.py"
PREVIEW = "topos/ingestion/log_preview.py"
FINGERPRINT = "topos/core/log_fingerprint.py"
TESTS = ["tests/topos/test_nr181_no_raw_frame_logs.py", "tests/topos/test_browser_events_ingest.py",
         "tests/ingestion/test_ai_chat_writer_class.py"]
FAST = ["tests/topos/test_nr181_no_raw_frame_logs.py"]

WARN = '                            logger.warning("Relay message is not valid JSON: %s", fingerprint(raw))\n'
ERROR = ('            logger.error("Handler raised exception: type=%s id=%s error=%s\\n%s", msg_type, data.get("id"),\n'
         '                         type(exc).__name__, "".join(traceback.format_tb(exc.__traceback__)).rstrip())\n')
TB = '                         type(exc).__name__, "".join(traceback.format_tb(exc.__traceback__)).rstrip())\n'
DIGEST = '    return f"length={len(data)} hmac={hmac.new(_KEY, data, hashlib.sha256).hexdigest()[:DIGEST_HEX]}"\n'

MUTANTS = [
    # -- WS0's review mutants ----------------------------------------------------------------------------------------
    ("rv_r3_invalid_frame_head_in_a_debug_line", [
        (CLIENT, WARN, WARN + '                            logger.debug("Relay frame head: %s", str(raw)[:60])\n')],
     FAST),
    ("rv_r3_handler_error_text_prefix_in_a_debug_line", [
        (CLIENT, TB, TB + '            logger.debug("Handler error detail: %s", str(exc)[:40])\n')], FAST),
    ("rv_r3_preview_keeps_a_content_head", [
        (PREVIEW, "    return fingerprint(value)\n", "    return fingerprint(value) + ' head=' + str(value)[:12]\n")],
     TESTS),
    ("rv_r3_whole_frame_in_the_json_logs_extra", [
        (CLIENT, WARN, WARN.replace("fingerprint(raw))", 'fingerprint(raw), extra={"extra": {"frame": str(raw)}})'))],
     FAST),
    # -- the lane's own --------------------------------------------------------------------------------------------
    ("r3_invalid_frame_quoted", [(CLIENT, WARN, WARN.replace("fingerprint(raw))", "raw)"))], FAST),
    ("r3_handler_error_text_logged", [
        (CLIENT, ERROR, '            logger.error("Handler raised exception: %s", exc, exc_info=True)\n')], FAST),
    ("r3_handler_error_traceback_text_logged", [
        (CLIENT, TB, TB.replace(".rstrip())\n", ".rstrip(), exc_info=True)\n"))], FAST),
    ("r3_preview_is_the_old_prefix", [
        (PREVIEW, "    return fingerprint(value)\n", "    return str(value)[:max_len]\n")], TESTS),
    ("r3_digest_unkeyed", [
        (FINGERPRINT, DIGEST, DIGEST.replace("hmac.new(_KEY, data, hashlib.sha256)", "hashlib.sha256(data)"))], FAST),
    ("r3_digest_key_fixed_in_the_source", [
        (FINGERPRINT, "_KEY = secrets.token_bytes(32)\n", "_KEY = bytes(32)\n")], FAST),
    ("r3_length_in_characters_not_bytes", [
        (FINGERPRINT, DIGEST, DIGEST.replace("length={len(data)}", "length={len(value) if isinstance(value, str) else len(data)}"))],
     FAST),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
