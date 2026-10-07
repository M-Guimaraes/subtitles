"""Local ASR, per-chunk checkpoints and the global merge. Owned by stage 4.

The model is loaded once per worker from an absolute local path with
``local_files_only=True``. The faster-whisper segment generator is consumed
inside the stage: creating it does not run the transcription.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import AppConfig
from .domain import (
    CHECKPOINT_SCHEMA_VERSION,
    AudioChunk,
    AudioChunkSpec,
    ChunkTranscript,
    ErrorCode,
    LanguageDecision,
    LanguageSample,
    MediaFingerprint,
    ModelIdentity,
    ModelKind,
    NasSubtitlesError,
    Seconds,
    Transcript,
    TranscriptSegment,
    Word,
    canonical_json,
)
from .language import decide_source_language, normalize_language_tag
from .logging_setup import path_token
from .models import read_model_manifest

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
        self._model: Any | None = None
        self._identity: ModelIdentity | None = None

    @property
    def model_identity(self) -> ModelIdentity:
        if self._identity is None:
            recorded = next(
                (item for item in read_model_manifest(self.config) if item.kind is ModelKind.ASR),
                None,
            )
            self._identity = recorded or ModelIdentity(
                kind=ModelKind.ASR,
                name=self.config.asr.model,
                path=self.model_path,
            )
        return self._identity

    def detect_language(self, samples: Sequence[AudioChunk]) -> LanguageDecision:
        collected: list[LanguageSample] = []
        model = self._load()
        from faster_whisper.audio import decode_audio

        for chunk in samples:
            try:
                audio = decode_audio(str(chunk.path), sampling_rate=chunk.sample_rate)
                language, probability, _all = model.detect_language(audio=audio, vad_filter=True)
            except Exception:
                collected.append(
                    LanguageSample(
                        offset_seconds=chunk.spec.extract_start_seconds,
                        duration_seconds=chunk.spec.extract_duration_seconds,
                        language=None,
                        probability=None,
                        has_speech=False,
                    )
                )
                continue
            normalised = normalize_language_tag(str(language))
            has_speech = probability is not None and float(probability) >= 0.15
            collected.append(
                LanguageSample(
                    offset_seconds=chunk.spec.extract_start_seconds,
                    duration_seconds=chunk.spec.extract_duration_seconds,
                    language=normalised,
                    probability=float(probability) if probability is not None else None,
                    has_speech=has_speech,
                )
            )
        dummy = _stream_without_metadata()
        return decide_source_language(
            self.config, stream=dummy, samples=collected, transcriber=self
        )

    def transcribe(self, chunk: AudioChunk, *, language: str) -> ChunkTranscript:
        """Transcribe one chunk and return absolute, video-timeline timestamps."""
        model = self._load()
        generator, info = model.transcribe(
            str(chunk.path),
            language=language,
            beam_size=self.config.asr.beam_size,
            word_timestamps=self.config.asr.word_timestamps,
            vad_filter=self.config.asr.vad_filter,
            condition_on_previous_text=self.config.asr.condition_on_previous_text,
        )
        raw_segments = list(generator)
        segments = _absolute_segments(raw_segments, chunk=chunk)
        detected = normalize_language_tag(getattr(info, "language", None)) or language
        probability = getattr(info, "language_probability", None)
        return ChunkTranscript(
            chunk=chunk.spec,
            language=detected,
            segments=segments,
            language_probability=float(probability) if probability is not None else None,
            model_identity=self.model_identity,
        )

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if not self.model_path.exists():
            raise NasSubtitlesError(
                "Whisper model is not installed; run `nas-subs models install`",
                code=ErrorCode.MODEL_MISSING,
                detail={"path_token": path_token(self.model_path)},
            )
        from faster_whisper import WhisperModel

        try:
            self._model = WhisperModel(
                str(self.model_path),
                device=self.config.asr.device,
                compute_type=self.config.asr.compute_type,
                cpu_threads=self.config.worker.cpu_threads,
                local_files_only=True,
            )
        except Exception as exc:
            raise NasSubtitlesError(
                "Whisper model could not be loaded offline",
                code=ErrorCode.MODEL_MISSING,
                detail={"path_token": path_token(self.model_path)},
            ) from exc
        return self._model


def chunk_checkpoint_path(config: AppConfig, *, job_id: str, chunk_index: int) -> Path:
    return config.work_dir / job_id / f"chunk-{chunk_index:04d}.transcript.json"


def write_chunk_checkpoint(
    path: Path,
    transcript: ChunkTranscript,
    *,
    job_id: str,
    fingerprint: MediaFingerprint,
    stage_config_hash: str,
) -> Path:
    """Write to a temporary file, flush, then rename on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "job_id": job_id,
        "fingerprint_digest": fingerprint.digest(),
        "stage_config_hash": stage_config_hash,
        "model_identity": _model_payload(transcript.model_identity),
        "language": transcript.language,
        "language_probability": transcript.language_probability,
        "chunk": {
            "index": transcript.chunk.index,
            "owned_start_seconds": transcript.chunk.owned_start_seconds,
            "owned_end_seconds": transcript.chunk.owned_end_seconds,
            "extract_start_seconds": transcript.chunk.extract_start_seconds,
            "extract_end_seconds": transcript.chunk.extract_end_seconds,
        },
        "segments": [_segment_payload(segment) for segment in transcript.segments],
    }
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(canonical_json(payload))
        handle.write("\n")
        handle.flush()
        os_fsync(handle)
    temporary.replace(path)
    return path


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
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        return None
    if payload.get("job_id") != job_id:
        return None
    if payload.get("fingerprint_digest") != fingerprint.digest():
        return None
    if payload.get("stage_config_hash") != stage_config_hash:
        return None
    stored_model = payload.get("model_identity")
    if isinstance(stored_model, dict):
        token = stored_model.get("identity_token")
        if token and token != model_identity.identity_token():
            return None
    try:
        chunk_raw = payload["chunk"]
        spec = AudioChunkSpec(
            index=int(chunk_raw["index"]),
            owned_start_seconds=float(chunk_raw["owned_start_seconds"]),
            owned_end_seconds=float(chunk_raw["owned_end_seconds"]),
            extract_start_seconds=float(chunk_raw["extract_start_seconds"]),
            extract_end_seconds=float(chunk_raw["extract_end_seconds"]),
        )
        segments = tuple(_segment_from_payload(item) for item in payload.get("segments") or [])
    except (KeyError, TypeError, ValueError):
        return None
    identity = model_identity
    if isinstance(stored_model, dict) and stored_model.get("path"):
        identity = ModelIdentity(
            kind=ModelKind(str(stored_model.get("kind") or ModelKind.ASR)),
            name=str(stored_model.get("name") or model_identity.name),
            path=Path(str(stored_model["path"])),
            version=stored_model.get("version")
            if isinstance(stored_model.get("version"), str)
            else None,
            sha256=stored_model.get("sha256")
            if isinstance(stored_model.get("sha256"), str)
            else None,
        )
    return ChunkTranscript(
        chunk=spec,
        language=str(payload.get("language") or ""),
        segments=segments,
        language_probability=(
            float(payload["language_probability"])
            if payload.get("language_probability") is not None
            else None
        ),
        model_identity=identity,
    )


