"""Model bootstrap, verification and the model manifest.

Owned by stages 4 and 5. Downloads happen only in ``models install``, never
at import time, never at worker startup and never as a fallback to a remote
API. Everything else runs with ``local_files_only`` semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .config import AppConfig
from .domain import (
    ErrorCode,
    ModelIdentity,
    ModelKind,
    NasSubtitlesError,
    canonical_json,
)
from .logging_setup import path_token

__all__ = [
    "MODEL_MANIFEST_FILENAME",
    "asr_model_path",
    "configure_argos_environment",
    "install_models",
    "read_model_manifest",
    "translation_package_path",
    "verify_models",
    "write_model_manifest",
]

MODEL_MANIFEST_FILENAME = "models.json"
"""Lives in ``models_dir`` and records version, origin, checksum and licence."""

_WHISPER_REPO_PREFIX = "Systran/faster-whisper-"
_ARGOS_LICENSE = "MIT / CC-BY-SA (package metadata)"
_WHISPER_LICENSE = "MIT (faster-whisper) / Whisper weights per origin"


def asr_model_path(config: AppConfig) -> Path:
    """Absolute local directory of the Whisper model, never a hub alias."""
    return config.asr_models_dir / config.asr.model


def translation_package_path(config: AppConfig, *, source: str, target: str) -> Path:
    """Absolute local path of the installed Argos package for a direct pair."""
    configure_argos_environment(config)
    expected = config.translation_models_dir / f"{source}_{target}"
    if expected.is_dir() and any(expected.iterdir()):
        return expected
    if config.translation_models_dir.is_dir():
        for child in sorted(config.translation_models_dir.iterdir()):
            if not child.is_dir():
                continue
            name = child.name.lower()
            if name.startswith(f"{source}_") and name.endswith(f"_{target}"):
                return child
            if f"{source}_{target}" in name:
                return child
    raise NasSubtitlesError(
        f"direct {source}->{target} Argos package is not installed under models_dir",
        code=ErrorCode.TRANSLATION_PAIR_MISSING,
        detail={"path_token": path_token(config.translation_models_dir)},
    )


def configure_argos_environment(config: AppConfig) -> None:
    """Point Argos at ``models_dir`` before the package is imported."""
    config.translation_models_dir.mkdir(parents=True, exist_ok=True)
    os.environ["ARGOS_PACKAGES_DIR"] = str(config.translation_models_dir)
    os.environ["ARGOS_DEVICE_TYPE"] = "cpu"


def install_models(config: AppConfig) -> tuple[ModelIdentity, ...]:
    """Download the Whisper model and the direct Argos pair, then record them.

    Fails with a concrete instruction when the direct pair is unavailable
    rather than falling back to a pivot language or a remote service.
    """
    installed = (_install_whisper(config), _install_argos(config))
    write_model_manifest(config, installed)
    return installed


def verify_models(config: AppConfig, *, offline: bool = True) -> tuple[ModelIdentity, ...]:
    """Check that every model in the manifest is present and loadable offline."""
    del offline  # Offline is the only supported verification mode.
    identities = read_model_manifest(config)
    if not identities:
        raise NasSubtitlesError(
            "model manifest is missing; run `nas-subs models install`",
            code=ErrorCode.MODEL_MISSING,
        )
    verified: list[ModelIdentity] = []
    for identity in identities:
        if not identity.path.exists():
            raise NasSubtitlesError(
                f"model {identity.name} is missing at its recorded path",
                code=ErrorCode.MODEL_MISSING,
                detail={"name": identity.name, "path_token": path_token(identity.path)},
            )
        if identity.kind is ModelKind.ASR:
            _load_whisper_offline(config, identity.path)
        elif identity.kind is ModelKind.TRANSLATION:
            configure_argos_environment(config)
            translation_package_path(config, source="en", target="pt")
        verified.append(identity)
    return tuple(verified)


def read_model_manifest(config: AppConfig) -> tuple[ModelIdentity, ...]:
    path = config.models_dir / MODEL_MANIFEST_FILENAME
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NasSubtitlesError(
            "model manifest could not be read",
            code=ErrorCode.MODEL_MISSING,
        ) from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return ()
    return tuple(_identity_from_payload(item) for item in models if isinstance(item, dict))


def write_model_manifest(config: AppConfig, models: tuple[ModelIdentity, ...]) -> Path:
    config.models_dir.mkdir(parents=True, exist_ok=True)
    path = config.models_dir / MODEL_MANIFEST_FILENAME
    payload = {
        "models": [
            {
                "kind": str(model.kind),
                "name": model.name,
                "path": str(model.path),
                "version": model.version,
                "revision": model.revision,
                "sha256": model.sha256,
                "source": model.source,
                "license": model.license,
            }
            for model in models
        ]
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _install_whisper(config: AppConfig) -> ModelIdentity:
    destination = asr_model_path(config)
    destination.mkdir(parents=True, exist_ok=True)
    repo_id = f"{_WHISPER_REPO_PREFIX}{config.asr.model}"
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover - hub ships with faster-whisper
        raise NasSubtitlesError(
            "huggingface_hub is required for `models install`",
            code=ErrorCode.MODEL_MISSING,
        ) from exc
    try:
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(destination),
            local_files_only=False,
        )
    except Exception as exc:
        raise NasSubtitlesError(
            f"failed to download {repo_id}; rerun `nas-subs models install` on a networked machine",
            code=ErrorCode.MODEL_MISSING,
            detail={"repo": repo_id},
        ) from exc
    _load_whisper_offline(config, destination)
    digest = _directory_checksum(destination)
    return ModelIdentity(
        kind=ModelKind.ASR,
        name=config.asr.model,
        path=destination,
        version=config.asr.model,
        sha256=digest,
        source=repo_id,
        license=_WHISPER_LICENSE,
    )


def _install_argos(config: AppConfig) -> ModelIdentity:
    configure_argos_environment(config)
    import argostranslate.package as argos_package

    try:
        argos_package.update_package_index()
        available = argos_package.get_available_packages()
    except Exception as exc:
        raise NasSubtitlesError(
            "could not list Argos packages; the direct en->pt pair was not installed",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
        ) from exc
    match = [
        package
        for package in available
        if getattr(package, "from_code", None) == "en" and getattr(package, "to_code", None) == "pt"
    ]
    if not match:
        raise NasSubtitlesError(
            "no direct en->pt Argos package is published; install aborted (no pivot, no API)",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
        )
    package = match[0]
    try:
        downloaded = Path(str(package.download()))
        argos_package.install_from_path(downloaded)
    except Exception as exc:
        raise NasSubtitlesError(
            "failed to download or install the direct en->pt Argos package",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
        ) from exc
    installed = translation_package_path(config, source="en", target="pt")
    return ModelIdentity(
        kind=ModelKind.TRANSLATION,
        name="en-pt",
        path=installed,
        version=str(getattr(package, "package_version", None) or "unknown"),
        sha256=_directory_checksum(installed),
        source="argos-translate en->pt",
        license=_ARGOS_LICENSE,
    )


def _load_whisper_offline(config: AppConfig, model_path: Path) -> None:
    from faster_whisper import WhisperModel

    try:
        WhisperModel(
            str(model_path),
            device=config.asr.device,
            compute_type=config.asr.compute_type,
            cpu_threads=config.worker.cpu_threads,
            local_files_only=True,
        )
    except Exception as exc:
        raise NasSubtitlesError(
            "Whisper model could not be loaded offline from the local path",
            code=ErrorCode.MODEL_MISSING,
            detail={"path_token": path_token(model_path)},
        ) from exc


def _directory_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    for child in sorted(path.rglob("*")):
        if not child.is_file():
            continue
        digest.update(child.name.encode("utf-8"))
        digest.update(child.read_bytes())
    return digest.hexdigest()


def _identity_from_payload(payload: dict[str, Any]) -> ModelIdentity:
    return ModelIdentity(
        kind=ModelKind(str(payload["kind"])),
        name=str(payload["name"]),
        path=Path(str(payload["path"])),
        version=payload.get("version") if isinstance(payload.get("version"), str) else None,
        revision=payload.get("revision") if isinstance(payload.get("revision"), str) else None,
        sha256=payload.get("sha256") if isinstance(payload.get("sha256"), str) else None,
        source=payload.get("source") if isinstance(payload.get("source"), str) else None,
        license=payload.get("license") if isinstance(payload.get("license"), str) else None,
    )
