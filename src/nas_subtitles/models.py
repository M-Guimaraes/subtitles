"""Model bootstrap, verification and the model manifest.

Owned by stages 4 and 5. Downloads happen only in ``models install``, never
at import time, never at worker startup and never as a fallback to a remote
API. Everything else runs with ``local_files_only`` semantics.
"""

from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .domain import ModelIdentity

__all__ = [
    "MODEL_MANIFEST_FILENAME",
    "asr_model_path",
    "install_models",
    "read_model_manifest",
    "translation_package_path",
    "verify_models",
    "write_model_manifest",
]

MODEL_MANIFEST_FILENAME = "models.json"
"""Lives in ``models_dir`` and records version, origin, checksum and licence."""


def asr_model_path(config: AppConfig) -> Path:
    """Absolute local directory of the Whisper model, never a hub alias."""
    raise NotImplementedError("model bootstrap is implemented in stage 4 (asr)")


def translation_package_path(config: AppConfig, *, source: str, target: str) -> Path:
    """Absolute local path of the installed Argos package for a direct pair."""
    raise NotImplementedError("model bootstrap is implemented in stage 5 (translation)")


def install_models(config: AppConfig) -> tuple[ModelIdentity, ...]:
    """Download the Whisper model and the direct Argos pair, then record them.

    Fails with a concrete instruction when the direct pair is unavailable
    rather than falling back to a pivot language or a remote service.
    """
    raise NotImplementedError("model bootstrap is implemented in stage 4 (asr)")


def verify_models(config: AppConfig, *, offline: bool = True) -> tuple[ModelIdentity, ...]:
    """Check that every model in the manifest is present and loadable offline."""
    raise NotImplementedError("model verification is implemented in stage 4 (asr)")


def read_model_manifest(config: AppConfig) -> tuple[ModelIdentity, ...]:
    raise NotImplementedError("model bootstrap is implemented in stage 4 (asr)")


def write_model_manifest(config: AppConfig, models: tuple[ModelIdentity, ...]) -> Path:
    raise NotImplementedError("model bootstrap is implemented in stage 4 (asr)")
