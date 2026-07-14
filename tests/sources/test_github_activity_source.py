"""GitHub activity source: registry definition, parser, and canonical mapper."""

from __future__ import annotations

import sqlite3

import pytest

from topos.canonicalization.mappers import MAPPER_REGISTRY
from topos.canonicalization.mappers.github_activity_mapper import GithubActivityCanonicalMapper
from topos.ingestion.parsers import PARSER_REGISTRY, GithubActivityParser
from topos.ingestion.parsers.base import NormalizedRecord
from topos.ingestion.sources.base import RawRecord
from topos.sources.bundled_canonical_triples import infer_bundled_canonical_triple
from topos.sources.definitions import accepts_app_ingest
from topos.sources.registry import GITHUB_ACTIVITY, REGISTRY
from topos.storage.canonical.activity_tables import ActivityEventsManager
from topos.storage.db.migrations import apply_all_migrations


def _github_event(**overrides) -> dict:
    """GitHub REST /users/{u}/events-shaped record (extra fields intentionally kept)."""
    event = {
        "id": "44851245900",
        "type": "PushEvent",
        "actor": {"id": 583231, "login": "jonny", "avatar_url": "https://avatars.githubusercontent.com/u/583231"},
        "repo": {"id": 41881900, "name": "dialogues/topos", "url": "https://api.github.com/repos/dialogues/topos"},
        "payload": {
            "push_id": 21980276450,
            "size": 3,
            "distinct_size": 3,
            "ref": "refs/heads/main",
            "commits": [{"sha": "a" * 40}, {"sha": "b" * 40}, {"sha": "c" * 40}],
        },
        "public": True,
        "created_at": "2026-07-01T12:34:56Z",
    }
    event.update(overrides)
    return event


def test_registry_definition_matches_browser_activity_template() -> None:
    assert REGISTRY["github_activity"] is GITHUB_ACTIVITY
    assert GITHUB_ACTIVITY.source_type == "ui_stream"
    assert GITHUB_ACTIVITY.delivery == "client_push"
    assert GITHUB_ACTIVITY.schema_id == "github.activity.v1"
    assert GITHUB_ACTIVITY.parser_id == "github.activity.v1"
    assert GITHUB_ACTIVITY.canonical_group_id == "activity"
    assert GITHUB_ACTIVITY.canonical_mapper_id == "github_activity"
    assert GITHUB_ACTIVITY.default_scope_id == "activity"
    assert GITHUB_ACTIVITY.allowed_scope_ids == ["activity:read", "activity:write"]
    assert accepts_app_ingest(GITHUB_ACTIVITY)


def test_bundled_triple_and_registries() -> None:
    assert infer_bundled_canonical_triple(schema_id="github.activity.v1") == (
        "github_activity",
        "activity",
    )
    assert PARSER_REGISTRY["github.activity.v1"] is GithubActivityParser
    assert MAPPER_REGISTRY["github_activity"] is GithubActivityCanonicalMapper


def test_parser_validate_requires_id_type_created_at() -> None:
    parser = GithubActivityParser(dataset_id="user:default:device")
    for missing in ("id", "type", "created_at"):
        payload = _github_event()
        payload.pop(missing)
        result = parser.validate(RawRecord(record_id="r-1", payload=payload))
        assert not result.is_valid
        assert missing in result.errors[0]
    assert parser.validate(RawRecord(record_id="r-1", payload=_github_event())).is_valid


def test_parser_parse_preserves_github_fields() -> None:
    parser = GithubActivityParser(dataset_id="user:default:device")
    normalized = parser.parse(RawRecord(record_id="r-1", payload=_github_event()))
    assert normalized.record_id == "44851245900"
    assert normalized.payload["type"] == "PushEvent"
    assert normalized.payload["created_at"] == "2026-07-01T12:34:56Z"
    assert normalized.payload["repo"]["name"] == "dialogues/topos"
    assert normalized.payload["actor"]["login"] == "jonny"
    assert normalized.payload["payload"]["size"] == 3
    assert normalized.payload["dataset_id"] == "user:default:device"


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("PushEvent", "push"),
        ("PullRequestEvent", "pull_request"),
        ("IssuesEvent", "issue"),
        ("IssueCommentEvent", "comment"),
        ("WatchEvent", "star"),
        ("CreateEvent", "create"),
        ("ForkEvent", "fork"),
        ("ReleaseEvent", "release"),
        ("GollumEvent", "gollum"),  # unmapped: lowercase, "Event" suffix stripped
    ],
)
def test_mapper_activity_type_mapping(event_type: str, expected: str) -> None:
    normalized = NormalizedRecord(
        record_id="44851245900",
        payload=_github_event(type=event_type),
    )
    mapped = GithubActivityCanonicalMapper().map(normalized)
    assert mapped.payload["activity_type"] == expected


