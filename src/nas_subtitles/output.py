"""SRT rendering, staging layout and exclusive publication. Owned by stage 6.

Publication writes a uniquely named temporary file in the *target* directory,
verifies and fsyncs it, then creates the final name with ``os.link``. An
existing name is never overwritten: ``EEXIST`` becomes ``output_conflict``,
and a filesystem without hard links becomes ``unsupported_atomic_publish``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .config import AppConfig, MediaRoot
from .domain import (
    JobManifest,
    JobRecord,
    PublishResult,
    SubtitleCue,
)

__all__ = [
    "PREVIEW_MARKER",
    "SrtSubtitleRenderer",
    "preview_path_for",
    "publish_exclusive",
    "sidecar_path_for",
    "staging_path_for",
    "supports_atomic_publish",
    "write_manifest",
]

PREVIEW_MARKER = ".preview"
"""A preview is written to staging only and can never become a sidecar."""


class SrtSubtitleRenderer:
    """``SubtitleRenderer`` implementation over the ``srt`` library."""

    def render(self, cues: Sequence[SubtitleCue]) -> str:
        """UTF-8 SRT, indices from 1, ``HH:MM:SS,mmm`` timestamps."""
        raise NotImplementedError("rendering is implemented in stage 6 (output)")

    def parse(self, content: str) -> tuple[SubtitleCue, ...]:
        raise NotImplementedError("rendering is implemented in stage 6 (output)")


def staging_path_for(config: AppConfig, job: JobRecord) -> Path:
    """``output_dir/<root_id>/<relative tree>/<stem>.pt-BR.srt``."""
    raise NotImplementedError("publication is implemented in stage 6 (output)")


def sidecar_path_for(config: AppConfig, root: MediaRoot, relative_path: str) -> Path:
    """``<stem>.pt-BR.srt`` beside the video; requires ``publish_mode: sidecar``."""
    raise NotImplementedError("publication is implemented in stage 6 (output)")


def preview_path_for(config: AppConfig, job: JobRecord) -> Path:
    """Staging path carrying :data:`PREVIEW_MARKER` in the name."""
    raise NotImplementedError("publication is implemented in stage 6 (output)")


def supports_atomic_publish(directory: Path) -> bool:
    """Probe hard-link support with the caller's own temporary file."""
    raise NotImplementedError("publication is implemented in stage 6 (output)")


def publish_exclusive(*, content: str, target: Path) -> PublishResult:
    """Create ``target`` exclusively; never overwrite, never partially write."""
    raise NotImplementedError("publication is implemented in stage 6 (output)")


def write_manifest(config: AppConfig, manifest: JobManifest) -> Path:
    """Write the manifest under ``state_dir``, never beside the video."""
    raise NotImplementedError("publication is implemented in stage 6 (output)")
