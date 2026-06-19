"""RetrievalRequest carries query_text from pipeline."""

from topos.query.types import RetrievalRequest


def test_retrieval_request_accepts_query_text() -> None:
    req = RetrievalRequest(manifest=object(), access_mode="summary", query_text="docker nginx")
    assert req.query_text == "docker nginx"
