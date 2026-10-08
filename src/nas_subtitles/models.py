"""Model bootstrap, verification and the model manifest.

Owned by stages 4 and 5. Downloads happen only in ``models install``, never
at import time, never at worker startup and never as a fallback to a remote
API. Everything else runs with ``local_files_only`` semantics.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
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
    "configure_stanza_offline",
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
_ARGOS_PROBE_PHRASE = "Hello."
_STANZA_OFFLINE_FLAG = "_nas_subtitles_offline"


def asr_model_path(config: AppConfig) -> Path:
    """Absolute local directory of the Whisper model, never a hub alias."""
    return config.asr_models_dir / config.asr.model


def translation_package_path(config: AppConfig, *, source: str, target: str) -> Path:
    """Absolute local path of the installed Argos package for a direct pair."""
    configure_argos_environment(config)
    for root in _argos_package_dirs(config):
        found = _find_pair_directory(root, source=source, target=target)
        if found is not None:
            return found
    raise NasSubtitlesError(
        f"direct {source}->{target} Argos package is not installed under models_dir",
        code=ErrorCode.TRANSLATION_PAIR_MISSING,
        detail={"path_token": path_token(config.translation_models_dir)},
    )


def _find_pair_directory(root: Path, *, source: str, target: str) -> Path | None:
    if not root.is_dir():
        return None
    expected = root / f"{source}_{target}"
    if expected.is_dir() and any(expected.iterdir()):
        return expected
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name.lower()
        if name.startswith(f"{source}_") and name.endswith(f"_{target}"):
            return child
        if f"{source}_{target}" in name:
            return child
    return None


def _xdg_argos_packages_dir() -> Path | None:
    xdg = os.environ.get("XDG_DATA_HOME")
    if not xdg:
        return None
    path = Path(xdg) / "argos-translate" / "packages"
    return path if path.is_dir() else None


def _stanza_bundle_is_current(packages_dir: Path) -> bool:
    """True when bundled Stanza resources match Stanza 1.10 (``packages`` + model file)."""
    if not packages_dir.is_dir():
        return False
    for child in packages_dir.iterdir():
        resources_path = child / "stanza" / "resources.json"
        if not resources_path.is_file():
            continue
        try:
            payload = json.loads(resources_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        english = payload.get("en") if isinstance(payload, dict) else None
        if not isinstance(english, dict) or "packages" not in english:
            continue
        default = english.get("packages", {}).get("default")
        tokenize = english.get("tokenize")
        name = default.get("tokenize") if isinstance(default, dict) else None
        if not name or not isinstance(tokenize, dict) or name not in tokenize:
            continue
        model = child / "stanza" / "en" / "tokenize" / f"{name}.pt"
        if model.is_file():
            return True
    return False


def _argos_package_dirs(config: AppConfig) -> tuple[Path, ...]:
    """Search order: Stanza-current copies first, then ``models_dir/argos``."""
    primary = config.translation_models_dir
    candidates = [primary]
    extra = _xdg_argos_packages_dir()
    if extra is not None and extra.resolve() != primary.resolve():
        candidates.append(extra)
    current = [path for path in candidates if _stanza_bundle_is_current(path)]
    rest = [path for path in candidates if path not in current]
    ordered: list[Path] = []
    for path in (*current, *rest):
        if path not in ordered:
            ordered.append(path)
    return tuple(ordered) or (primary,)


def configure_argos_environment(config: AppConfig) -> None:
    """Point Argos at ``models_dir`` before the package is imported.

    ``argostranslate.settings`` reads ``ARGOS_PACKAGES_DIR`` at import time.
    If that module is already loaded, rebind its package directories so the
    runtime view matches the path recorded in the model manifest.
    """
    install_dir = config.translation_models_dir
    install_dir.mkdir(parents=True, exist_ok=True)
    search_dirs = _argos_package_dirs(config)
    os.environ["ARGOS_PACKAGES_DIR"] = str(search_dirs[0])
    os.environ["ARGOS_DEVICE_TYPE"] = "cpu"
    settings = sys.modules.get("argostranslate.settings")
    if settings is not None:
        unbound: Any = settings
        unbound.package_data_dir = install_dir
        unbound.package_dirs = list(search_dirs)
        unbound.device = "cpu"
    translate = sys.modules.get("argostranslate.translate")
    cache_clear = getattr(getattr(translate, "get_installed_languages", None), "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


def configure_stanza_offline() -> None:
    """Force every ``stanza.Pipeline`` used by Argos to stay offline.

    Argos 1.11 ships Stanza tokenize assets inside the ``en->pt`` package, but
    Stanza 1.10 still defaults to ``download_method=DOWNLOAD_RESOURCES``, which
    hits ``raw.githubusercontent.com`` even when those files are present. The
    bundled ``resources.json`` is also an older schema without a ``packages``
    key; we adapt it in memory. Both wraps are applied once, before Argos
    constructs ``StanzaSentencizer``.
    """
    try:
        stanza = importlib.import_module("stanza")
    except ImportError as exc:
        raise NasSubtitlesError(
            "stanza is required for offline Argos sentence splitting",
            code=ErrorCode.MODEL_MISSING,
        ) from exc
    stanza_module: Any = stanza
    original = stanza_module.Pipeline
    if getattr(original, _STANZA_OFFLINE_FLAG, False):
        return

    _patch_stanza_resource_loader()

    def offline_pipeline(*args: Any, **kwargs: Any) -> Any:
        kwargs["download_method"] = None
        return original(*args, **kwargs)

    setattr(offline_pipeline, _STANZA_OFFLINE_FLAG, True)
    stanza_module.Pipeline = offline_pipeline
    try:
        core: Any = importlib.import_module("stanza.pipeline.core")
    except ImportError:
        return
    if getattr(core, "Pipeline", None) is original:
        core.Pipeline = offline_pipeline


def _adapt_stanza_resources(resources: Any) -> Any:
    """Add a synthetic ``packages.default`` map for Argos-bundled resources."""
    if not isinstance(resources, dict):
        return resources
    for info in resources.values():
        if not isinstance(info, dict) or "packages" in info:
            continue
        default_processors = info.get("default_processors")
        if isinstance(default_processors, dict):
            info["packages"] = {"default": dict(default_processors)}
    return resources


def _patch_stanza_resource_loader() -> None:
    def wrap_loader(original_load: Any) -> Any:
        if getattr(original_load, _STANZA_OFFLINE_FLAG, False):
            return original_load

        def load_resources_json(*args: Any, **kwargs: Any) -> Any:
            return _adapt_stanza_resources(original_load(*args, **kwargs))

        setattr(load_resources_json, _STANZA_OFFLINE_FLAG, True)
        return load_resources_json

    for module_name in ("stanza.resources.common", "stanza.pipeline.core"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        target: Any = module
        loader = getattr(target, "load_resources_json", None)
        if loader is None:
            continue
        target.load_resources_json = wrap_loader(loader)


def install_models(config: AppConfig) -> tuple[ModelIdentity, ...]:
    """Download the Whisper model and the direct Argos pair, then record them.

    Fails with a concrete instruction when the direct pair is unavailable
    rather than falling back to a pivot language or a remote service.
    """
    installed = (_install_whisper(config), _install_argos(config))
    write_model_manifest(config, installed)
    return installed


def verify_models(config: AppConfig, *, offline: bool = True) -> tuple[ModelIdentity, ...]:
    """Check that every model in the manifest is present and loadable offline.

    A directory on disk is not enough: Whisper must load with
    ``local_files_only``, and Argos must recognize ``en->pt`` and translate a
    short probe phrase without touching the network.
    """
    del offline  # Verification never reaches the network.
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
            _verify_argos_offline(config)
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

    configure_argos_environment(config)

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
    installed = _argos_runtime_package_path(source="en", target="pt")
    if installed is None:
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


def _verify_argos_offline(config: AppConfig) -> None:
    configure_argos_environment(config)
    configure_stanza_offline()
    translation_package_path(config, source="en", target="pt")
    argos_translate = _import_argos_translate()
    configure_argos_environment(config)
    cache_clear = getattr(
        getattr(argos_translate, "get_installed_languages", None), "cache_clear", None
    )
    if callable(cache_clear):
        cache_clear()
    if not _argos_has_direct_pair(argos_translate, source="en", target="pt"):
        raise NasSubtitlesError(
            "Argos does not recognize the installed en->pt pair",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
        )
    try:
        output = argos_translate.translate(_ARGOS_PROBE_PHRASE, "en", "pt")
    except Exception as exc:
        raise NasSubtitlesError(
            "Argos could not translate offline with the installed en->pt pair",
            code=ErrorCode.MODEL_MISSING,
        ) from exc
    if not str(output or "").strip():
        raise NasSubtitlesError(
            "Argos produced empty text for a non-empty probe phrase",
            code=ErrorCode.EMPTY_TRANSLATION,
        )


def _import_argos_translate() -> Any:
    try:
        return importlib.import_module("argostranslate.translate")
    except ImportError as exc:
        raise NasSubtitlesError(
            "argostranslate is required to verify the en->pt pair",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
        ) from exc


def _argos_has_direct_pair(argos_translate: Any, *, source: str, target: str) -> bool:
    try:
        languages = list(argos_translate.get_installed_languages())
    except Exception:
        return False
    by_code = {getattr(language, "code", None): language for language in languages}
    from_language = by_code.get(source)
    to_language = by_code.get(target)
    if from_language is None or to_language is None:
        return False
    get_translation = getattr(from_language, "get_translation", None)
    if not callable(get_translation):
        return False
    try:
        return get_translation(to_language) is not None
    except Exception:
        return False


def _argos_runtime_package_path(*, source: str, target: str) -> Path | None:
    try:
        argos_package = importlib.import_module("argostranslate.package")
        installed = argos_package.get_installed_packages()
    except Exception:
        return None
    for package in installed:
        if getattr(package, "from_code", None) != source:
            continue
        if getattr(package, "to_code", None) != target:
            continue
        raw = getattr(package, "package_path", None)
        if raw is None:
            continue
        path = Path(str(raw))
        if path.is_dir():
            return path
    return None


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
