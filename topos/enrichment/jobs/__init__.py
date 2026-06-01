"""Enrichment job registry."""

from .base import BaseEnrichmentJob
from .canonical.embeddings_job import EmbeddingsJob
from .canonical.entities_job import EntitiesJob
from .canonical.emo_27_job import Emo27Job
from .canonical.sentiment_job import SentimentJob
from .canonical.topics_job import TopicsJob
from .raw.attachments_job import AttachmentsJob
from .raw.language_job import LanguageJob
from .raw.time_normalization_job import TimeNormalizationJob
from .raw.tool_calls_job import ToolCallsJob

CANONICAL_JOBS = [EntitiesJob(), TopicsJob(), SentimentJob(), EmbeddingsJob(), Emo27Job()]
RAW_JOBS = [AttachmentsJob(), ToolCallsJob(), LanguageJob(), TimeNormalizationJob()]

__all__ = [
    "BaseEnrichmentJob",
    "EntitiesJob",
    "TopicsJob",
    "SentimentJob",
    "EmbeddingsJob",
    "Emo27Job",
    "AttachmentsJob",
    "ToolCallsJob",
    "LanguageJob",
    "TimeNormalizationJob",
    "CANONICAL_JOBS",
    "RAW_JOBS",
]
