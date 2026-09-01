"""A job that dies has to say so where someone can see it.

What this guards: the difference between a failed import and a slow one.

A crash was written to the node's own database and nowhere else, so the last
thing the control plane ever heard was the ``processing / 0%`` posted before the
run began. The job then read as working forever. Seen live: an import failed on
an unsupported URL scheme and the screen showed ``processing · 0.0%`` for as long
as anyone cared to watch, with the reason sitting in a log file.

That is the worst state a progress display can be in, because nothing the user
can do distinguishes it from patience being required.
"""

from __future__ import annotations

import asyncio

import pytest

from topos.pipeline import job_runner


class _Client:
    """Records what would be posted upstream."""

    posts: list = []

    def __init__(self, *_a, **_kw) -> None: ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).posts.append({"url": url, "json": json, "headers": headers})
        return None


@pytest.fixture
def posts(monkeypatch):
    _Client.posts = []
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return _Client.posts


PAYLOAD = {
    "progress_api_url": "https://cp.example",
    "progress_api_key": "key-123",
    "dataset_id": "ds-1",
}


@pytest.mark.asyncio
async def test_a_dead_job_is_reported_as_failed(posts):
    await job_runner.report_terminal_failure(PAYLOAD, ["job-1"], "boom")
    assert len(posts) == 1
    body = posts[0]["json"]
    assert body["status"] == "failed"
    assert body["job_id"] == "job-1"
    assert body["dataset_id"] == "ds-1"


@pytest.mark.asyncio
async def test_the_reason_travels_with_it(posts):
    """"Failed" with no reason is only marginally better than silence — the user
    still cannot tell whether to retry or to change something."""
    await job_runner.report_terminal_failure(
        PAYLOAD, ["job-1"], "Request URL has an unsupported protocol 'local-export://'"
    )
    assert "local-export://" in posts[0]["json"]["error_message"]


@pytest.mark.asyncio
async def test_it_posts_to_the_same_channel_progress_uses(posts):
    # A job that could report progress can always report its own death.
    await job_runner.report_terminal_failure(PAYLOAD, ["job-1"], "boom")
    assert posts[0]["url"] == "https://cp.example/v1/ingestion/progress"
    assert posts[0]["headers"]["Authorization"] == "Bearer key-123"


@pytest.mark.asyncio
async def test_every_coalesced_sibling_is_reported(posts):
    # Jobs are coalesced; reporting only the first leaves the others reading as
    # working forever.
    await job_runner.report_terminal_failure(PAYLOAD, ["job-1", "job-2", "job-3"], "boom")
    assert [p["json"]["job_id"] for p in posts] == ["job-1", "job-2", "job-3"]


@pytest.mark.asyncio
async def test_a_long_error_is_truncated_not_dropped(posts):
    await job_runner.report_terminal_failure(PAYLOAD, ["job-1"], "x" * 9000)
    assert 0 < len(posts[0]["json"]["error_message"]) <= 2000


@pytest.mark.asyncio
async def test_no_credentials_means_no_post_and_no_crash(posts):
    # A locally-triggered job has no upstream to tell. That must be quiet, not fatal.
    await job_runner.report_terminal_failure({}, ["job-1"], "boom")
    await job_runner.report_terminal_failure({"progress_api_url": "https://cp"}, ["job-1"], "boom")
    assert posts == []


@pytest.mark.asyncio
async def test_reporting_never_raises(monkeypatch, posts):
    """Reporting a failure must never become a second failure that hides the
    first — the log line is the last thing standing if this goes wrong."""

    class _Broken(_Client):
        async def post(self, *_a, **_kw):
            raise RuntimeError("network is down")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Broken)
    await job_runner.report_terminal_failure(PAYLOAD, ["job-1"], "boom")  # must not raise


@pytest.mark.asyncio
async def test_a_crashing_executor_reports_upstream(monkeypatch, posts):
    """The integration this exists for: an executor that raises must produce an
    upstream 'failed', not just a local database row."""
    marked: list = []

    async def _run_db(_factory, fn, *args, **kwargs):
        marked.append(fn.__name__)
        return None

    async def _boom(_payload):
        raise RuntimeError("Request URL has an unsupported protocol 'local-export://'")

    monkeypatch.setattr(job_runner, "_run_db", _run_db)
    monkeypatch.setitem(job_runner.EXECUTORS, "file_ingestion", _boom)

    await job_runner.process_job(
        lambda: object(),
        {
            "job_id": "job-9",
            "kind": "file_ingestion",
            "payload": dict(PAYLOAD, job_id="job-9"),
        },
    )

    assert posts, "a crashed job reported nothing upstream"
    assert posts[0]["json"]["status"] == "failed"
    assert "local-export://" in posts[0]["json"]["error_message"]
