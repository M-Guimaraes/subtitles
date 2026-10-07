"""Name filters, sidecar language, stability and explicit-path checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nas_subtitles.config import AppConfig
from nas_subtitles.discovery import (
    compute_fingerprint,
    find_existing_subtitles,
    has_portuguese_subtitle,
    is_candidate_name,
    is_stable,
    iter_candidate_files,
    observe,
    resolve_explicit_path,
    scan,
)
from nas_subtitles.domain import ErrorCode, NasSubtitlesError, ScanObservation
from nas_subtitles.repository import open_repository


def test_sample_token_and_part_files_are_rejected() -> None:
    assert is_candidate_name(Path("Show.S01E01.mkv"))
    assert is_candidate_name(Path("Show.S01E01.MKV"))
    assert not is_candidate_name(Path("sample.mkv"))
    assert not is_candidate_name(Path("Show.sample.mkv"))
    assert is_candidate_name(Path("sampler.mkv"))
    assert not is_candidate_name(Path("Show.S01E01.mkv.part"))
    assert not is_candidate_name(Path("download/Show.mkv"))
    assert not is_candidate_name(Path("Show.srt"))


def test_scanner_does_not_follow_symlinks(config: AppConfig, media_root: Path) -> None:
    real = media_root / "real"
    real.mkdir()
    video = real / "episode.mkv"
    video.write_bytes(b"x" * 32)
    outside = media_root.parent / "outside"
    outside.mkdir()
    target = outside / "secret.mkv"
    target.write_bytes(b"secret")
    link_dir = media_root / "linked"
    link_dir.symlink_to(outside)
    file_link = media_root / "alias.mkv"
    file_link.symlink_to(target)
    found = list(iter_candidate_files(config, config.roots[0]))
    names = {path.name for path in found}
    assert "episode.mkv" in names
    assert "secret.mkv" not in names
    assert "alias.mkv" not in names


def test_external_portuguese_sidecar_excludes_forced_and_bare_srt(
    media_root: Path,
) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"x")
    (media_root / "episode.srt").write_text("bare", encoding="utf-8")
    found = find_existing_subtitles(path=video)
    assert has_portuguese_subtitle(found) is False
    (media_root / "episode.forced.pt.srt").write_text("forced", encoding="utf-8")
    found = find_existing_subtitles(path=video)
    forced = [item for item in found if item.is_forced]
    assert forced
    assert all(not item.satisfies_target for item in forced)
    (media_root / "episode.pt-BR.srt").write_text("pt", encoding="utf-8")
    found = find_existing_subtitles(path=video)
    assert has_portuguese_subtitle(found) is True


def test_explicit_path_must_live_inside_a_root(config: AppConfig, tmp_path: Path) -> None:
    outsider = tmp_path / "other" / "film.mkv"
    outsider.parent.mkdir()
    outsider.write_bytes(b"x")
    with pytest.raises(NasSubtitlesError) as raised:
        resolve_explicit_path(config, outsider)
    assert raised.value.code is ErrorCode.MEDIA_PATH_OUTSIDE_ROOTS


def test_stability_requires_two_observations_and_minimum_age(config: AppConfig) -> None:
    now = datetime.now(tz=UTC)
    first = ScanObservation(
        root_id="r",
        relative_path="a.mkv",
        size_bytes=10,
        mtime_ns=int((now - timedelta(seconds=10)).timestamp() * 1_000_000_000),
        first_stable_seen_at=now,
        last_seen_at=now,
    )
    assert is_stable(config, first, now=now) is False
    second = ScanObservation(
        root_id="r",
        relative_path="a.mkv",
        size_bytes=10,
        mtime_ns=int(
            (now - timedelta(seconds=config.minimum_file_age_seconds + 5)).timestamp() * 1e9
        ),
        first_stable_seen_at=now - timedelta(seconds=config.stability_window_seconds + 1),
        last_seen_at=now,
    )
    assert is_stable(config, second, now=now) is True


def test_growing_file_resets_stability(config: AppConfig, media_root: Path) -> None:
    repo = open_repository(config)
    video = media_root / "growing.mkv"
    video.write_bytes(b"a" * 64)
    now = datetime.now(tz=UTC)
    first = observe(repo, root=config.roots[0], path=video, now=now)
    video.write_bytes(b"a" * 128)
    later = now + timedelta(seconds=1)
    second = observe(repo, root=config.roots[0], path=video, now=later)
    repo.close()
    assert first.size_bytes != second.size_bytes
    assert second.first_stable_seen_at == later
    assert is_stable(config, second, now=later) is False


def test_scan_refuses_a_missing_root(config: AppConfig, tmp_path: Path) -> None:
    missing = config.model_copy(update={"media_roots": (tmp_path / "no-such-library",)})
    repo = open_repository(config)
    with pytest.raises(NasSubtitlesError) as raised:
        scan(missing, repo)
    repo.close()
    assert raised.value.code is ErrorCode.MEDIA_ROOT_MISSING


def test_fingerprint_hashes_only_the_ends(config: AppConfig, media_root: Path) -> None:
    video = media_root / "clip.mkv"
    video.write_bytes(b"head" + b"\x00" * 100 + b"tail")
    fingerprint = compute_fingerprint(root=config.roots[0], path=video, audio_stream_index=1)
    assert len(fingerprint.head_sha256) == 64
    assert fingerprint.relative_path == "clip.mkv"
    assert fingerprint.audio_stream_index == 1
