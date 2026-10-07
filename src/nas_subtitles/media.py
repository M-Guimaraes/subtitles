"""FFprobe inspection and chunked audio extraction. Owned by stage 3.

Every external command is built as an argv list and run with ``shell=False``
and a timeout, so a filename containing spaces, Unicode or shell
metacharacters is never interpreted.
"""

from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .domain import (
    AudioChunk,
    AudioChunkSpec,
    AudioStreamInfo,
    ProbeResult,
    Seconds,
)

__all__ = [
    "FfmpegAudioExtractor",
    "FfprobeMediaProbe",
    "ensure_free_space",
    "plan_chunks",
    "select_audio_stream",
]


class FfprobeMediaProbe:
    """``MediaProbe`` implementation shelling out to ``ffprobe -of json``."""

    def __init__(self, *, ffprobe_path: str = "ffprobe", timeout_seconds: float = 60.0) -> None:
        self.ffprobe_path = ffprobe_path
        self.timeout_seconds = timeout_seconds

    def probe(self, path: Path) -> ProbeResult:
        raise NotImplementedError("media inspection is implemented in stage 3 (media)")


class FfmpegAudioExtractor:
    """``AudioExtractor`` implementation producing mono 16 kHz PCM chunks."""

    def __init__(self, *, ffmpeg_path: str = "ffmpeg", timeout_seconds: float = 1800.0) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.timeout_seconds = timeout_seconds

    def plan_chunks(
        self,
        *,
        duration_seconds: Seconds,
        stream_start_seconds: Seconds,
        chunk_seconds: Seconds,
        overlap_seconds: Seconds,
    ) -> tuple[AudioChunkSpec, ...]:
        raise NotImplementedError("audio extraction is implemented in stage 3 (media)")

    def extract(
        self,
        *,
        source: Path,
        stream_index: int,
        spec: AudioChunkSpec,
        destination: Path,
    ) -> AudioChunk:
        raise NotImplementedError("audio extraction is implemented in stage 3 (media)")


def plan_chunks(
    *,
    duration_seconds: Seconds,
    stream_start_seconds: Seconds,
    chunk_seconds: Seconds,
    overlap_seconds: Seconds,
) -> tuple[AudioChunkSpec, ...]:
    """Pure chunk planner, shared by the real extractor and the test fakes."""
    raise NotImplementedError("audio extraction is implemented in stage 3 (media)")


def select_audio_stream(
    probe_result: ProbeResult, *, override_index: int | None = None
) -> AudioStreamInfo:
    """Pick the audio stream to transcribe.

    Priority: explicit global index, then a non-commentary ``eng``/``en``
    stream, then a Portuguese stream, then a non-commentary default, then the
    first remaining audio stream. Raises ``invalid_media`` when none exists.
    """
    raise NotImplementedError("stream selection is implemented in stage 3 (media)")


def ensure_free_space(config: AppConfig, path: Path) -> None:
    """Raise ``insufficient_space`` when the work filesystem is below the floor."""
    raise NotImplementedError("space checks are implemented in stage 3 (media)")