def test_mapper_push_event_payload() -> None:
    normalized = NormalizedRecord(record_id="44851245900", payload=_github_event())
    mapped = GithubActivityCanonicalMapper().map(normalized)
    assert mapped.record_id == "github:44851245900"
    assert mapped.payload["event_id"] == "github:44851245900"
    assert mapped.payload["activity_type"] == "push"
    assert mapped.payload["url"] == "https://github.com/dialogues/topos"
    assert mapped.payload["title"] == "dialogues/topos: pushed 3 commits"
    assert mapped.payload["occurred_at"] == "2026-07-01T12:34:56Z"
    assert mapped.payload["source_record_id"] == "44851245900"
    assert mapped.payload["metadata_json"] == {
        "event_type": "PushEvent",
        "repo": "dialogues/topos",
        "actor": "jonny",
    }


def test_mapper_prefers_payload_html_url() -> None:
    normalized = NormalizedRecord(
        record_id="44851245901",
        payload=_github_event(
            id="44851245901",
            type="PullRequestEvent",
            payload={
                "action": "opened",
                "number": 42,
                "pull_request": {"number": 42, "html_url": "https://github.com/dialogues/topos/pull/42"},
            },
        ),
    )
    mapped = GithubActivityCanonicalMapper().map(normalized)
    assert mapped.payload["url"] == "https://github.com/dialogues/topos/pull/42"
    assert mapped.payload["title"] == "dialogues/topos: opened pull request #42"


def test_mapper_humanized_titles() -> None:
    mapper = GithubActivityCanonicalMapper()
    cases = [
        ({"type": "IssuesEvent", "payload": {"action": "closed", "issue": {"number": 7}}},
         "dialogues/topos: closed issue #7"),
        ({"type": "IssueCommentEvent", "payload": {"action": "created", "issue": {"number": 7}}},
         "dialogues/topos: commented on issue #7"),
        ({"type": "WatchEvent", "payload": {"action": "started"}}, "dialogues/topos: starred"),
        ({"type": "CreateEvent", "payload": {"ref_type": "branch", "ref": "feature-x"}},
         "dialogues/topos: created branch feature-x"),
        ({"type": "CreateEvent", "payload": {"ref_type": "repository", "ref": None}},
         "dialogues/topos: created repository"),
        ({"type": "ForkEvent", "payload": {}}, "dialogues/topos: forked"),
        ({"type": "ReleaseEvent", "payload": {"release": {"tag_name": "v1.2.0"}}},
         "dialogues/topos: released v1.2.0"),
        ({"type": "PushEvent", "payload": {"size": 1}}, "dialogues/topos: pushed 1 commit"),
        ({"type": "GollumEvent", "payload": {}}, "dialogues/topos: gollum"),
    ]
    for overrides, expected in cases:
        mapped = mapper.map(NormalizedRecord(record_id="e-1", payload=_github_event(**overrides)))
        assert mapped.payload["title"] == expected, overrides["type"]


def test_map_many_push_event_emits_activity_plus_journal_per_commit() -> None:
    """Dual lane: 1 activity_events record + 1 journal_entries record per commit."""
    event = _github_event(
        payload={
            "push_id": 21980276450,
            "size": 2,
            "ref": "refs/heads/main",
            "commits": [
                {
                    "sha": "a" * 40,
                    "message": "fix: tighten retry loop",
                    "timestamp": "2026-07-01T12:30:00Z",
                },
                {"sha": "b" * 40, "message": "docs: add sync notes"},
            ],
        }
    )
    records = GithubActivityCanonicalMapper().map_many(
        NormalizedRecord(record_id="44851245900", payload=event)
    )
    assert len(records) == 3

    activity = records[0]
    assert activity.table is None  # default lane → group table (activity_events)
    assert activity.payload["event_id"] == "github:44851245900"
    assert activity.payload["activity_type"] == "push"

    first, second = records[1], records[2]
    assert first.table == "journal_entries"
    assert first.record_id == f"github:dialogues/topos:{'a' * 40}"
    assert first.payload["entry_id"] == f"github:dialogues/topos:{'a' * 40}"
    # Commit timestamp wins over event created_at; ends_at mirrors entry_at.
    assert first.payload["entry_at"] == "2026-07-01T12:30:00Z"
    assert first.payload["ends_at"] == "2026-07-01T12:30:00Z"
    assert first.payload["starts_at"] is None  # never invent durations
    assert first.payload["duration"] is None
    assert first.payload["mood_tag"] is None
    assert first.payload["category"] == "code"
    assert first.payload["content"] == "fix: tighten retry loop"
    assert first.payload["people"] is None
    assert first.payload["place_name"] is None
    assert first.payload["source_record_id"] == f"44851245900:{'a' * 40}"
    assert first.payload["metadata_json"] == {
        "repo": "dialogues/topos",
        "sha": "a" * 40,
        "ref": "refs/heads/main",
        "branch": "main",
        "event_id": "github:44851245900",
    }

    # Commit without timestamp falls back to the event created_at.
    assert second.payload["entry_at"] == "2026-07-01T12:34:56Z"
    assert second.payload["ends_at"] == "2026-07-01T12:34:56Z"
    assert second.payload["content"] == "docs: add sync notes"
    assert second.payload["source_record_id"] == f"44851245900:{'b' * 40}"


