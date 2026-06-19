"""Enrichment job registry."""

from .base import BaseEnrichmentJob
from .canonical.availability_scores_job import AvailabilityScoresJob
from .canonical.dimension_summary_job import DimensionSummaryJob
from .canonical.embeddings_job import EmbeddingsJob
from .canonical.entities_job import EntitiesJob
from .canonical.emo_27_job import Emo27Job
from .canonical.goal_extraction_job import GoalExtractionJob
from .canonical.relationship_edges_job import RelationshipEdgesJob
from .canonical.sentiment_job import SentimentJob
from .canonical.topics_job import TopicsJob
from .canonical.url_classification_signal_job import UrlClassificationSignalJob
from .raw.attachments_job import AttachmentsJob
from .raw.language_job import LanguageJob
from .raw.time_normalization_job import TimeNormalizationJob
from .raw.tool_calls_job import ToolCallsJob

CANONICAL_JOBS = [EntitiesJob(), TopicsJob(), SentimentJob(), EmbeddingsJob(), Emo27Job()]

SIGNAL_DERIVATION_JOBS = [
    Emo27Job(),
    EntitiesJob(),
    EmbeddingsJob(),
    TopicsJob(),
    SentimentJob(),
    DimensionSummaryJob(),
    GoalExtractionJob(),
    RelationshipEdgesJob(),
    AvailabilityScoresJob(),
    UrlClassificationSignalJob(),
]

SIGNAL_JOB_REGISTRY = {job.get_job_name(): job for job in SIGNAL_DERIVATION_JOBS}

RAW_JOBS = [AttachmentsJob(), ToolCallsJob(), LanguageJob(), TimeNormalizationJob()]

__all__ = [
    "BaseEnrichmentJob",
    "EntitiesJob",
    "TopicsJob",
    "SentimentJob",
    "EmbeddingsJob",
    "Emo27Job",
    "DimensionSummaryJob",
    "GoalExtractionJob",
    "RelationshipEdgesJob",
    "AvailabilityScoresJob",
    "UrlClassificationSignalJob",
    "AttachmentsJob",
    "ToolCallsJob",
    "LanguageJob",
    "TimeNormalizationJob",
    "CANONICAL_JOBS",
    "SIGNAL_DERIVATION_JOBS",
    "SIGNAL_JOB_REGISTRY",
    "RAW_JOBS",
]
