"""Shared helpers for the AI coding-tool readers (CONTRACTS §2.3, §2.4, §2.7, §2.7b).

protects: the scrub table (every secret shape, every home-path form, hostnames),
the Claude Code keyboard-origin rule as a data table, the harness-tag rule with
leading system-reminder spans stripped rather than dropped, the taint rule, and
the §2.2 record builder's empty guard.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from topos.ingestion.parsers.ai_coding_tool import common
from topos.ingestion.parsers.ai_coding_tool.common import (
    CLAUDE_CODE_KEYBOARD_ORIGIN,
    HARNESS_TAGS,
    Ledger,
    is_harness_text,
    make_record,
    origin_violation,
    scrub_text,
    strip_leading_system_reminders,
    taints,
)


# ---------------------------------------------------------------------------
# §2.4 scrub (aligned with spike B, P0.2)
# ---------------------------------------------------------------------------

NO_IDENT = {"hostnames": (), "usernames": ()}


def test_exact_node_secrets_are_replaced_first_and_counted_under_one_name() -> None:
    key = "3f9c2b7e1a4d5c6b8e7f0a1b2c3d4e5f"  # gitleaks:allow
    owner = "ok_" + "a1" * 24
    label = "tpa_claude-code." + "b2" * 20
    text = f"TOPOS_KEY is {key}; owner {owner}; label {label}; again {key}"
    out, counts = scrub_text(text, secrets=(key, owner, label), **NO_IDENT)
    assert key not in out and owner not in out and label not in out
    assert out == "TOPOS_KEY is [TOPOS_SECRET]; owner [TOPOS_SECRET]; label [TOPOS_SECRET]; again [TOPOS_SECRET]"
    assert counts == {"topos_secret": 4}


def test_exact_secrets_below_the_spike_floor_are_not_used_as_patterns() -> None:
    out, counts = scrub_text("the word retry appears here", secrets=("retry", "short1234567890"), **NO_IDENT)
    assert out == "the word retry appears here" and counts == {}


@pytest.mark.parametrize(
    "name,secret",
    [
        ("topos_node_key", "tpn_" + "c3" * 20),
        ("topos_node_key", "tpn_" + "c3" * 9),
        ("topos_agent_label", "tpa_codex." + "d4" * 20),
        ("topos_client_token", "tpk_d4e5f6a7b8c9d0e1f2a3b4c5"),  # gitleaks:allow
        ("topos_owner_key", "ok_" + "e5" * 10),
        ("topos_owner_key", "ok_" + "e5" * 24),
        ("anthropic_key", "sk-ant-api03-" + "A" * 40),
        ("openai_key", "sk-proj" + "B" * 20),
        ("stripe_key", "sk_live_" + "C" * 24),
        ("stripe_key", "rk_test_" + "C" * 24),
        ("stripe_key", "whsec_" + "D" * 32),
        ("github_token", "ghp_" + "C" * 36),
        ("github_token", "gho_" + "D" * 40),
        ("github_token", "github_pat_" + "E" * 22 + "_" + "F" * 59),
        ("gitlab_token", "glpat-" + "G" * 20),
        ("aws_access_key", "AKIADEFGHIJKLMNOPQRS"),  # gitleaks:allow
        ("aws_access_key", "ASIADEFGHIJKLMNOPQRS"),  # gitleaks:allow
        ("slack_token", "xoxb-12345678ab-abcdefghijklmnop"),  # gitleaks:allow
        ("slack_token", "xoxe-" + "H" * 24),
        ("slack_webhook", "https://hooks.slack.com/services/TABCD1234/BABCD12345/" + "I" * 24),
        ("google_api_key", "AIza" + "E" * 35),
        ("google_api_key", "GOCSPX-" + "J" * 28),
        ("google_api_key", "ya29." + "K" * 100),
        ("npm_hf_token", "npm_" + "L" * 36),
        ("npm_hf_token", "hf_" + "M" * 34),
        ("sendgrid_key", "SG." + "N" * 22 + "." + "O" * 43),
        ("digitalocean_token", "dop_v1_" + "ab" * 32),
        ("vault_token", "hvs." + "P" * 90),
        ("age_secret_key", "AGE-SECRET-KEY-1" + "Q" * 58),
        ("mapbox_token", "pk.eyJ1Ijoi" + "R" * 60 + "." + "S" * 22),
        ("supabase_key", "sb_secret_" + "T" * 40),
        ("twilio_sid", "SK" + "ab" * 16),
        ("jwt", "eyJ" + "U" * 20 + ".eyJ" + "V" * 30 + "." + "W" * 43),
        ("private_key_block", "-----BEGIN RSA PRIVATE KEY-----\nMIIEfake\n-----END RSA PRIVATE KEY-----"),  # gitleaks:allow
        ("private_key_block", "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"),
        ("private_key_block", "-----BEGIN OPENSSH PRIVATE KEY BLOCK-----\nabc\n-----END OPENSSH PRIVATE KEY BLOCK-----"),
    ],
)
def test_each_secret_shape_is_scrubbed_and_counted_by_rule_name(name: str, secret: str) -> None:
    out, counts = scrub_text(f"before {secret} after", **NO_IDENT)
    assert secret not in out
    placeholder = "[TOPOS_SECRET]" if name.startswith("topos_") else "[SECRET]"
    assert out == f"before {placeholder} after"
    assert counts == {name: 1}


@pytest.mark.parametrize(
    "name,text,expected",
    [
        ("bearer_token", "Authorization: Bearer " + "F" * 30 + " end", "Authorization: Bearer [SECRET] end"),
        ("bearer_token", "curl -H 'Authorization: Basic YWRtaW46c2VjcmV0cGFzc3dvcmQ=' x", "curl -H 'Authorization: Basic [SECRET]' x"),  # gitleaks:allow
        ("api_key_header", 'headers = {"X-API-Key": "' + "G" * 32 + '"}', 'headers = {"X-API-Key": "[SECRET]"}'),
        ("aws_secret_key", "aws_secret_access_key = " + "H" * 40, "aws_secret_access_key = [SECRET]"),
        ("env_assignment", "DB_PASSWORD=hunter2hunter2\nexport GH_TOKEN=abc123\nLOG_LEVEL=keep",
         "DB_PASSWORD=[SECRET]\nexport GH_TOKEN=[SECRET]\nLOG_LEVEL=keep"),
        ("env_assignment", 'CLOUDFLARE_API_TOKEN="' + "I" * 40 + '"', 'CLOUDFLARE_API_TOKEN="[SECRET]"'),
        ("env_assignment", "TOPOS_KEY=" + "ab" * 16, "TOPOS_KEY=[SECRET]"),
        ("code_literal", 'password = "' + "J" * 20 + '"', 'password = "[SECRET]"'),
        ("code_literal", '{"token": "' + "ab" * 32 + '", "expires_in": 3600}', '{"token": "[SECRET]", "expires_in": 3600}'),
        ("yaml_unquoted", "openweather:\n  api_key: " + "K" * 32 + "\n  units: metric", "openweather:\n  api_key: [SECRET]\n  units: metric"),
        ("url_credential", "DATABASE_URL_X=postgres://app_user:" + "L" * 18 + "@db.example:5432/app",
         "DATABASE_URL_X=postgres://app_user:[SECRET]@db.example:5432/app"),
        ("url_credential", "git clone https://deploy:" + "M" * 16 + "@git.example/acme/infra.git",
         "git clone https://deploy:[SECRET]@git.example/acme/infra.git"),
    ],
)
def test_contextual_rules_keep_the_name_and_drop_the_value(name: str, text: str, expected: str) -> None:
    out, counts = scrub_text(text, **NO_IDENT)
    assert out == expected
    assert set(counts) == {name}


def test_a_vendor_shape_on_an_env_line_is_counted_once_under_the_shape() -> None:
    out, counts = scrub_text("OPENAI_API_KEY=sk-proj" + "B" * 20, **NO_IDENT)
    assert out == "OPENAI_API_KEY=[SECRET]"
    assert counts == {"openai_key": 1}


def test_all_four_home_path_forms_and_hostnames_are_scrubbed() -> None:
    text = (
        "/Users/fake/dev/widget/app.py and /home/fake/dev/widget and -Users-fake-dev-widget "
        "and /private/tmp/claude-501/-Users-fake-dev-widget/scratch on fakebox.local or fakebox-7"
    )
    out, counts = scrub_text(text, hostnames=("fakebox-7",), usernames=())
    assert out == (
        "~/dev/widget/app.py and ~/dev/widget and -~-dev-widget "
        "and /private/tmp/claude-501/-~-dev-widget/scratch on [HOST] or [HOST]"
    )
    assert "fake" not in out
    assert counts == {"home_path": 2, "home_slug": 2, "hostname_local": 1, "hostname_machine": 1}


def test_windows_home_and_shell_prompt_user_are_scrubbed() -> None:
    out, counts = scrub_text(
        r"C:\Users\fake\dev\widget and fake@fakebox-7 widget % make test and /Users/Shared/x",
        hostnames=("fakebox-7",), usernames=("fake",),
    )
    assert out == r"~\dev\widget and ~@[HOST] widget % make test and /Users/Shared/x"
    assert counts == {"home_windows": 1, "user_prompt": 1, "hostname_machine": 1}


def test_scrub_table_is_data_one_row_per_rule() -> None:
    names = [rule.name for rule in common.SECRET_SHAPE_RULES]
    assert names == [
        "private_key_block",
        "topos_node_key",
        "topos_agent_label",
        "topos_client_token",
        "topos_owner_key",
        "anthropic_key",
        "openai_key",
        "stripe_key",
        "github_token",
        "gitlab_token",
        "aws_access_key",
        "aws_secret_key",
        "slack_token",
        "slack_webhook",
        "google_api_key",
        "npm_hf_token",
        "sendgrid_key",
        "digitalocean_token",
        "vault_token",
        "age_secret_key",
        "mapbox_token",
        "supabase_key",
        "twilio_sid",
        "jwt",
        "bearer_token",
        "api_key_header",
        "env_assignment",
        "code_literal",
        "yaml_unquoted",
        "url_credential",
    ]
    assert [rule.name for rule in common.PATH_RULES] == ["home_path", "home_windows", "home_slug", "hostname_local"]
    for rule in common.SECRET_SHAPE_RULES + common.PATH_RULES:
        assert rule.name and rule.pattern.pattern and rule.replacement


@pytest.mark.parametrize(
    "text",
    [
        "Check the uploader's retry loop; it is ok_ish but tokens like skate are fine.",
        "commit " + "ab" * 20 + "\nAuthor: CI Bot <ci@example.com>\n\n    fix: tighten relay timeout",
        'session_id = "0000000a-0000-4000-8000-00000000000a"',
        'assert hashlib.md5(data).hexdigest() == "' + "cd" * 16 + '"',
        "ef" * 32 + "  acme-1.4.2-py3-none-any.whl",
        "See https://github.com/acme/widgets/pull/412 for the discussion.",
        "Digest: sha256:" + "12" * 32,
        '      "integrity": "sha512-' + "A" * 86 + '==",',
        'build: {"version": "1.4.2", "built_at": "2026-10-15T14:02:11Z"}',
        "upstream app { server 10.0.0.12:8080; }",
        "  --bg: #1f2a44; --fg: #e6e8ef;",
        "================ 412 passed, 3 skipped in 48.21s ================",
        "Set OPENAI_API_KEY in your shell before running the tests.",
        "import json\nimport os\nfrom pathlib import Path\n\nROOT = Path(__file__).resolve().parent",
        'ETag: "' + "Qm" * 24 + '"',
        "Retry the checkpoint on SQLITE_BUSY instead of failing the sync; readers hold the mark for under 5 ms.",
        "curl -s http://127.0.0.1:8743/health | jq .status",
        "Tokens: expire after 3600 seconds; the token endpoint returns JSON.",
        "/Users/Shared/Relocated Items stays; so does the password policy page.",
    ],
)
def test_spike_negatives_survive_untouched(text: str) -> None:
    out, counts = scrub_text(text, **NO_IDENT)
    assert out == text
    assert counts == {}


def test_the_scrub_covers_the_full_text_not_the_first_8000_chars() -> None:
    # The model privacy filter truncates at 8,000 chars (privacy_filter.py:152-157);
    # the deterministic pass must not share that ceiling.
    filler = ("def retry(n):\n    return n * 2\n" * 400)
    assert len(filler) > 8000
    key = "3f9c2b7e1a4d5c6b8e7f0a1b2c3d4e5f"  # gitleaks:allow
    text = filler + "AKIADEFGHIJKLMNOPQRS at /Users/fake/x " + key + "\n" + filler  # gitleaks:allow
    out, counts = scrub_text(text, secrets=(key,), **NO_IDENT)
    assert "AKIADEFGHIJKLMNOPQRS" not in out and key not in out and "/Users/fake" not in out  # gitleaks:allow
    assert counts == {"aws_access_key": 1, "home_path": 1, "topos_secret": 1}
    assert len(out) > 8000


def test_node_secrets_reads_env_keys_and_label_files_from_the_given_home(tmp_path) -> None:
    from topos.ingestion.parsers.ai_coding_tool.common import node_secrets

    home = tmp_path / "topos-home"
    (home / "agents").mkdir(parents=True)
    (home / ".env").write_text(
        "OPENAI_API_KEY=sk-not-a-node-secret-" + "Z" * 20 + "\n"
        "TOPOS_KEY=3f9c2b7e1a4d5c6b8e7f0a1b2c3d4e5f\n"  # gitleaks:allow
        'export TOPOS_OWNER_KEY="ok_' + "a1" * 24 + '"\n'
        "TOPOS_NODE_KEY = tpn_" + "c3" * 20 + "\n"
        "TOPOS_SHORT=abc\n",
        encoding="utf-8",
    )
    (home / "agents" / "claude-code.token").write_text("tpa_claude-code." + "b2" * 20 + "\n", encoding="utf-8")
    (home / "agents" / "codex.token").write_text("\n", encoding="utf-8")
    (home / "agents" / "notes.txt").write_text("tpa_not-a-label." + "d4" * 20 + "\n", encoding="utf-8")
    secrets = node_secrets(home)
    assert secrets == (
        "3f9c2b7e1a4d5c6b8e7f0a1b2c3d4e5f",
        "ok_" + "a1" * 24,
        "tpn_" + "c3" * 20,
        "tpa_claude-code." + "b2" * 20,
    )
    out, counts = scrub_text("key " + secrets[0] + " label " + secrets[3], secrets=secrets, **NO_IDENT)
    assert out == "key [TOPOS_SECRET] label [TOPOS_SECRET]" and counts == {"topos_secret": 2}


def test_node_secrets_is_empty_when_the_home_is_missing(tmp_path) -> None:
    from topos.ingestion.parsers.ai_coding_tool.common import node_secrets

    assert node_secrets(tmp_path / "nowhere") == ()


# ---------------------------------------------------------------------------
# §2.3 harness text and the keyboard-origin rule (spike A)
# ---------------------------------------------------------------------------


def test_leading_system_reminder_spans_are_stripped_not_dropped() -> None:
    text = "<system-reminder>\nctx\n</system-reminder>\n<system-reminder>more</system-reminder>\nPlease rename the module"
    stripped, n = strip_leading_system_reminders(text)
    assert stripped == "Please rename the module"
    assert n == 2
    assert is_harness_text(text) is False


def test_a_bare_system_reminder_is_harness_text() -> None:
    assert is_harness_text("<system-reminder>\nThe file was modified.\n</system-reminder>") is True
    assert is_harness_text("  \n<system-reminder>x</system-reminder>\n  ") is True


@pytest.mark.parametrize(
    "text",
    [
        "<task-notification>agent-7 finished</task-notification>",
        "<command-name>/clear</command-name>",
        "<command-message>clear</command-message>",
        "<local-command-stdout>main</local-command-stdout>",
        "<local-command-caveat>Caveat</local-command-caveat>",
        "<bash-input>ls</bash-input>",
        "<ci-monitor-event>run 42</ci-monitor-event>",
        "<artifact-view-context>x</artifact-view-context>",
        "<create-pr-command>open</create-pr-command>",
        "<cross-session-message>hi</cross-session-message>",
        "<ide_opened_file>a.py</ide_opened_file>",
        "<ide_selection>x</ide_selection>",
        "[Request interrupted by user for tool use]",
        "[Request interrupted by user]",
        "<system-reminder>a</system-reminder>\n<command-name>/x</command-name>",
    ],
)
def test_harness_tags_are_recognised_at_the_start_only(text: str) -> None:
    assert is_harness_text(text) is True
    assert is_harness_text("I typed this. " + text) is False


def test_harness_tag_table_matches_spike_a() -> None:
    assert HARNESS_TAGS == (
        "task-notification",
        "command-name",
        "command-message",
        "local-command-stdout",
        "local-command-caveat",
        "bash-input",
        "ci-monitor-event",
        "artifact-view-context",
        "create-pr-command",
        "cross-session-message",
        "system-reminder",
        "ide_",
    )


def _user(**fields) -> dict:
    record = {"type": "user", "uuid": "u-1", "message": {"role": "user", "content": "hi"}}
    record.update(fields)
    return record


@pytest.mark.parametrize(
    "fields,tag",
    [
        ({}, None),
        ({"userType": "external", "promptSource": "sdk", "entrypoint": "claude-desktop"}, None),
        ({"origin": {"kind": "human"}, "turnOrigin": "human"}, None),
        ({"isMeta": True}, "isMeta"),
        ({"isCompactSummary": True}, "isCompactSummary"),
        ({"isVisibleInTranscriptOnly": True}, "isVisibleInTranscriptOnly"),
        ({"isSidechain": True}, "isSidechain"),
        ({"origin": {"kind": "peer"}}, "origin.kind=peer"),
        ({"origin": {"kind": "task-notification"}}, "origin.kind=task-notification"),
        ({"turnOrigin": "peer"}, "turnOrigin=peer"),
        ({"promptSource": "system"}, "promptSource=system"),
        ({"isMeta": True, "origin": {"kind": "peer"}, "turnOrigin": "peer"}, "isMeta"),
    ],
)
def test_keyboard_origin_rule_is_a_data_table_tolerant_of_absent_fields(fields: dict, tag) -> None:
    assert origin_violation(_user(**fields), CLAUDE_CODE_KEYBOARD_ORIGIN) == tag


def test_keyboard_origin_rule_rows_are_the_spike_a_fields_in_order() -> None:
    assert [rule.field for rule in CLAUDE_CODE_KEYBOARD_ORIGIN] == [
        "isMeta",
        "isCompactSummary",
        "isVisibleInTranscriptOnly",
        "isSidechain",
        "origin.kind",
        "turnOrigin",
        "promptSource",
    ]


# ---------------------------------------------------------------------------
# §2.7 taint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,command",
    [
        ("mcp__plugin_topos_topos__query_scope", None),
        ("mcp__topos__query_scope", None),
        ("mcp__topos__remote__topos-gmail__search_gmail_messages", None),
        ("mcp__acme__remote__gdrive_search", None),
        ("Topos", None),
        ("Bash", "curl --unix-socket ~/.topos/engine.sock http://x/healthcheck"),
        ("Bash", "sqlite3 /Users/fake/.topos/database.db 'select 1'"),
        ("Bash", "ls engine.sock"),
        ("Bash", "topos-node status --json"),
        (None, "cat ~/.topos/.env"),
    ],
)
def test_topos_surfaces_taint(name, command) -> None:
    assert taints(name, command) is True


@pytest.mark.parametrize(
    "name,command",
    [
        ("Bash", "ls -la"),
        ("Read", None),
        ("mcp__github__search_code", None),
        ("mcp__slack__read_channel", None),
        (None, None),
        ("Bash", "git log --oneline"),
    ],
)
def test_other_surfaces_do_not_taint(name, command) -> None:
    assert taints(name, command) is False


# ---------------------------------------------------------------------------
# §2.2 record builder and the ledger
# ---------------------------------------------------------------------------


def test_make_record_builds_the_flat_record_with_millisecond_utc_stamps() -> None:
    created = datetime(2026, 10, 1, 9, 0, 0, 250_000, tzinfo=timezone.utc)
    record = make_record(
        tool="claude_code",
        record_uuid="abc",
        session_id="sess-1",
        role="user",
        content="  hello  ",
        created_at=created,
        turn_index=3,
        title="T",
        origin="keyboard",
        share_excluded=False,
        timestamp_source="record",
    )
    assert record == {
        "id": "claude_code:abc",
        "thread_id": "sess-1",
        "role": "user",
        "content": "hello",
        "created_at": "2026-10-01T09:00:00.250Z",
        "_metadata": {
            "tool": "claude_code",
            "session_id": "sess-1",
            "turn_index": 3,
            "conversation_title": "T",
            "origin": "keyboard",
            "share_excluded": False,
            "timestamp_source": "record",
        },
    }


def test_make_record_carries_model_slug_only_when_given() -> None:
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = make_record(
        tool="codex", record_uuid="x", session_id="s", role="assistant", content="ok",
        created_at=created, turn_index=0, title="T", origin="assistant", share_excluded=True,
        timestamp_source="session", model_slug="gpt-5",
    )
    assert record["_metadata"]["model_slug"] == "gpt-5"
    assert record["_metadata"]["share_excluded"] is True


def test_make_record_refuses_empty_content() -> None:
    with pytest.raises(ValueError, match="empty"):
        make_record(
            tool="claude_code", record_uuid="x", session_id="s", role="user", content="  \n ",
            created_at=datetime(2026, 10, 1, tzinfo=timezone.utc), turn_index=0, title="T",
            origin="keyboard", share_excluded=False, timestamp_source="record",
        )


def test_make_record_refuses_unknown_tool_and_role() -> None:
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        make_record(tool="chatgpt", record_uuid="x", session_id="s", role="user", content="a",
                    created_at=created, turn_index=0, title="T", origin="keyboard",
                    share_excluded=False, timestamp_source="record")
    with pytest.raises(ValueError):
        make_record(tool="cursor", record_uuid="x", session_id="s", role="tool", content="a",
                    created_at=created, turn_index=0, title="T", origin="keyboard",
                    share_excluded=False, timestamp_source="record")


def test_make_record_is_a_plain_dict_the_codex_and_cursor_readers_can_edit() -> None:
    # L6 rewrites the title after each file loop and adds its own facet; the
    # parsers must still accept the result, with or without model_slug.
    from topos.ingestion.parsers.ai_coding_tool import CodexTranscriptParser
    from topos.ingestion.sources.base import RawRecord

    record = make_record(
        tool="codex", record_uuid="item-1", session_id="thread-9", role="user", content="hi",
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc), turn_index=0, title="draft",
        origin="keyboard", share_excluded=False, timestamp_source="session", model_slug=None,
    )
    assert type(record) is dict and type(record["_metadata"]) is dict
    assert "model_slug" not in record["_metadata"]
    record["_metadata"]["conversation_title"] = "Final title"
    record["_metadata"]["cross_tool_import"] = True
    result = CodexTranscriptParser(dataset_id="u:d:x").validate(RawRecord(record_id="r", payload=record))
    assert result.is_valid and result.errors == []
    parsed = CodexTranscriptParser(dataset_id="u:d:x").parse(RawRecord(record_id="r", payload=record)).payload
    assert parsed["_metadata"]["conversation_title"] == "Final title"
    assert parsed["_metadata"]["cross_tool_import"] is True


@pytest.mark.parametrize("source", ["record", "session", "file_mtime"])
def test_make_record_accepts_the_three_timestamp_sources_only(source: str) -> None:
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    ok = make_record(tool="cursor", record_uuid="b", session_id="c", role="assistant", content="x",
                     created_at=created, turn_index=0, title="T", origin="assistant", share_excluded=False,
                     timestamp_source=source)
    assert ok["_metadata"]["timestamp_source"] == source
    with pytest.raises(ValueError, match="timestamp_source"):
        make_record(tool="cursor", record_uuid="b", session_id="c", role="assistant", content="x",
                    created_at=created, turn_index=0, title="T", origin="assistant", share_excluded=False,
                    timestamp_source="mtime")


def test_ledger_accepts_the_other_readers_reasons_positionally_and_keeps_its_counters_writable() -> None:
    ledger = Ledger()
    for reason in ("tool_bubble", "before_since", "unparseable", "other_item:reasoning",
                   "other_bubble_type:7", "non_keyboard_origin:source.subagent"):
        ledger.drop(reason, 2)
    ledger.message_nodes = 40
    ledger.turns_emitted = 9
    ledger.conversations_seen = 3
    ledger.conversations_kept = 2
    out = ledger.as_dict()
    assert out["dropped"] == {
        "before_since": 2, "non_keyboard_origin:source.subagent": 2, "other_bubble_type:7": 2,
        "other_item:reasoning": 2, "tool_bubble": 2, "unparseable": 2,
    }
    assert out["dropped_nodes_total"] == 12 and out["dropped_files"] == {}
    assert (out["message_nodes"], out["turns_emitted"], out["conversations_seen"], out["conversations_kept"]) == (40, 9, 3, 2)


def test_ledger_counts_drops_by_reason_and_origin_scrubs_taints_and_secret_sightings() -> None:
    ledger = Ledger()
    ledger.drop("tool_use")
    ledger.drop("harness_injected", origin="command-name")
    ledger.drop("non_keyboard_origin:isMeta", 2, origin="isMeta")
    ledger.drop("side_file")
    ledger.scrubbed("openai_key")
    ledger.scrubbed("topos_secret", 2)
    ledger.tainted(3)
    ledger.note_secret_sightings("sess-a", 2)
    ledger.note_secret_sightings("sess-a", 1)
    ledger.note_secret_sightings("sess-b", 1)
    ledger.turns_emitted = 5
    as_dict = ledger.as_dict()
    assert as_dict["dropped"] == {
        "harness_injected": 1,
        "non_keyboard_origin:isMeta": 2,
        "side_file": 1,
        "tool_use": 1,
    }
    assert as_dict["dropped_nodes_total"] == 4
    assert as_dict["dropped_files"] == {"side_file": 1}
    assert as_dict["origins"] == {"command-name": 1, "isMeta": 2}
    assert as_dict["scrubbed"] == {"openai_key": 1, "topos_secret": 2}
    assert as_dict["tainted_turns"] == 3
    assert as_dict["secret_sightings"] == {"sess-a": 3, "sess-b": 1}
    assert as_dict["sessions_with_secrets"] == 2
    assert as_dict["turns_emitted"] == 5
