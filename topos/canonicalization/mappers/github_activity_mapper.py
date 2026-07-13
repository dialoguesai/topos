"""GitHub activity events → dual-lane canonical mapper.

Every event maps to one ``activity_events`` row (single-lane behavior kept).
Additionally, each commit inside a PushEvent ``payload.commits[]`` maps to one
``journal_entries`` row — a commit reads like a work-journal entry (category
'code', content = commit message). Only PushEvent commits fan out; PRs/issues
stay activity-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ...ingestion.parsers.base import NormalizedRecord
from .base import CanonicalMapper, CanonicalRecord, MappingMetadata

# GitHub REST event type → canonical activity_type. Unknown types fall back to the
# lowercased type with the "Event" suffix stripped (e.g. GollumEvent → gollum).
_ACTIVITY_TYPES = {
    "PushEvent": "push",
    "PullRequestEvent": "pull_request",
    "IssuesEvent": "issue",
    "IssueCommentEvent": "comment",
    "WatchEvent": "star",
    "CreateEvent": "create",
    "ForkEvent": "fork",
    "ReleaseEvent": "release",
}


def _activity_type(event_type: str) -> str:
    mapped = _ACTIVITY_TYPES.get(event_type)
    if mapped:
        return mapped
    stripped = event_type[: -len("Event")] if event_type.endswith("Event") else event_type
    return stripped.lower() or "event"


def _event_url(repo_name: Optional[str], event_payload: Any) -> Optional[str]:
    """Most specific html_url the event payload carries, else the repo page."""
    if isinstance(event_payload, dict):
        if str(event_payload.get("html_url") or "").strip():
            return str(event_payload["html_url"])
        for key in ("comment", "pull_request", "issue", "release", "forkee"):
            obj = event_payload.get(key)
            if isinstance(obj, dict) and str(obj.get("html_url") or "").strip():
                return str(obj["html_url"])
    if repo_name:
        return f"https://github.com/{repo_name}"
    return None


def _number_suffix(obj: Any) -> str:
    number = obj.get("number") if isinstance(obj, dict) else None
    return f" #{number}" if number is not None else ""


def _title(repo_name: Optional[str], event_type: str, activity_type: str, event_payload: Any) -> str:
    """Humanize the event ("owner/repo: pushed 3 commits" style)."""
    prefix = repo_name or "github"
    p: Dict[str, Any] = event_payload if isinstance(event_payload, dict) else {}
    if event_type == "PushEvent":
        commits = p.get("commits")
        count = p.get("size") or (len(commits) if isinstance(commits, list) else 0)
        if count:
            return f"{prefix}: pushed {count} {'commit' if count == 1 else 'commits'}"
        return f"{prefix}: pushed commits"
    if event_type == "PullRequestEvent":
        action = str(p.get("action") or "updated")
        return f"{prefix}: {action} pull request{_number_suffix(p.get('pull_request')) or _number_suffix(p)}"
    if event_type == "IssuesEvent":
        action = str(p.get("action") or "updated")
        return f"{prefix}: {action} issue{_number_suffix(p.get('issue'))}"
    if event_type == "IssueCommentEvent":
        return f"{prefix}: commented on issue{_number_suffix(p.get('issue'))}"
    if event_type == "WatchEvent":
        return f"{prefix}: starred"
    if event_type == "CreateEvent":
        ref_type = str(p.get("ref_type") or "repository")
        ref = str(p.get("ref") or "").strip()
        return f"{prefix}: created {ref_type} {ref}".rstrip()
    if event_type == "ForkEvent":
        return f"{prefix}: forked"
    if event_type == "ReleaseEvent":
        release = p.get("release") if isinstance(p.get("release"), dict) else {}
        tag = str(release.get("tag_name") or release.get("name") or "").strip()
        return f"{prefix}: released {tag}" if tag else f"{prefix}: published a release"
    return f"{prefix}: {activity_type.replace('_', ' ')}"


def _commit_timestamp(commit: Dict[str, Any]) -> Optional[str]:
    """Commit timestamp if the payload carries one (REST events API usually
    omits it — webhook-shaped payloads have ``timestamp``, some mirrors nest an
    author date). None → caller falls back to the event created_at."""
    for candidate in (commit.get("timestamp"), commit.get("date")):
        if str(candidate or "").strip():
            return str(candidate)
    author = commit.get("author")
    if isinstance(author, dict) and str(author.get("date") or "").strip():
        return str(author["date"])
    return None


def _branch_from_ref(ref: Optional[str]) -> Optional[str]:
    if ref and ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    return None


@dataclass
class GithubActivityCanonicalMapper(CanonicalMapper):
    version: str = "v2"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        payload = normalized.payload
        record_id = str(payload.get("id") or normalized.record_id)
        event_type = str(payload.get("type") or "")
        activity_type = _activity_type(event_type)
        repo = payload.get("repo")
        repo_name = str(repo.get("name")) if isinstance(repo, dict) and repo.get("name") else None
        actor = payload.get("actor")
        actor_login = actor.get("login") if isinstance(actor, dict) else None
        event_payload = payload.get("payload")
        metadata = {"event_type": event_type, "repo": repo_name, "actor": actor_login}
        canonical = {
            "event_id": f"github:{record_id}",
            "activity_type": activity_type,
            "url": _event_url(repo_name, event_payload),
            "title": _title(repo_name, event_type, activity_type, event_payload),
            "occurred_at": payload.get("created_at") or payload.get("occurred_at"),
            "source_record_id": record_id,
            "metadata_json": {k: v for k, v in metadata.items() if v is not None},
        }
        return CanonicalRecord(record_id=canonical["event_id"], payload=canonical)

    def map_many(self, normalized: NormalizedRecord) -> List[CanonicalRecord]:
        """Dual lane: the activity_events row (always) + one journal_entries
        row per PushEvent commit. Deterministic ids keep re-ingest idempotent:
        entry_id 'github:{repo}:{sha}', source_record_id '{event_id}:{sha}'."""
        records = [self.map(normalized)]
        payload = normalized.payload
        if str(payload.get("type") or "") != "PushEvent":
            return records

        event_record_id = str(payload.get("id") or normalized.record_id)
        created_at = payload.get("created_at") or payload.get("occurred_at")
        repo = payload.get("repo")
        repo_name = str(repo.get("name")) if isinstance(repo, dict) and repo.get("name") else None
        event_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        ref = str(event_payload.get("ref") or "").strip() or None
        branch = _branch_from_ref(ref)
        commits = event_payload.get("commits")
        if not isinstance(commits, list):
            return records

        for commit in commits:
            if not isinstance(commit, dict):
                continue
            sha = str(commit.get("sha") or "").strip()
            if not sha:
                continue  # no deterministic identity without a sha
            entry_id = f"github:{repo_name}:{sha}" if repo_name else f"github:{sha}"
            entry_at = _commit_timestamp(commit) or created_at
            metadata: Dict[str, Any] = {
                "repo": repo_name,
                "sha": sha,
                "ref": ref,
                "branch": branch,
                "event_id": f"github:{event_record_id}",
            }
            journal = {
                "entry_id": entry_id,
                "entry_at": entry_at,
                # A commit is a point-in-time entry: no invented duration/start.
                "starts_at": None,
                "ends_at": entry_at,
                "duration": None,
                "mood_tag": None,
                "category": "code",
                "content": str(commit.get("message") or ""),
                "people": None,
                "place_name": None,
                "source_record_id": f"{event_record_id}:{sha}",
                "metadata_json": {k: v for k, v in metadata.items() if v is not None},
            }
            records.append(
                CanonicalRecord(record_id=entry_id, payload=journal, table="journal_entries")
            )
        return records

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="github_activity", mapping_version=self.version)
