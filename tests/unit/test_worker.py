"""Cleanup stays inside work_dir and backup uses sqlite backup."""

from __future__ import annotations

from pathlib import Path

from nas_subtitles.config import AppConfig
from nas_subtitles.repository import open_repository
from nas_subtitles.worker import backup_state, cleanup_work_dir


def test_cleanup_ignores_non_job_directories_and_media(config: AppConfig, media_root: Path) -> None:
    repo = open_repository(config)
    stray = config.work_dir / "not-a-uuid"
    stray.mkdir()
    (stray / "chunk.wav").write_bytes(b"x")
    media = media_root / "keep.mkv"
    media.write_bytes(b"media")
    summary = cleanup_work_dir(config, repo, older_than_days=0, dry_run=False)
    repo.close()
    assert (stray / "chunk.wav").is_file()
    assert media.is_file()
    assert summary.inspected_jobs == 0


def test_backup_creates_a_readable_copy(config: AppConfig, tmp_path: Path) -> None:
    repo = open_repository(config)
    repo.close()
    folder = backup_state(config, tmp_path / "backups")
    assert (folder / "jobs.sqlite3").is_file()