def merge_chunk_transcripts(
    transcripts: Sequence[ChunkTranscript], *, duration_seconds: Seconds
) -> Transcript:
    """Order words globally, then drop duplicates across chunk boundaries.

    Only equivalent tokens with overlapping intervals are deduplicated, and
    the more confident version is kept. A phrase legitimately repeated at a
    different time stays.
    """
    words: list[Word] = []
    language = transcripts[0].language if transcripts else ""
    for transcript in transcripts:
        if transcript.language:
            language = transcript.language
        for segment in transcript.segments:
            owned = [word for word in segment.words if transcript.chunk.owns(word.midpoint_seconds)]
            if owned:
                words.extend(owned)
            elif transcript.chunk.owns(segment.start_seconds) or transcript.chunk.owns(
                (segment.start_seconds + segment.end_seconds) / 2.0
            ):
                words.extend(segment.words)
    words.sort(key=lambda word: (word.start_seconds, word.end_seconds, word.text))
    deduped = _dedupe_overlapping_words(words)
    segments = _segments_from_words(deduped)
    return Transcript(
        language=language,
        duration_seconds=duration_seconds,
        segments=segments,
        words=deduped,
    )


def os_fsync(handle: Any) -> None:
    handle.flush()
    try:
        import os

        os.fsync(handle.fileno())
    except OSError:
        return


def _absolute_segments(
    raw_segments: Sequence[Any], *, chunk: AudioChunk
) -> tuple[TranscriptSegment, ...]:
    offset = chunk.spec.extract_start_seconds
    built: list[TranscriptSegment] = []
    for index, raw in enumerate(raw_segments):
        start = offset + float(getattr(raw, "start", 0.0) or 0.0)
        end = offset + float(getattr(raw, "end", start) or start)
        words = tuple(_absolute_words(getattr(raw, "words", None), offset=offset, chunk=chunk))
        text = str(getattr(raw, "text", "") or "").strip()
        if not text and not words:
            continue
        midpoint = (start + end) / 2.0
        if words:
            owned_words = tuple(word for word in words if chunk.spec.owns(word.midpoint_seconds))
            if not owned_words:
                continue
            words = owned_words
            text = " ".join(word.text for word in words).strip()
            start = words[0].start_seconds
            end = words[-1].end_seconds
        elif not chunk.spec.owns(midpoint):
            continue
        built.append(
            TranscriptSegment(
                index=index,
                start_seconds=start,
                end_seconds=max(end, start),
                text=text,
                words=words,
                avg_logprob=getattr(raw, "avg_logprob", None),
                no_speech_probability=getattr(raw, "no_speech_probability", None),
                compression_ratio=getattr(raw, "compression_ratio", None),
                temperature=getattr(raw, "temperature", None),
                chunk_index=chunk.spec.index,
            )
        )
    return tuple(built)


