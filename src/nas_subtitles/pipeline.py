"""Stage orchestration for a single job. Owned by stages 4 to 7.

Runs the stages in order, reusing any checkpoint whose identity and stage
config hash still match. A stage failure keeps the artifacts of the stages
before it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .domain import (
    AudioExtractor,
    JobRecord,
    JobRepository,
    JobState,
    MediaProbe,
    PipelineStage,
    QualityReport,
    Seconds,
    SubtitleRenderer,
    Transcriber,
    Translator,
)

__all__ = ["PipelineResult", "StageContext", "run_job", "run_stage"]


@dataclass(frozen=True, slots=True)
class StageContext:
    """Everything a stage needs, with the engines injected for testability."""

    config: AppConfig
    repository: JobRepository
    job: JobRecord
    probe: MediaProbe
    extractor: AudioExtractor
    transcriber: Transcriber
    translator: Translator
    renderer: SubtitleRenderer


@dataclass(frozen=True, slots=True)
class PipelineResult:
    job_id: str
    state: JobState
    last_stage: PipelineStage
    quality: QualityReport
    output_path: Path | None = None
    cue_count: int = 0
    media_seconds: Seconds = 0.0


def run_stage(context: StageContext, stage: PipelineStage) -> StageContext:
    """Execute one stage, reusing a valid checkpoint when one exists."""
    raise NotImplementedError("the pipeline is implemented in stages 4 to 7")


def run_job(
    context: StageContext,
    *,
    start_stage: PipelineStage | None = None,
    stop_after: PipelineStage | None = None,
) -> PipelineResult:
    """Run the job from its current stage, honouring cancellation and SIGTERM."""
    raise NotImplementedError("the pipeline is implemented in stages 4 to 7")
