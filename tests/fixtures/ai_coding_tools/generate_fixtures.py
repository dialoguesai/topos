#!/usr/bin/env python3
"""Write the synthetic Claude Code transcript trees under ``claude_code/``.

Every record here is invented: fake uuids, a fake home (``/Users/fake``), fake
secrets of every CONTRACTS §2.4 shape, fake hostnames. Nothing is copied from a
real ``~/.claude/projects``. One case per directory; each case is a reader root
(``<case>/<project-slug>/<session>.jsonl``) so a test can point
``read_claude_code_batch(root_path=...)`` at it directly.

Run from the repo root: ``python tests/fixtures/ai_coding_tools/generate_fixtures.py``.
The expected counts in ``tests/ingestion/test_claude_code_reader.py`` are literals
derived from the records written here; change both together.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "claude_code"
SLUG = "-Users-fake-dev-widget"
CWD = "/Users/fake/dev/widget"
VERSION = "2.1.290"
BASE = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def ts(i: int, base: datetime = BASE) -> str:
    stamp = base + timedelta(milliseconds=250 * i)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def user(n: int, parent: str | None, session: str, i: int, content, *, base: datetime = BASE, **extra) -> dict:
    record = {
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": CWD,
        "sessionId": session,
        "version": VERSION,
        "gitBranch": "main",
        "type": "user",
        "message": {"role": "user", "content": content},
        "uuid": uid(n),
        "timestamp": ts(i, base),
    }
    record.update(extra)
    return record


def assistant(n: int, parent: str | None, session: str, i: int, blocks, msg_id: str, *, base: datetime = BASE,
              stop: str | None = None, **extra) -> dict:
    record = {
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": CWD,
        "sessionId": session,
        "version": VERSION,
        "gitBranch": "main",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": "claude-fable-5-1",
            "content": blocks,
        },
        "requestId": f"req_{n:06x}",
        "type": "assistant",
        "uuid": uid(n),
        "timestamp": ts(i, base),
    }
    if stop is not None:
        record["message"]["stop_reason"] = stop
    record.update(extra)
    return record


def attachment(n: int, parent: str | None, session: str, i: int, payload: dict, *, base: datetime = BASE) -> dict:
    return {"parentUuid": parent, "isSidechain": False, "type": "attachment", "attachment": payload,
            "uuid": uid(n), "sessionId": session, "timestamp": ts(i, base)}


def system(n: int, parent: str | None, session: str, i: int, subtype: str, *, base: datetime = BASE) -> dict:
    return {"parentUuid": parent, "isSidechain": False, "type": "system", "subtype": subtype, "isMeta": False,
            "uuid": uid(n), "sessionId": session, "timestamp": ts(i, base)}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def tool_use(name: str, inputs: dict, tid: str = "toolu_01") -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inputs}


def tool_result(content: str, tid: str = "toolu_01") -> list:
    return [{"tool_use_id": tid, "type": "tool_result", "content": content}]


def thinking(t: str) -> dict:
    return {"type": "thinking", "thinking": t, "signature": "sig_fake"}


def write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def session_file(case: str, session: str, slug: str = SLUG) -> Path:
    return ROOT / case / slug / f"{session}.jsonl"


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def case_plain() -> None:
    s = uid(0x101)
    write_jsonl(session_file("plain", s), [
        user(1, None, s, 0, "Add a retry to the uploader"),
        assistant(2, uid(1), s, 1, [text("I'll add a retry with backoff.")], "msg_m1"),
        user(3, uid(2), s, 2, [text("Make it three attempts")]),
        assistant(4, uid(3), s, 3, [text("Done: three attempts.")], "msg_m2"),
        assistant(5, uid(4), s, 4, [text("Each attempt waits with jitter.")], "msg_m2"),
    ])


def case_tool_result_in_user() -> None:
    s = uid(0x102)
    write_jsonl(session_file("tool_result_in_user", s), [
        user(0x11, None, s, 0, "Read the config"),
        assistant(0x12, uid(0x11), s, 1, [text("Reading it now.")], "msg_m11"),
        assistant(0x13, uid(0x12), s, 2, [tool_use("Read", {"file_path": f"{CWD}/config.toml"})], "msg_m11"),
        user(0x14, uid(0x13), s, 3, tool_result("timeout = 5\nretries = 3"),
             toolUseResult={"type": "text", "file": {"filePath": f"{CWD}/config.toml"}}),
        assistant(0x15, uid(0x14), s, 4, [text("The config sets a 5s timeout and 3 retries.")], "msg_m12"),
    ])


def case_thinking_and_tool_use() -> None:
    s = uid(0x103)
    write_jsonl(session_file("thinking_and_tool_use", s), [
        user(0x21, None, s, 0, "Where is the retry loop?"),
        assistant(0x22, uid(0x21), s, 1, [thinking("Need to grep for retry")], "msg_m21"),
        assistant(0x23, uid(0x22), s, 2, [tool_use("Grep", {"pattern": "retry"})], "msg_m21"),
        user(0x24, uid(0x23), s, 3, tool_result("uploader.py:42: for attempt in range(3):")),
        assistant(0x25, uid(0x24), s, 4, [text("It's in uploader.py at line 42.")], "msg_m22"),
    ])


def case_harness_injected() -> None:
    s = uid(0x104)
    records = [
        # 1. a bare system-reminder: harness, tag system-reminder
        user(0x31, None, s, 0, "<system-reminder>\nThe file was modified.\n</system-reminder>"),
        # 2–3. slash-command records
        user(0x32, uid(0x31), s, 1, "<command-name>/clear</command-name>\n<command-message>clear</command-message>\n<command-args></command-args>"),
        user(0x33, uid(0x32), s, 2, "<command-message>clear</command-message>"),
        # 4. task notification on a new build: origin.kind says so (origin rule wins)
        user(0x34, uid(0x33), s, 3, "<task-notification>agent-7 finished</task-notification>",
             origin={"kind": "task-notification"}),
        # 5. the same on an older build: only the tag says so
        user(0x35, uid(0x34), s, 4, "<task-notification>older build</task-notification>"),
        # 6–9. other harness tags
        user(0x36, uid(0x35), s, 5, "<cross-session-message>lane L2 says hi</cross-session-message>"),
        user(0x37, uid(0x36), s, 6, "<local-command-stdout>main</local-command-stdout>"),
        user(0x38, uid(0x37), s, 7, "<bash-input>ls -la</bash-input>"),
        user(0x39, uid(0x38), s, 8, "<ide_opened_file>uploader.py</ide_opened_file>"),
        # 10. the interrupt marker
        user(0x3A, uid(0x39), s, 9, "[Request interrupted by user for tool use]"),
        # 11. genuinely typed, behind a leading reminder: text kept after the strip
        user(0x3B, uid(0x3A), s, 10, "<system-reminder>\nContext: tests passed.\n</system-reminder>\nPlease rename the module to uploads"),
        # 12. a peer message: plain text, no tag, isMeta
        user(0x3C, uid(0x3B), s, 11, "Lane L2 here: the dispatch route is ready for you", isMeta=True),
        # 13. an old-build record: no origin, no turnOrigin, no promptSource
        user(0x3D, uid(0x3C), s, 12, "Now run the tests", version="2.1.170"),
        # 14. a peer on a new build carries all three marks; counted once, by the first rule
        user(0x3E, uid(0x3D), s, 13, "Peer: merged", isMeta=True, origin={"kind": "peer"}, turnOrigin="peer"),
        # 15. turnOrigin alone
        user(0x3F, uid(0x3E), s, 14, "Scheduled: check the deploy", turnOrigin="peer"),
        # 16. promptSource system
        user(0x40, uid(0x3F), s, 15, "Injected prompt", promptSource="system"),
        # 17. an image plus text: typed
        user(0x41, uid(0x40), s, 16, [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
            text("What's in this screenshot?"),
        ]),
        # 18. typed in the desktop app: every mark says human, promptSource sdk
        user(0x42, uid(0x41), s, 17, "Typed in the desktop app", promptSource="sdk",
             origin={"kind": "human"}, turnOrigin="human", entrypoint="claude-desktop"),
        # 20–23. the remaining harness tags
        user(0x44, uid(0x42), s, 18, "<local-command-caveat>Caveat: the messages below were generated by the user while running local commands.</local-command-caveat>"),
        user(0x45, uid(0x44), s, 19, "<ci-monitor-event>run 42 failed</ci-monitor-event>"),
        user(0x46, uid(0x45), s, 20, "<artifact-view-context>viewing /x</artifact-view-context>"),
        user(0x47, uid(0x46), s, 21, "<create-pr-command>open a PR</create-pr-command>"),
        # 19. one reply
        assistant(0x43, uid(0x47), s, 22, [text("Renamed, tests pass, and the screenshot shows the tray icon.")], "msg_m31"),
    ]
    write_jsonl(session_file("harness_injected", s), records)


def case_compaction() -> None:
    s = uid(0x105)
    write_jsonl(session_file("compaction", s), [
        user(0x51, None, s, 0, "Start the uploader rewrite"),
        assistant(0x52, uid(0x51), s, 1, [text("Starting with the retry loop.")], "msg_m51"),
        {"type": "summary", "summary": "Uploader rewrite", "leafUuid": uid(0x52)},
        user(0x53, None, s, 2,
             "This session is being continued from a previous conversation that ran out of context. Summary: retry loop started.",
             isCompactSummary=True, logicalParentUuid=uid(0x52)),
        {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted", "isMeta": True,
         "uuid": uid(0x54), "parentUuid": uid(0x53), "sessionId": s, "timestamp": ts(3),
         "compactMetadata": {"trigger": "auto", "preTokens": 150000}},
        user(0x55, uid(0x54), s, 4, "Continue with the tests"),
        assistant(0x56, uid(0x55), s, 5, [text("Tests added for the retry loop.")], "msg_m52"),
    ])


def case_fork_copy() -> None:
    orig = uid(0x106)
    fork = uid(0x107)
    shared = [
        user(0x61, None, orig, 0, "Explain the retry loop"),
        assistant(0x62, uid(0x61), orig, 1, [text("It retries three times.")], "msg_m61"),
    ]
    write_jsonl(session_file("fork_copy", orig), shared)
    copied = [dict(r, sessionId=fork) for r in shared]
    write_jsonl(session_file("fork_copy", fork), copied + [
        user(0x63, uid(0x62), fork, 2, "Now add logging"),
        assistant(0x64, uid(0x63), fork, 3, [text("Logging added.")], "msg_m62"),
    ])


def case_side_files() -> None:
    s = uid(0x108)
    main = [
        user(0x71, None, s, 0, "Hello"),
        assistant(0x72, uid(0x71), s, 1, [text("Hi.")], "msg_m71"),
    ]
    write_jsonl(session_file("side_files", s), main)
    folder = session_file("side_files", s).parent
    # Unique records in the side files: a reader that fails to skip them shows extra rows.
    write_jsonl(folder / f"{s}.jsonl.orphaned-20261001T090000Z", [user(0x73, None, s, 2, "Orphan text")])
    write_jsonl(folder / f"{s}.superseded-20261001T085959Z.jsonl", [user(0x74, None, s, 3, "Superseded text")])


def case_subagent_taint() -> None:
    s = uid(0x109)
    write_jsonl(session_file("subagent_taint", s), [
        user(0x81, None, s, 0, "Check my recent emails via Topos"),
        assistant(0x82, uid(0x81), s, 1, [text("I'll have a subagent look.")], "msg_m81"),
        assistant(0x83, uid(0x82), s, 2, [tool_use("Agent", {"prompt": "search mail"}, "toolu_agent")], "msg_m81"),
        user(0x84, uid(0x83), s, 7, tool_result("Found 2 mails from Bob.", "toolu_agent")),
        assistant(0x85, uid(0x84), s, 8, [text("Bob wrote about the invoice.")], "msg_m82"),
        assistant(0x86, uid(0x85), s, 9, [text("Want me to draft a reply?")], "msg_m83"),
    ])
    sub = session_file("subagent_taint", s).parent / s / "subagents" / "agent-a1b2c3.jsonl"
    write_jsonl(sub, [
        user(0x91, None, s, 3, "search mail", isSidechain=True, agentId="a1b2c3"),
        assistant(0x92, uid(0x91), s, 4,
                  [tool_use("mcp__topos__remote__topos-gmail__search_gmail_messages", {"q": "from:bob"}, "toolu_g")],
                  "msg_m91", isSidechain=True, agentId="a1b2c3"),
        user(0x93, uid(0x92), s, 5, tool_result("2 results", "toolu_g"), isSidechain=True, agentId="a1b2c3"),
        assistant(0x94, uid(0x93), s, 6, [text("Found 2 mails.")], "msg_m92", isSidechain=True, agentId="a1b2c3"),
    ])


def case_topos_call_then_three() -> None:
    s = uid(0x10A)
    write_jsonl(session_file("topos_call_then_three", s), [
        user(0xA1, None, s, 0, "What did I work on this week?"),
        assistant(0xA2, uid(0xA1), s, 1, [text("Let me ask your Topos.")], "msg_ma1"),
        assistant(0xA3, uid(0xA2), s, 2,
                  [tool_use("mcp__plugin_topos_topos__query_scope",
                            {"scope_id": "work_context:read", "intent": "this week's work"}, "toolu_q")],
                  "msg_ma2"),
        user(0xA4, uid(0xA3), s, 3, tool_result('{"notice": "Content from your Topos.", "data": {}}', "toolu_q")),
        assistant(0xA5, uid(0xA4), s, 4, [text("You worked on the uploader.")], "msg_ma3"),
        user(0xA6, uid(0xA5), s, 5, "And last week?"),
        assistant(0xA7, uid(0xA6), s, 6, [text("Last week was the tray icon.")], "msg_ma4"),
        assistant(0xA8, uid(0xA7), s, 7, [text("Anything else?")], "msg_ma5"),
    ])


def case_bash_touches_topos() -> None:
    s = uid(0x10B)
    write_jsonl(session_file("bash_touches_topos", s), [
        user(0xB1, None, s, 0, "Is the node up?"),
        assistant(0xB2, uid(0xB1), s, 1, [text("Checking.")], "msg_mb1"),
        assistant(0xB3, uid(0xB2), s, 2,
                  [tool_use("Bash", {"command": "curl --unix-socket ~/.topos/engine.sock http://localhost/healthcheck"}, "toolu_b")],
                  "msg_mb2"),
        user(0xB4, uid(0xB3), s, 3, tool_result('{"status":"ok"}', "toolu_b")),
        assistant(0xB5, uid(0xB4), s, 4, [text("The node answers.")], "msg_mb3"),
        assistant(0xB6, uid(0xB5), s, 5, [text("Shall I continue?")], "msg_mb4"),
    ])


# Invented secrets. The three "node secrets" are what a runner would pass as
# ``secrets=``; the rest exercise one §2.4 shape each.
NODE_SECRETS = {
    "topos_key": "3f9c2b7e1a4d5c6b8e7f0a1b2c3d4e5f",  # gitleaks:allow
    "owner_key": "ok_" + "a1" * 24,
    "agent_label": "tpa_claude-code." + "b2" * 20,
}
SHAPE_SECRETS = {
    "topos_node_key": "tpn_" + "c3" * 20,
    "topos_client_token": "tpk_d4e5f6a7b8c9d0e1f2a3b4c5",  # gitleaks:allow
    "topos_owner_key": "ok_" + "e5" * 10,
    "anthropic_key": "sk-ant-api03-" + "A" * 40,
    "openai_key": "sk-proj" + "B" * 20,
    "github_token": "ghp_" + "C" * 36,
    "aws_access_key": "AKIADEFGHIJKLMNOPQRS",  # gitleaks:allow
    "slack_token": "xoxb-12345678ab-abcdefghijklmnop",  # gitleaks:allow
    "google_api_key": "AIza" + "E" * 35,
    "bearer_token": "F" * 30,
    "private_key_block": "-----BEGIN RSA PRIVATE KEY-----\nMIIEfakefakefake\n-----END RSA PRIVATE KEY-----",
    "env_assignment": "hunter2hunter2",
}
HOME_FORMS = (
    "/Users/fake/dev/widget/app.py",
    "/home/fake/dev/widget",
    "-Users-fake-dev-widget",
    "/private/tmp/claude-501/-Users-fake-dev-widget/scratch",
)
HOSTNAMES = ("fakebox.local", "fakebox-7")


def case_secrets() -> None:
    s = uid(0x10C)
    n = NODE_SECRETS
    sh = SHAPE_SECRETS
    u1 = (
        f"My env has TOPOS_KEY={n['topos_key']} and the owner key {n['owner_key']}; "
        f"the agent label is {n['agent_label']}. Also a node key {sh['topos_node_key']} "
        f"and a client token {sh['topos_client_token']} and another owner key {sh['topos_owner_key']}."
    )
    a1 = (
        f"I saw {sh['anthropic_key']} and {sh['openai_key']} in {HOME_FORMS[0]}; "
        f"the Linux copy is at {HOME_FORMS[1]} and the slug is {HOME_FORMS[2]}. "
        f"Scratch lives in {HOME_FORMS[3]}. Hosts: {HOSTNAMES[0]} and {HOSTNAMES[1]}."
    )
    u2 = (
        f"Tokens: {sh['github_token']} {sh['aws_access_key']} {sh['slack_token']} {sh['google_api_key']}\n"
        f"Authorization: Bearer {sh['bearer_token']}\n"
        f"{sh['private_key_block']}\n"
        f"DB_PASSWORD={sh['env_assignment']}\n"
        "That is everything."
    )
    # Every record of this case carries a marker for the repo's secret scanner:
    # the values are invented, and the reader never copies unknown keys.
    mark = {"_synthetic": "invented secrets for the scrub guards; gitleaks:allow"}
    write_jsonl(session_file("secrets", s), [
        user(0xC1, None, s, 0, u1, **mark),
        assistant(0xC2, uid(0xC1), s, 1, [text(a1)], "msg_mc1", **mark),
        user(0xC3, uid(0xC2), s, 2, u2, **mark),
    ])


def case_two_folders() -> None:
    a = uid(0x10D)
    b = uid(0x10E)
    write_jsonl(session_file("two_folders", a, "-Users-fake-dev-widget"), [
        user(0xD1, None, a, 0, "Widget work"),
        assistant(0xD2, uid(0xD1), a, 1, [text("Sure.")], "msg_md1"),
    ])
    # A folder's slug is its working directory, so this session's records name the secretproj path.
    secret_cwd = "/Users/fake/dev/secretproj"
    write_jsonl(session_file("two_folders", b, "-Users-fake-dev-secretproj"), [
        user(0xD3, None, b, 2, "Secret work", cwd=secret_cwd),
        assistant(0xD4, uid(0xD3), b, 3, [text("Quietly.")], "msg_md2", cwd=secret_cwd),
    ])


def case_empty_turn() -> None:
    s = uid(0x10F)
    write_jsonl(session_file("empty_turn", s), [
        user(0xE1, None, s, 0, ""),
        user(0xE2, uid(0xE1), s, 1, [text("   ")]),
        assistant(0xE3, uid(0xE2), s, 2, [text("")], "msg_me1"),
        user(0xE4, uid(0xE3), s, 3, "Real question"),
        assistant(0xE5, uid(0xE4), s, 4, [text("Real answer.")], "msg_me2"),
    ])


LONG_PROMPT = (
    "Please walk me through the uploader's retry policy, the jitter it applies between attempts, "
    "and where the ceiling on attempts is enforced in the code"
)


def case_titles() -> None:
    t1, t2, t3 = uid(0x110), uid(0x111), uid(0x112)
    write_jsonl(session_file("titles", t1), [
        {"type": "summary", "summary": "Summary title", "leafUuid": uid(0xF2)},
        user(0xF1, None, t1, 0, "First prompt one"),
        assistant(0xF2, uid(0xF1), t1, 1, [text("Reply one.")], "msg_mf1"),
    ])
    title_file = session_file("titles", t1).parent / t1 / "custom-title.json"
    title_file.parent.mkdir(parents=True, exist_ok=True)
    title_file.write_text(json.dumps({"title": "My custom title"}) + "\n", encoding="utf-8")
    write_jsonl(session_file("titles", t2), [
        user(0xF3, None, t2, 2, "First prompt two"),
        assistant(0xF4, uid(0xF3), t2, 3, [text("Reply two.")], "msg_mf2"),
        {"type": "ai-title", "aiTitle": "AI made title", "sessionId": t2},
        {"type": "custom-title", "customTitle": "Custom via record", "sessionId": t2},
    ])
    write_jsonl(session_file("titles", t3), [
        user(0xF5, None, t3, 4, LONG_PROMPT),
        assistant(0xF6, uid(0xF5), t3, 5, [text("Reply three.")], "msg_mf3"),
    ])


def case_no_title() -> None:
    # Nothing to title from: the only user record is a slash command.
    s = uid(0x116)
    write_jsonl(session_file("no_title", s), [
        user(0x1A1, None, s, 0, "<command-name>/status</command-name>\n<command-message>status</command-message>"),
        assistant(0x1A2, uid(0x1A1), s, 1, [text("Status shown.")], "msg_nt1"),
    ])


def case_since_window() -> None:
    old, new = uid(0x114), uid(0x115)
    old_base = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)
    new_base = datetime(2026, 10, 10, 9, 0, 0, tzinfo=timezone.utc)
    write_jsonl(session_file("since_window", old), [
        user(0x101, None, old, 0, "Old question", base=old_base),
        assistant(0x102, uid(0x101), old, 1, [text("Old answer.")], "msg_old", base=old_base),
    ])
    write_jsonl(session_file("since_window", new), [
        user(0x103, None, new, 0, "New question", base=new_base),
        assistant(0x104, uid(0x103), new, 1, [text("New answer.")], "msg_new", base=new_base),
    ])


def case_agentic_reply() -> None:
    # One agentic turn written the way Claude Code writes it: one record per content block, a new
    # message.id per API call, tool calls and their results between the narration, meta records
    # mid-turn, a stop-hook system record at the end. Then a second turn that a prompt typed
    # mid-turn (a queued_command attachment) splits in two.
    s = uid(0x117)
    write_jsonl(session_file("agentic_reply", s), [
        user(0x1B1, None, s, 0, "Fix the flaky upload test"),
        assistant(0x1B2, uid(0x1B1), s, 1, [text("Let me look at the test first.")], "msg_ag1", stop="tool_use"),
        assistant(0x1B3, uid(0x1B2), s, 2, [tool_use("Read", {"file_path": f"{CWD}/tests/test_upload.py"}, "toolu_ag1")],
                  "msg_ag1", stop="tool_use"),
        attachment(0x1B4, uid(0x1B3), s, 3, {"type": "total_tokens_reminder", "text": "Tokens used: 1200"}),
        user(0x1B5, uid(0x1B4), s, 4, tool_result("def test_upload():\n    sleep(0.1)", "toolu_ag1")),
        {"type": "custom-title", "customTitle": "Flaky upload test", "sessionId": s},
        assistant(0x1B6, uid(0x1B5), s, 5, [thinking("It sleeps instead of waiting")], "msg_ag2", stop="tool_use"),
        assistant(0x1B7, uid(0x1B6), s, 6, [text("The test sleeps instead of waiting for the upload.")], "msg_ag2",
                  stop="tool_use"),
        assistant(0x1B8, uid(0x1B7), s, 7, [tool_use("Edit", {"file_path": f"{CWD}/tests/test_upload.py"}, "toolu_ag2")],
                  "msg_ag2", stop="tool_use"),
        user(0x1B9, uid(0x1B8), s, 8, tool_result("The file has been updated.", "toolu_ag2")),
        assistant(0x1BA, uid(0x1B9), s, 9, [text("Fixed: it now waits on the upload event.")], "msg_ag3", stop="end_turn"),
        assistant(0x1BB, uid(0x1BA), s, 10, [text("The test passed ten runs in a row.")], "msg_ag3", stop="end_turn"),
        system(0x1BC, uid(0x1BB), s, 11, "stop_hook_summary"),
        user(0x1BD, uid(0x1BC), s, 12, "Thanks. Now the lint error?"),
        assistant(0x1BE, uid(0x1BD), s, 13, [text("Checking the lint output.")], "msg_ag4", stop="tool_use"),
        assistant(0x1BF, uid(0x1BE), s, 14, [tool_use("Bash", {"command": "ruff check ."}, "toolu_ag3")], "msg_ag4",
                  stop="tool_use"),
        user(0x1C0, uid(0x1BF), s, 15, tool_result("app.py:12: E501 line too long", "toolu_ag3")),
        attachment(0x1C1, uid(0x1C0), s, 16, {"type": "queued_command", "prompt": "also fix the README typo",
                                              "commandMode": "prompt", "origin": {"kind": "human"}}),
        assistant(0x1C2, uid(0x1C1), s, 17, [text("Line 12 was too long; it is wrapped now.")], "msg_ag5", stop="tool_use"),
        assistant(0x1C3, uid(0x1C2), s, 18, [text("And the README typo is fixed.")], "msg_ag5", stop="end_turn"),
    ])


OPEN_REPLY_SESSION = 0x118


def case_open_reply() -> None:
    # A turn still running when the reader gets there: the last record asked for a tool and nothing
    # answered yet. ``open_reply_tail`` holds the records that arrive later; a test appends them.
    s = uid(OPEN_REPLY_SESSION)
    write_jsonl(session_file("open_reply", s), [
        user(0x1D1, None, s, 0, "Run the slow suite"),
        assistant(0x1D2, uid(0x1D1), s, 1, [text("Starting the slow suite.")], "msg_or1", stop="tool_use"),
        assistant(0x1D3, uid(0x1D2), s, 2, [tool_use("Bash", {"command": "pytest -m slow"}, "toolu_or1")], "msg_or1",
                  stop="tool_use"),
    ])
    write_jsonl(session_file("open_reply_tail", s), [
        user(0x1D4, uid(0x1D3), s, 3, tool_result("12 passed", "toolu_or1")),
        assistant(0x1D5, uid(0x1D4), s, 4, [text("All 12 slow tests passed.")], "msg_or2", stop="end_turn"),
    ])


def case_harness_trailing() -> None:
    # Harness spans AFTER typed text: stripped when a complete span runs from the start of a line to the
    # end of the text; an inline mention or a span with typed text after it is the person's own words.
    s = uid(0x119)
    write_jsonl(session_file("harness_trailing", s), [
        user(0x1E1, None, s, 0, "Fix the flaky test\n\n<system-reminder>\nThe user opened tests/test_upload.py.\n</system-reminder>"),
        assistant(0x1E2, uid(0x1E1), s, 1, [text("On it.")], "msg_ht1"),
        user(0x1E3, uid(0x1E2), s, 2, "Why does `<command-name>` show up in my rows?"),
        user(0x1E4, uid(0x1E3), s, 3, "Keep this:\n<system-reminder>pasted in the middle</system-reminder>\nand this line too"),
        user(0x1E5, uid(0x1E4), s, 4, [text("Ship it"), text("<system-reminder>\nThe todo list is empty.\n</system-reminder>")]),
        user(0x1E6, uid(0x1E5), s, 5, "Two notes\n<system-reminder>a</system-reminder>\n<local-command-stdout>b</local-command-stdout>\n"),
        user(0x1E7, uid(0x1E6), s, 6, 'Look here\n<ide_selection path="app.py">\nline 3\n</ide_selection>'),
        user(0x1E8, uid(0x1E7), s, 7, "Is it fine to end a note with <system-reminder>x</system-reminder>"),
        user(0x1E9, uid(0x1E8), s, 8, "a\n<system-reminder>1</system-reminder>\nkeep me\n<system-reminder>2</system-reminder>"),
    ])


def case_reply_edges() -> None:
    # Where a reply must end and where it must not: a system record ends it; sub-agent records inside the
    # main file do not; a record holding text AND a Topos call starts an excluded row of its own, so the
    # narration before it stays shareable.
    s = uid(0x11A)
    sub = {"isSidechain": True, "agentId": "e1f2"}
    write_jsonl(session_file("reply_edges", s), [
        user(0x201, None, s, 0, "Check the deploy"),
        assistant(0x202, uid(0x201), s, 1, [text("Looking at the deploy log.")], "msg_re1", stop="tool_use"),
        system(0x203, uid(0x202), s, 2, "api_error"),
        assistant(0x204, uid(0x203), s, 3, [text("Retried: the deploy is green.")], "msg_re2", stop="end_turn"),
        user(0x205, uid(0x204), s, 4, "Now the release notes"),
        assistant(0x206, uid(0x205), s, 5, [text("Drafting them with a helper.")], "msg_re3", stop="tool_use"),
        assistant(0x207, uid(0x206), s, 6, [tool_use("Agent", {"prompt": "draft notes"}, "toolu_re1")], "msg_re3",
                  stop="tool_use"),
        user(0x208, uid(0x207), s, 7, "draft notes", **sub),
        assistant(0x209, uid(0x208), s, 8, [text("Helper draft ready.")], "msg_re4", stop="end_turn", **sub),
        user(0x20A, uid(0x209), s, 9, tool_result("Notes drafted.", "toolu_re1")),
        assistant(0x20B, uid(0x20A), s, 10, [text("The notes are in RELEASE.md.")], "msg_re5", stop="end_turn"),
        user(0x20C, uid(0x20B), s, 11, "What did I ship last week?"),
        assistant(0x20D, uid(0x20C), s, 12, [text("Let me check.")], "msg_re6", stop="tool_use"),
        assistant(0x20E, uid(0x20D), s, 13,
                  [text("Asking your Topos now."),
                   tool_use("mcp__plugin_topos_topos__query_scope", {"scope_id": "work_context:read"}, "toolu_re2")],
                  "msg_re7", stop="tool_use"),
        user(0x20F, uid(0x20E), s, 14, tool_result('{"data": {}}', "toolu_re2")),
        assistant(0x210, uid(0x20F), s, 15, [text("You shipped the uploader.")], "msg_re8", stop="end_turn"),
    ])


QUEUED_SESSION = 0x120
QUEUED_FORK = 0x121


def queued(n: int, parent: str | None, session: str, i: int, prompt, *, mode: str = "prompt", origin=None,
           source: str | None = None, sidechain: bool = False, **extra) -> dict:
    # A prompt delivered into a running turn, shaped as Claude Code writes it: the fields live on the attachment.
    payload = {"type": "queued_command", "prompt": prompt, "commandMode": mode, "timestamp": ts(i)}
    if origin is not None:
        payload["origin"] = origin
    if source is not None:
        payload["source_uuid"] = source
    payload.update(extra)
    record = attachment(n, parent, session, i, payload)
    record["isSidechain"] = sidechain
    return record


def case_queued_prompts() -> None:
    # Prompts queued while the agent works. Only the person's own (commandMode prompt, origin.kind human, not
    # isMeta) become user rows; task notifications, peer messages, isMeta and origin-less entries stay out.
    # A source_uuid that names a typed record in the file is that record's copy, before or after it.
    s = uid(QUEUED_SESSION)
    human = {"kind": "human"}
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}
    original = [
        user(0x221, None, s, 0, "Refactor the parser"),
        assistant(0x222, uid(0x221), s, 1, [text("Reading the parser first.")], "msg_q1", stop="tool_use"),
        assistant(0x223, uid(0x222), s, 2, [tool_use("Read", {"file_path": f"{CWD}/parser.py"}, "toolu_q1")], "msg_q1",
                  stop="tool_use"),
        queued(0x224, uid(0x223), s, 3,
               f"Also update {CWD}/README.md\n<system-reminder>The user sent this while you worked.</system-reminder>",
               origin=human, source="qsrc-0001", humanTurn=True),
    ]
    write_jsonl(session_file("queued_prompts", s), original + [
        user(0x225, uid(0x224), s, 4, tool_result("def parse(): ...", "toolu_q1")),
        assistant(0x226, uid(0x225), s, 5, [text("Parser split into two modules; README updated.")], "msg_q2",
                  stop="tool_use"),
        queued(0x227, uid(0x226), s, 6, "<task-notification>build done</task-notification>", mode="task-notification"),
        queued(0x228, uid(0x227), s, 7, "<task-notification>tests done</task-notification>", mode="task-notification",
               origin={"kind": "task-notification"}),
        queued(0x229, uid(0x228), s, 8, "Peer lane says the build is green", origin={"kind": "peer", "from": "lane-b"},
               isMeta=True),
        queued(0x22A, uid(0x229), s, 9, "Meta text in a human queue entry", origin=human, isMeta=True),
        queued(0x22B, uid(0x22A), s, 10, "<ci-monitor-event>checks green</ci-monitor-event>", source="qsrc-0002"),
        queued(0x22C, uid(0x22B), s, 11, [text("What does this diagram show?"), image], origin=human),
        queued(0x22D, uid(0x22C), s, 12, [image], origin=human, source="qsrc-0004"),
        queued(0x22E, uid(0x22D), s, 13, "<bash-input>ls</bash-input>", origin=human, source="qsrc-0005"),
        queued(0x22F, uid(0x22E), s, 14, "Refactor the parser", origin=human, source=uid(0x221)),
        queued(0x230, uid(0x22F), s, 15, {"text": "a prompt that is neither text nor blocks"}, origin=human),
        queued(0x231, uid(0x230), s, 16, [{"type": "document", "source": {"type": "text", "data": "notes"}}],
               origin=human),
        queued(0x232, uid(0x231), s, 17, "Queued inside a sub-agent", origin=human, sidechain=True),
        queued(0x233, uid(0x232), s, 18, "Ship it", origin=human, source=uid(0x234)),
        user(0x234, uid(0x233), s, 19, "Ship it"),
        assistant(0x235, uid(0x234), s, 20, [text("Shipped.")], "msg_q3", stop="end_turn"),
        queued(0x236, uid(0x235), s, 21, "   ", origin=human, source="qsrc-0006"),
        # A typed record with no uuid: a queued entry with no source_uuid is not its copy.
        {k: v for k, v in user(0x238, uid(0x236), s, 22, "No uuid on this one").items() if k != "uuid"},
        # A source_uuid naming an assistant record is no typed copy; the person's prompt stays a row.
        queued(0x239, None, s, 23, "Also check the tests", origin=human, source=uid(0x222)),
        # A list with a stray non-block item is not text and image blocks.
        queued(0x23A, uid(0x239), s, 24, ["stray", text("Stray item beside a block")], origin=human),
        # A queued entry with no uuid cannot be a row.
        {k: v for k, v in queued(0x23B, uid(0x23A), s, 25, "Queued with no uuid", origin=human).items()
         if k != "uuid"},
        # Text on both sides of an image: the image adds nothing between them.
        queued(0x23C, uid(0x23A), s, 26, [text("Before the image"), image, text("after the image")], origin=human),
        # A peer entry whose source_uuid names a typed record is counted by its origin, not as a copy.
        queued(0x23D, uid(0x23C), s, 27, "Peer echo of the first prompt", origin={"kind": "peer"}, isMeta=True,
               source=uid(0x221)),
    ])
    f = uid(QUEUED_FORK)
    write_jsonl(session_file("queued_prompts", f), [dict(r, sessionId=f) for r in (original[0], original[3])] + [
        queued(0x237, uid(0x224), f, 22, "Fork-only follow-up", origin=human, source="qsrc-0007"),
    ])


def main() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    for build in (
        case_plain,
        case_tool_result_in_user,
        case_thinking_and_tool_use,
        case_harness_injected,
        case_compaction,
        case_fork_copy,
        case_side_files,
        case_subagent_taint,
        case_topos_call_then_three,
        case_bash_touches_topos,
        case_secrets,
        case_two_folders,
        case_empty_turn,
        case_titles,
        case_no_title,
        case_since_window,
        case_agentic_reply,
        case_open_reply,
        case_harness_trailing,
        case_reply_edges,
        case_queued_prompts,
    ):
        build()
    print(f"wrote {sum(1 for _ in ROOT.rglob('*') if _.is_file())} files under {ROOT}")


if __name__ == "__main__":
    main()