def _absolute_words(raw_words: Any, *, offset: Seconds, chunk: AudioChunk) -> tuple[Word, ...]:
    if not raw_words:
        return ()
    words: list[Word] = []
    for raw in raw_words:
        text = str(getattr(raw, "word", None) or getattr(raw, "text", "") or "").strip()
        if not text:
            continue
        start = offset + float(getattr(raw, "start", 0.0) or 0.0)
        end = offset + float(getattr(raw, "end", start) or start)
        words.append(
            Word(
                text=text,
                start_seconds=start,
                end_seconds=max(end, start),
                probability=getattr(raw, "probability", None),
                chunk_index=chunk.spec.index,
            )
        )
    return tuple(words)


def _dedupe_overlapping_words(words: Sequence[Word]) -> tuple[Word, ...]:
    kept: list[Word] = []
    for word in words:
        if not kept:
            kept.append(word)
            continue
        previous = kept[-1]
        same = _normalize_token(previous.text) == _normalize_token(word.text)
        overlap = min(previous.end_seconds, word.end_seconds) > max(
            previous.start_seconds, word.start_seconds
        )
        if same and overlap:
            prev_score = previous.probability if previous.probability is not None else -1.0
            new_score = word.probability if word.probability is not None else -1.0
            if new_score > prev_score:
                kept[-1] = word
            continue
        kept.append(word)
    return tuple(kept)


def _normalize_token(text: str) -> str:
    return text.strip().casefold()


def _segments_from_words(words: Sequence[Word]) -> tuple[TranscriptSegment, ...]:
    if not words:
        return ()
    segments: list[TranscriptSegment] = []
    current: list[Word] = [words[0]]
    for word in words[1:]:
        gap = word.start_seconds - current[-1].end_seconds
        if gap > 0.8:
            segments.append(_segment_from_group(len(segments), current))
            current = [word]
        else:
            current.append(word)
    segments.append(_segment_from_group(len(segments), current))
    return tuple(segments)


def _segment_from_group(index: int, words: Sequence[Word]) -> TranscriptSegment:
    return TranscriptSegment(
        index=index,
        start_seconds=words[0].start_seconds,
        end_seconds=words[-1].end_seconds,
        text=" ".join(word.text for word in words).strip(),
        words=tuple(words),
        chunk_index=words[0].chunk_index,
    )


def _model_payload(identity: ModelIdentity | None) -> dict[str, object] | None:
    if identity is None:
        return None
    return {
        "kind": str(identity.kind),
        "name": identity.name,
        "path": str(identity.path),
        "version": identity.version,
        "sha256": identity.sha256,
        "identity_token": identity.identity_token(),
    }


def _segment_payload(segment: TranscriptSegment) -> dict[str, object]:
    return {
        "index": segment.index,
        "start_seconds": segment.start_seconds,
        "end_seconds": segment.end_seconds,
        "text": segment.text,
        "avg_logprob": segment.avg_logprob,
        "no_speech_probability": segment.no_speech_probability,
        "compression_ratio": segment.compression_ratio,
        "temperature": segment.temperature,
        "chunk_index": segment.chunk_index,
        "words": [
            {
                "text": word.text,
                "start_seconds": word.start_seconds,
                "end_seconds": word.end_seconds,
                "probability": word.probability,
                "chunk_index": word.chunk_index,
            }
            for word in segment.words
        ],
    }


def _segment_from_payload(payload: object) -> TranscriptSegment:
    if not isinstance(payload, dict):
        raise ValueError("segment is not an object")
    words_raw = payload.get("words") or []
    words = tuple(
        Word(
            text=str(item["text"]),
            start_seconds=float(item["start_seconds"]),
            end_seconds=float(item["end_seconds"]),
            probability=item.get("probability"),
            chunk_index=item.get("chunk_index"),
        )
        for item in words_raw
        if isinstance(item, dict)
    )
    return TranscriptSegment(
        index=int(payload["index"]),
        start_seconds=float(payload["start_seconds"]),
        end_seconds=float(payload["end_seconds"]),
        text=str(payload.get("text") or ""),
        words=words,
        avg_logprob=payload.get("avg_logprob"),
        no_speech_probability=payload.get("no_speech_probability"),
        compression_ratio=payload.get("compression_ratio"),
        temperature=payload.get("temperature"),
        chunk_index=payload.get("chunk_index"),
    )


def _stream_without_metadata() -> Any:
    from .domain import AudioStreamInfo

    return AudioStreamInfo(index=0)
