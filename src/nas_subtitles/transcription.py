"""Local ASR, per-chunk checkpoints and the global merge. Owned by stage 4.

The model is loaded once per worker from an absolute local path with
``local_files_only=True``. The faster-whisper segment generator is consumed
inside the stage: creating it does not run the transcription.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .config import AppConfig
from .domain import (
    AudioChunk,
    ChunkTranscript,
    LanguageDecision,
    MediaFingerprint,
    ModelIdentity,
    Seconds,
    Transcript,
)

__all__ = [
    "FasterWhisperTranscriber",
    "chunk_checkpoint_path",
    "merge_chunk_transcripts",
    "read_chunk_checkpoint",
    "write_chunk_checkpoint",
]


class FasterWhisperTranscriber:
    """``Transcriber`` implementation over faster-whisper / CTranslate2."""

    def __init__(self, config: AppConfig, *, model_path: Path) -> None:
        self.config = config
        self.model_path = model_path

    @property
    def model_identity(self) -> ModelIdentity:
        raise NotImplementedError("ASR is implemented in stage 4 (asr)")

    def detect_language(self, samples: Sequence[AudioChunk]) -> LanguageDecision:
        raise NotImplementedError("ASR is implemented in stage 4 (asr)")

    def transcribe(self, chunk: AudioChunk, *, language: str) -> ChunkTranscript:
        """Transcribe one chunk and return absolute, video-timeline timestamps."""
        raise NotImplementedError("ASR is implemented in stage 4 (asr)")


def chunk_checkpoint_path(config: AppConfig, *, job_id: str, chunk_index: int) -> Path:
    raise NotImplementedError("checkpoints are implemented in stage 4 (asr)")


def write_chunk_checkpoint(
    path: Path,
    transcript: ChunkTranscript,
    *,
    job_id: str,
    fingerprint: MediaFingerprint,
    stage_config_hash: str,
) -> Path:
    """Write to a temporary file, flush, then rename on the same filesystem."""
    raise NotImplementedError("checkpoints are implemented in stage 4 (asr)")


def read_chunk_checkpoint(
    path: Path,
    *,
    job_id: str,
    fingerprint: MediaFingerprint,
    stage_config_hash: str,
    model_identity: ModelIdentity,
) -> ChunkTranscript | None:
    """Return the checkpoint only when job, fingerprint, schema, config hash
    and model identity all match; otherwise ``None`` so the chunk is redone."""
    raise NotImplementedError("checkpoints are implemented in stage 4 (asr)")


def merge_chunk_transcripts(
    transcripts: Sequence[ChunkTranscript], *, duration_seconds: Seconds
) -> Transcript:
    """Order words globally, then drop duplicates across chunk boundaries.

    Only equivalent tokens with overlapping intervals are deduplicated, and
    the more confident version is kept. A phrase legitimately repeated at a
    different time stays.
    """
    raise NotImplementedError("merging is implemented in stage 4 (asr)")