def test_map_many_journal_lane_is_authored_only_when_authorship_stamped() -> None:
    """Strict policy: commits stamped ai_assisted/participated/bot stay
    activity-only; only `authored` fans out to journal_entries. Absent field
    keeps the legacy all-commits behavior (previous test covers it)."""
    event = _github_event(
        payload={
            "size": 4,
            "ref": "refs/heads/main",
            "commits": [
                {"sha": "a" * 40, "message": "my own work", "authorship": "authored"},
                {"sha": "b" * 40, "message": "co-authored with claude", "authorship": "ai_assisted"},
                {"sha": "c" * 40, "message": "someone else's PR", "authorship": "participated"},
                {"sha": "d" * 40, "message": "chore(deps): bump", "authorship": "bot"},
            ],
        }
    )
    records = GithubActivityCanonicalMapper().map_many(
        NormalizedRecord(record_id="e-lane", payload=event)
    )
    # 1 activity event + ONLY the authored commit's journal entry.
    assert len(records) == 2
    journal = records[1]
    assert journal.table == "journal_entries"
    assert journal.payload["content"] == "my own work"
    # Authorship is recorded on the journal row's metadata for later filtering.
    assert journal.payload["metadata_json"]["authorship"] == "authored"


def test_map_many_authorship_matching_is_case_insensitive() -> None:
    event = _github_event(
        payload={
            "size": 1,
            "commits": [{"sha": "e" * 40, "message": "shouting", "authorship": "AUTHORED"}],
        }
    )
    records = GithubActivityCanonicalMapper().map_many(
        NormalizedRecord(record_id="e-case", payload=event)
    )
    assert len(records) == 2
    assert records[1].payload["metadata_json"]["authorship"] == "authored"


def test_map_many_non_push_event_is_activity_only() -> None:
    event = _github_event(
        type="PullRequestEvent",
        payload={
            "action": "opened",
            "number": 42,
            "pull_request": {"number": 42, "html_url": "https://github.com/dialogues/topos/pull/42"},
        },
    )
    records = GithubActivityCanonicalMapper().map_many(
        NormalizedRecord(record_id="44851245901", payload=event)
    )
    assert len(records) == 1
    assert records[0].table is None
    assert records[0].payload["activity_type"] == "pull_request"


def test_map_many_push_without_commits_or_sha_is_activity_only() -> None:
    mapper = GithubActivityCanonicalMapper()
    no_commits = _github_event(payload={"push_id": 1, "size": 0, "ref": "refs/heads/main"})
    assert len(mapper.map_many(NormalizedRecord(record_id="e1", payload=no_commits))) == 1
    # A commit without a sha has no deterministic identity → skipped.
    sha_less = _github_event(
        payload={"push_id": 2, "size": 1, "commits": [{"message": "no sha here"}]}
    )
    assert len(mapper.map_many(NormalizedRecord(record_id="e2", payload=sha_less))) == 1


def test_map_many_is_deterministic_across_remaps() -> None:
    event = _github_event(
        payload={"push_id": 3, "size": 1, "ref": "refs/heads/main", "commits": [{"sha": "c" * 40, "message": "m"}]}
    )
    normalized = NormalizedRecord(record_id="44851245900", payload=event)
    mapper = GithubActivityCanonicalMapper()
    first = mapper.map_many(normalized)
    second = mapper.map_many(normalized)
    assert [r.record_id for r in first] == [r.record_id for r in second]
    assert [r.payload for r in first] == [r.payload for r in second]


def test_github_event_maps_to_activity_events_table() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)

    normalized = NormalizedRecord(record_id="44851245900", payload=_github_event())
    mapped = GithubActivityCanonicalMapper().map(normalized)
    manager = ActivityEventsManager(conn)
    result = manager.upsert_batch(
        [mapped.payload],
        source_id="github_activity",
        sync_batch_id="github-batch-1",
    )
    assert result["events_created"] == 1

    row = conn.execute(
        """
        SELECT event_id, activity_type, url, title, source_id, sync_batch_id
        FROM activity_events WHERE event_id=?
        """,
        (mapped.payload["event_id"],),
    ).fetchone()
    assert row is not None
    assert row[1] == "push"
    assert row[2] == "https://github.com/dialogues/topos"
    assert row[3] == "dialogues/topos: pushed 3 commits"
    assert row[4] == "github_activity"
    assert row[5] == "github-batch-1"

    second = manager.upsert_batch(
        [mapped.payload],
        source_id="github_activity",
        sync_batch_id="github-batch-2",
    )
    assert second["events_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 1
