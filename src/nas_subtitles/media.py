"""FFprobe inspection and chunked audio extraction. Owned by stage 3.

Every external command is built as an argv list and run with ``shell=False``
and a timeout, so a filename containing spaces, Unicode or shell
metacharacters is never interpreted.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import AppConfig
from .domain import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_FORMAT,
    AUDIO_SAMPLE_RATE_HZ,
    AudioChunk,
    AudioChunkSpec,
    AudioStreamInfo,
    ErrorCode,
    NasSubtitlesError,
    ProbeResult,
    Seconds,
    SubtitleStreamInfo,
)
from .language import normalize_language_tag
from .logging_setup import path_token

__all__ = [
    "FfmpegAudioExtractor",
    "FfprobeMediaProbe",
    "ensure_free_space",
    "plan_chunks",
    "select_audio_stream",
]

_COMMENTARY_MARKERS = ("commentary", "comment", "description", "director")


class FfprobeMediaProbe:
    """``MediaProbe`` implementation shelling out to ``ffprobe -of json``."""

    def __init__(self, *, ffprobe_path: str = "ffprobe", timeout_seconds: float = 60.0) -> None:
        self.ffprobe_path = ffprobe_path
        self.timeout_seconds = timeout_seconds

    def probe(self, path: Path) -> ProbeResult:
        if not path.is_file():
            raise NasSubtitlesError(
                "media path is not a readable file",
                code=ErrorCode.INVALID_MEDIA,
                detail={"path_token": path_token(path)},
            )
        argv = [
            self.ffprobe_path,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
        raw = _run_command(argv, timeout_seconds=self.timeout_seconds)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NasSubtitlesError(
                "ffprobe produced output that is not JSON",
                code=ErrorCode.INVALID_MEDIA,
                detail={"path_token": path_token(path)},
            ) from exc
        if not isinstance(payload, dict):
            raise NasSubtitlesError(
                "ffprobe JSON was not an object",
                code=ErrorCode.INVALID_MEDIA,
                detail={"path_token": path_token(path)},
            )
        return _probe_from_payload(path, payload)


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
        return plan_chunks(
            duration_seconds=duration_seconds,
            stream_start_seconds=stream_start_seconds,
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
        )

    def extract(
        self,
        *,
        source: Path,
        stream_index: int,
        spec: AudioChunkSpec,
        destination: Path,
    ) -> AudioChunk:
        destination.parent.mkdir(parents=True, exist_ok=True)
        duration = spec.extract_duration_seconds
        argv = [
            self.ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{spec.extract_start_seconds:.3f}",
            "-i",
            str(source),
            "-t",
            f"{duration:.3f}",
            "-map",
            f"0:{stream_index}",
            "-vn",
            "-ac",
            str(AUDIO_CHANNELS),
            "-ar",
            str(AUDIO_SAMPLE_RATE_HZ),
            "-c:a",
            AUDIO_SAMPLE_FORMAT,
            str(destination),
        ]
        timeout = max(self.timeout_seconds, duration + 30.0)
        _run_command(argv, timeout_seconds=timeout)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise NasSubtitlesError(
                "ffmpeg did not produce an audio chunk",
                code=ErrorCode.SUBPROCESS_FAILED,
                detail={"chunk_index": spec.index, "path_token": path_token(destination)},
            )
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return AudioChunk(
            spec=spec,
            path=destination,
            sample_rate=AUDIO_SAMPLE_RATE_HZ,
            channels=AUDIO_CHANNELS,
            sha256=digest,
        )


def plan_chunks(
    *,
    duration_seconds: Seconds,
    stream_start_seconds: Seconds,
    chunk_seconds: Seconds,
    overlap_seconds: Seconds,
) -> tuple[AudioChunkSpec, ...]:
    """Pure chunk planner, shared by the real extractor and the test fakes.

    Ownership lives on the *video* timeline starting at 0. ``stream_start_seconds``
    only skips empty leading chunks whose owned interval ends before the audio
    stream exists; it never shifts owned bounds onto a relative stream clock.
    """
    if duration_seconds <= 0 or chunk_seconds <= 0:
        return ()
    count = max(1, math.ceil(duration_seconds / chunk_seconds))
    planned: list[AudioChunkSpec] = []
    for index in range(count):
        owned_start = index * chunk_seconds
        owned_end = min((index + 1) * chunk_seconds, duration_seconds)
        if owned_end <= stream_start_seconds:
            continue
        extract_start = max(0.0, owned_start - overlap_seconds)
        extract_end = min(duration_seconds, owned_end + overlap_seconds)
        planned.append(
            AudioChunkSpec(
                index=index,
                owned_start_seconds=owned_start,
                owned_end_seconds=owned_end,
                extract_start_seconds=extract_start,
                extract_end_seconds=extract_end,
            )
        )
    return tuple(planned)


def select_audio_stream(
    probe_result: ProbeResult, *, override_index: int | None = None
) -> AudioStreamInfo:
    """Pick the audio stream to transcribe.

    Priority: explicit global index, then a non-commentary ``eng``/``en``
    stream, then a Portuguese stream, then a non-commentary default, then the
    first remaining audio stream. Raises ``invalid_media`` when none exists.
    """
    streams = probe_result.audio_streams
    if override_index is not None:
        match = probe_result.stream_by_index(override_index)
        if match is None:
            raise NasSubtitlesError(
                f"no audio stream with global index {override_index}",
                code=ErrorCode.INVALID_MEDIA,
                detail={"audio_stream_index": override_index},
            )
        return match
    if not streams:
        raise NasSubtitlesError(
            "media has no audio stream",
            code=ErrorCode.INVALID_MEDIA,
            detail={"path_token": path_token(probe_result.path)},
        )

    def language_of(stream: AudioStreamInfo) -> str | None:
        return normalize_language_tag(stream.language) or normalize_language_tag(
            stream.raw_language_tag
        )

    english = [
        stream for stream in streams if language_of(stream) == "en" and not stream.is_commentary
    ]
    if english:
        return english[0]
    portuguese = [stream for stream in streams if language_of(stream) == "pt"]
    if portuguese:
        return portuguese[0]
    defaults = [stream for stream in streams if stream.is_default and not stream.is_commentary]
    if defaults:
        return defaults[0]
    remaining = [stream for stream in streams if not stream.is_commentary]
    if remaining:
        return remaining[0]
    return streams[0]


def ensure_free_space(config: AppConfig, path: Path) -> None:
    """Raise ``insufficient_space`` when the work filesystem is below the floor."""
    target = path if path.exists() else path.parent
    if not target.exists():
        raise NasSubtitlesError(
            "work directory does not exist",
            code=ErrorCode.INSUFFICIENT_SPACE,
            detail={"path_token": path_token(path)},
        )
    free_gib = shutil.disk_usage(target).free / 1024**3
    required = config.minimum_free_work_gib
    if free_gib < required:
        raise NasSubtitlesError(
            f"only {free_gib:.1f} GiB free, {required} GiB required",
            code=ErrorCode.INSUFFICIENT_SPACE,
            detail={"free_gib": round(free_gib, 3), "required_gib": required},
        )


def _run_command(argv: list[str], *, timeout_seconds: float) -> str:
    """Run ``argv`` with ``shell=False``. Never interpolates a filename into a shell."""
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        binary = argv[0] if argv else "command"
        raise NasSubtitlesError(
            f"{binary} is not installed",
            code=ErrorCode.SUBPROCESS_FAILED,
            detail={"binary": binary},
        ) from exc
    except PermissionError as exc:
        raise NasSubtitlesError(
            "permission denied running an external binary",
            code=ErrorCode.PERMISSION_DENIED,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise NasSubtitlesError(
            f"{argv[0]} exceeded {timeout_seconds:.0f}s",
            code=ErrorCode.SUBPROCESS_TIMEOUT,
            detail={"binary": argv[0]},
        ) from exc
    if completed.returncode != 0:
        raise NasSubtitlesError(
            f"{argv[0]} exited with status {completed.returncode}",
            code=ErrorCode.SUBPROCESS_FAILED,
            detail={"binary": argv[0], "status": completed.returncode},
        )
    return completed.stdout


def _as_mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _probe_from_payload(path: Path, payload: dict[str, Any]) -> ProbeResult:
    fmt = _as_mapping(payload.get("format"))
    duration = _optional_float(fmt.get("duration")) or 0.0
    size_bytes = _optional_int(fmt.get("size"))
    if size_bytes is None:
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = 0
    container = fmt.get("format_name") if isinstance(fmt.get("format_name"), str) else None
    audio: list[AudioStreamInfo] = []
    subs: list[SubtitleStreamInfo] = []
    for raw in payload.get("streams") or []:
        if not isinstance(raw, dict):
            continue
        codec_type = str(raw.get("codec_type") or "")
        if codec_type == "audio":
            audio.append(_audio_stream(raw, container_duration=duration))
        elif codec_type == "subtitle":
            subs.append(_subtitle_stream(raw))
    if duration <= 0:
        for stream in audio:
            if stream.duration_seconds and stream.duration_seconds > duration:
                duration = stream.duration_seconds
    return ProbeResult(
        path=path,
        duration_seconds=duration,
        size_bytes=size_bytes,
        container_format=container,
        audio_streams=tuple(audio),
        subtitle_streams=tuple(subs),
    )


def _audio_stream(raw: dict[str, Any], *, container_duration: Seconds) -> AudioStreamInfo:
    tags = _as_mapping(raw.get("tags"))
    disposition = _as_mapping(raw.get("disposition"))
    title = _optional_str(tags.get("title"))
    raw_language = _optional_str(tags.get("language"))
    return AudioStreamInfo(
        index=_require_int(raw.get("index"), field="index"),
        codec_name=_optional_str(raw.get("codec_name")),
        language=normalize_language_tag(raw_language),
        raw_language_tag=raw_language,
        channels=_optional_int(raw.get("channels")),
        sample_rate=_optional_int(raw.get("sample_rate")),
        start_time_seconds=_optional_float(raw.get("start_time")) or 0.0,
        duration_seconds=_optional_float(raw.get("duration")) or container_duration or None,
        title=title,
        is_default=_flag(disposition.get("default")),
        is_forced=_flag(disposition.get("forced")),
        is_commentary=_is_commentary(disposition, title),
    )


def _subtitle_stream(raw: dict[str, Any]) -> SubtitleStreamInfo:
    tags = _as_mapping(raw.get("tags"))
    disposition = _as_mapping(raw.get("disposition"))
    raw_language = _optional_str(tags.get("language"))
    return SubtitleStreamInfo(
        index=_require_int(raw.get("index"), field="index"),
        codec_name=_optional_str(raw.get("codec_name")),
        language=normalize_language_tag(raw_language),
        raw_language_tag=raw_language,
        title=_optional_str(tags.get("title")),
        is_default=_flag(disposition.get("default")),
        is_forced=_flag(disposition.get("forced")),
    )


def _is_commentary(disposition: dict[str, Any], title: str | None) -> bool:
    if _flag(disposition.get("comment")) or _flag(disposition.get("descriptions")):
        return True
    haystack = (title or "").lower()
    return any(marker in haystack for marker in _COMMENTARY_MARKERS)


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value not in {"0", "", "false", "False"}
    return False


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _require_int(value: object, *, field: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise NasSubtitlesError(
            f"ffprobe stream is missing a valid {field}",
            code=ErrorCode.INVALID_MEDIA,
        )
    return parsed
