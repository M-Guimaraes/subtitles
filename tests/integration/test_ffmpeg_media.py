"""Synthetic FFmpeg fixtures: multi-audio, start_time, and awkward filenames."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from nas_subtitles.config import AppConfig
from nas_subtitles.media import (
    FfmpegAudioExtractor,
    FfprobeMediaProbe,
    plan_chunks,
    select_audio_stream,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required"),
    pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is required"),
]


def _run(argv: list[str]) -> None:
    completed = subprocess.run(
        argv, shell=False, check=False, capture_output=True, text=True, timeout=60
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)


def test_probe_selects_english_by_global_index(tmp_path: Path) -> None:
    destination = tmp_path / "multi.mkv"
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:a:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "language=por",
            "-metadata:s:a:1",
            "language=eng",
            str(destination),
        ]
    )
    probed = FfprobeMediaProbe().probe(destination)
    assert len(probed.audio_streams) == 2
    indexes = {stream.index for stream in probed.audio_streams}
    assert 0 not in indexes  # 0 is the video stream
    selected = select_audio_stream(probed)
    assert selected.language == "en"
    overridden = select_audio_stream(probed, override_index=selected.index)
    assert overridden.index == selected.index


def test_extraction_uses_absolute_times_with_non_zero_start(tmp_path: Path) -> None:
    destination = tmp_path / "offset.mkv"
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:d=3",
            "-itsoffset",
            "1.25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(destination),
        ]
    )
    probed = FfprobeMediaProbe().probe(destination)
    stream = select_audio_stream(probed)
    assert stream.start_time_seconds >= 0.0
    specs = plan_chunks(
        duration_seconds=probed.duration_seconds,
        stream_start_seconds=stream.start_time_seconds,
        chunk_seconds=300.0,
        overlap_seconds=2.0,
    )
    assert specs[0].owned_start_seconds == 0.0
    chunk = FfmpegAudioExtractor().extract(
        source=destination,
        stream_index=stream.index,
        spec=specs[0],
        destination=tmp_path / "chunk.wav",
    )
    assert chunk.path.is_file()
    assert chunk.spec.owned_start_seconds == 0.0


def test_awkward_filenames_are_passed_as_argv(tmp_path: Path) -> None:
    destination = tmp_path / "My Show (1) & 'quotes'.mkv"
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(destination),
        ]
    )
    probed = FfprobeMediaProbe().probe(destination)
    assert probed.duration_seconds > 0
    stream = select_audio_stream(probed)
    specs = plan_chunks(
        duration_seconds=probed.duration_seconds,
        stream_start_seconds=0.0,
        chunk_seconds=300.0,
        overlap_seconds=0.0,
    )
    chunk = FfmpegAudioExtractor().extract(
        source=destination,
        stream_index=stream.index,
        spec=specs[0],
        destination=tmp_path / "out.wav",
    )
    assert chunk.path.stat().st_size > 0


def test_inspect_cli_reads_synthetic_media(
    config: AppConfig, config_path: Path, media_root: Path
) -> None:
    from typer.testing import CliRunner

    from nas_subtitles.cli import app
    from nas_subtitles.domain import ExitCode

    video = media_root / "clip.mkv"
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(video),
        ]
    )
    result = CliRunner().invoke(
        app, ["inspect", str(video), "--config", str(config_path), "--json"]
    )
    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    assert "selected_audio_stream_index" in result.stdout
