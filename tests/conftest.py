"""Shared fixtures.

Fixtures here must stay offline and synthetic. Tests that need a real model
belong behind the ``models`` marker; tests that need media should generate it
with FFmpeg rather than committing a file.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nas_subtitles.config import AppConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "config.example.yaml"


@pytest.fixture
def example_config_text() -> str:
    """The shipped example configuration, read verbatim."""
    return EXAMPLE_CONFIG.read_text(encoding="utf-8")


@pytest.fixture
def config_path(tmp_path: Path, example_config_text: str) -> Path:
    """The example configuration rewritten onto writable temporary directories."""
    directories = {
        "media": tmp_path / "media",
        "state": tmp_path / "state",
        "work": tmp_path / "work",
        "models": tmp_path / "models",
        "output": tmp_path / "output",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)

    text = example_config_text
    text = text.replace(
        "media_roots: [/media/library/series, /media/library/movies]",
        f"media_roots: [{directories['media']}]",
    )
    for name in ("state", "work", "models", "output"):
        text = text.replace(f"{name}_dir: /{name}", f"{name}_dir: {directories[name]}")

    destination = tmp_path / "config.yaml"
    destination.write_text(text, encoding="utf-8")
    return destination


@pytest.fixture
def config(config_path: Path) -> AppConfig:
    return load_config(config_path)


@pytest.fixture
def media_root(config: AppConfig) -> Path:
    return config.media_roots[0]


@pytest.fixture(autouse=True)
def _offline_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make accidental model downloads fail loudly instead of hitting the network."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("NO_PROXY", "*")
    yield
