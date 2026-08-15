"""GitHub activity events → ``activity_events`` canonical mapper.

One event, one activity row. Commit messages ride on that row's ``content``,
declared in the source definition (``canonical_field_map``:
``payload.commits[*].message``, joined for a multi-commit push).

There is deliberately no journal lane. Each PushEvent commit used to fan out
into a ``journal_entries`` row on the reading that a commit is a work-journal
entry, gated so that only commits stamped ``authorship='authored'`` fanned out.
Two things retired it:

1. ``journal_entries`` is authored-by-construction in ``provenance.roles`` — a
   row there IS the owner's own writing, belief-grade, eligible to mint goals
   and self-facts. Commit prose is written by coding agents now, so the lane was
   attributing sentences to the owner that the owner may never have read
   closely. The gate could not catch it: ``authorship`` is stamped from a
   co-author TRAILER, which demotes ``Co-Authored-By: Claude`` and passes the
   same message without one. That blind spot is not fixable by a better regex.
2. The lane existed because commit messages lived nowhere else. They now live on
   the activity row itself, where the role model already reads them as ambient —
   exposure, not expression. Nothing is lost by dropping the duplicate, and the
   same text still reaches embeddings, topic clustering, entities and triage.

This mirrors the browser mapper's refusal to promote page-body text into
``content``: the words on a page are the page author's, and a lane that reads
as first-person must not be fed prose the owner did not write. The source also
declares ``posture='ambient'``, which caps any row it produces at ``observed``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

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

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="github_activity", mapping_version=self.version)
