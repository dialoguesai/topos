"""PipelineStage enum values match PRD Appendix A."""

from topos.pipeline.stages import PipelineStage


def test_pipeline_stages_enum_values() -> None:
    expected = [
        "source_connect",
        "raw_write",
        "canonical_map",
        "signal_derive",
        "dimension_profile",
        "data_health",
    ]
    assert [stage.value for stage in PipelineStage] == expected
