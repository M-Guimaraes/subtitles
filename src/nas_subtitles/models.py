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
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import AppConfig
from .domain import (
    ENGLISH,
    ErrorCode,
    ModelIdentity,
    ModelKind,
    NasSubtitlesError,
    canonical_json,
)
from .logging_setup import path_token

__all__ = [
    "ARGOS_LANGUAGE_CODES",
    "MODEL_MANIFEST_FILENAME",
    "asr_model_path",
    "configure_argos_environment",
    "configure_stanza_offline",
    "install_models",
    "load_separation_model",
    "load_tts_voice",
    "piper_voice_path",
    "read_model_manifest",
    "separation_model_path",
    "to_argos_language_code",
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

_PIPER_VOICE_REPO = "https://huggingface.co/rhasspy/piper-voices"
_PIPER_LICENSE = "MIT (piper) / voice weights licensed per rhasspy/piper-voices"
_DEMUCS_MODEL_NAME = "htdemucs"
"""Fase 2 benchmark baseline (roadmap 006): 4-stem split, vocals = dialogue."""
_DEMUCS_LICENSE = "MIT (demucs) / weights per adefossez/HTDemucs on the HF hub"

ARGOS_LANGUAGE_CODES = {"pt-BR": "pb"}
"""Public config tags that Argos names differently. ``pb`` is Argos-only."""


def to_argos_language_code(language: str) -> str:
    """Map a public language tag to the code Argos packages use.

    ``pt-BR`` becomes ``pb`` (Portuguese Brazil). Unknown codes pass through,
    so ``en`` stays ``en``. Callers keep using public tags in config, jobs and
    SRT names; only Argos install, verify and translate see ``pb``.
    """
    return ARGOS_LANGUAGE_CODES.get(language, language)


def _argos_pair(*, source: str, target: str) -> tuple[str, str]:
    return to_argos_language_code(source), to_argos_language_code(target)


def asr_model_path(config: AppConfig) -> Path:
    """Absolute local directory of the Whisper model, never a hub alias."""
    return config.asr_models_dir / config.asr.model


def piper_voice_path(config: AppConfig) -> Path:
    """Absolute local directory of the configured Piper voice."""
    return config.tts_models_dir / config.dubbing.voice


def separation_model_path(config: AppConfig) -> Path:
    """Absolute local directory of the installed dialogue-separation model."""
    return config.separation_models_dir / _DEMUCS_MODEL_NAME


def translation_package_path(config: AppConfig, *, source: str, target: str) -> Path:
    """Absolute local path of the installed Argos package for a direct pair.

    ``source`` and ``target`` are public tags (``en``, ``pt-BR``). The search
    uses Argos codes, so ``pt-BR`` resolves to a ``translate-en_pb-*`` package
    rather than European ``en_pt``.
    """
    configure_argos_environment(config)
    argos_source, argos_target = _argos_pair(source=source, target=target)
    for root in _argos_package_dirs(config):
        found = _find_pair_directory(root, source=argos_source, target=argos_target)
        if found is not None:
            _ensure_compatible_stanza(config, found)
            return found
    raise NasSubtitlesError(
        f"direct {argos_source}->{argos_target} Argos package is not installed under models_dir",
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


def _package_stanza_is_current(package_path: Path) -> bool:
    """True when this Argos package's Stanza files match Stanza 1.10."""
    resources_path = package_path / "stanza" / "resources.json"
    if not resources_path.is_file():
        return False
    try:
        payload = json.loads(resources_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    english = payload.get("en") if isinstance(payload, dict) else None
    if not isinstance(english, dict) or "packages" not in english:
        return False
    default = english.get("packages", {}).get("default")
    tokenize = english.get("tokenize")
    name = default.get("tokenize") if isinstance(default, dict) else None
    if not name or name == "ewt" or not isinstance(tokenize, dict) or name not in tokenize:
        return False
    model = package_path / "stanza" / "en" / "tokenize" / f"{name}.pt"
    return model.is_file()


def _stanza_bundle_is_current(packages_dir: Path) -> bool:
    """True when bundled Stanza resources match Stanza 1.10 (``packages`` + model file)."""
    if not packages_dir.is_dir():
        return False
    return any(
        child.is_dir() and _package_stanza_is_current(child) for child in packages_dir.iterdir()
    )


def _current_stanza_dir(config: AppConfig, *, exclude: Path | None = None) -> Path | None:
    excluded = exclude.resolve() if exclude is not None else None
    for root in _argos_package_dirs(config):
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if excluded is not None and child.resolve() == excluded:
                continue
            if _package_stanza_is_current(child):
                return child / "stanza"
    return None


def _overlay_stanza_bundle(source: Path, package_path: Path) -> None:
    """Copy a Stanza 1.10 tree onto ``package_path/stanza``."""
    destination = package_path / "stanza"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)


def _download_stanza_tokenizer(model_dir: Path) -> None:
    """Fetch current English tokenize/mwt weights into ``model_dir``.

    Install-only. ``stanza.download`` writes ``resources.json``,
    ``en/tokenize/combined.pt`` and ``en/mwt/combined.pt``.
    """
    try:
        stanza = importlib.import_module("stanza")
    except ImportError as exc:
        raise NasSubtitlesError(
            "could not prepare compatible Stanza tokenizer for Argos package",
            code=ErrorCode.MODEL_MISSING,
        ) from exc
    model_dir.mkdir(parents=True, exist_ok=True)
    try:
        stanza.download(
            "en",
            model_dir=str(model_dir),
            package=None,
            processors={"tokenize": "combined", "mwt": "combined"},
        )
    except NasSubtitlesError:
        raise
    except Exception as exc:
        raise NasSubtitlesError(
            "could not prepare compatible Stanza tokenizer for Argos package",
            code=ErrorCode.MODEL_MISSING,
        ) from exc


def _ensure_compatible_stanza(
    config: AppConfig, package_path: Path, *, allow_download: bool = False
) -> None:
    """Overlay a Stanza 1.10 bundle when the Argos package ships an older one.

    Official ``translate-en_pb-*`` packages still bundle ``ewt.pt``, which
    Stanza 1.10 cannot load (``feat_dropout``). A sibling package may already
    have ``combined.pt`` from a previous install; copying it is local and
    keeps verify/runtime offline. ``stanza.download`` runs only when
    ``allow_download`` is set (``models install``), never during translation.
    """
    if _package_stanza_is_current(package_path):
        return
    donor = _current_stanza_dir(config, exclude=package_path)
    if donor is not None:
        _overlay_stanza_bundle(donor, package_path)
        if _package_stanza_is_current(package_path) or not allow_download:
            return
    elif not allow_download:
        return
    with tempfile.TemporaryDirectory(prefix="nas-subs-stanza-") as raw:
        downloaded = Path(raw)
        _download_stanza_tokenizer(downloaded)
        _overlay_stanza_bundle(downloaded, package_path)
    if not _package_stanza_is_current(package_path):
        raise NasSubtitlesError(
            "could not prepare compatible Stanza tokenizer for Argos package",
            code=ErrorCode.MODEL_MISSING,
        )


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

    Argos 1.11 ships Stanza tokenize assets inside the translation package, but
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
    rather than falling back to a pivot language or a remote service. Also
    installs the dubbing models (roadmap 006 fase 2): the configured Piper
    voice and the Demucs baseline separation model.
    """
    installed = (
        _install_whisper(config),
        _install_argos(config),
        _install_piper(config),
        _install_demucs(config),
    )
    write_model_manifest(config, installed)
    return installed


def verify_models(config: AppConfig, *, offline: bool = True) -> tuple[ModelIdentity, ...]:
    """Check that every model in the manifest is present and loadable offline.

    A directory on disk is not enough: Whisper must load with
    ``local_files_only``, Argos must recognize the configured direct pair
    (``en->pb`` when the public target is ``pt-BR``) and translate a short
    probe phrase without touching the network, the Piper voice must load
    with ``PiperVoice.load``, and the Demucs separation model must load with
    ``HF_HUB_OFFLINE`` set — none of this touches the network.
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
        elif identity.kind is ModelKind.TTS:
            _load_piper_offline(config, identity.path)
        elif identity.kind is ModelKind.SEPARATION:
            _load_demucs_offline(config, identity.path)
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
    public_source = ENGLISH
    public_target = config.target_language
    argos_source, argos_target = _argos_pair(source=public_source, target=public_target)

    try:
        argos_package.update_package_index()
        available = argos_package.get_available_packages()
    except Exception as exc:
        raise NasSubtitlesError(
            f"could not list Argos packages; the direct {argos_source}->{argos_target} "
            "pair was not installed",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
        ) from exc
    match = [
        package
        for package in available
        if getattr(package, "from_code", None) == argos_source
        and getattr(package, "to_code", None) == argos_target
    ]
    if not match:
        raise NasSubtitlesError(
            f"no direct {argos_source}->{argos_target} Argos package is published; "
            "install aborted (no pivot, no API)",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
        )
    package = match[0]
    try:
        downloaded = Path(str(package.download()))
        argos_package.install_from_path(downloaded)
    except Exception as exc:
        raise NasSubtitlesError(
            f"failed to download or install the direct {argos_source}->{argos_target} "
            "Argos package",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
        ) from exc
    installed = _argos_runtime_package_path(source=argos_source, target=argos_target)
    if installed is None:
        installed = translation_package_path(config, source=public_source, target=public_target)
    _ensure_compatible_stanza(config, installed, allow_download=True)
    return ModelIdentity(
        kind=ModelKind.TRANSLATION,
        name=f"translation:{public_source}:{public_target}",
        path=installed,
        version=str(getattr(package, "package_version", None) or "unknown"),
        sha256=_directory_checksum(installed),
        source=f"argos-translate {argos_source}->{argos_target}",
        license=_ARGOS_LICENSE,
    )


def _verify_argos_offline(config: AppConfig) -> None:
    configure_argos_environment(config)
    configure_stanza_offline()
    public_source = ENGLISH
    public_target = config.target_language
    argos_source, argos_target = _argos_pair(source=public_source, target=public_target)
    translation_package_path(config, source=public_source, target=public_target)
    argos_translate = _import_argos_translate()
    configure_argos_environment(config)
    cache_clear = getattr(
        getattr(argos_translate, "get_installed_languages", None), "cache_clear", None
    )
    if callable(cache_clear):
        cache_clear()
    if not _argos_has_direct_pair(argos_translate, source=argos_source, target=argos_target):
        raise NasSubtitlesError(
            f"Argos does not recognize the installed {argos_source}->{argos_target} pair",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
        )
    try:
        output = argos_translate.translate(_ARGOS_PROBE_PHRASE, argos_source, argos_target)
    except Exception as exc:
        raise NasSubtitlesError(
            "Argos could not translate offline with the installed "
            f"{argos_source}->{argos_target} pair",
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
            "argostranslate is required to verify the translation pair",
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


def load_tts_voice(config: AppConfig) -> Any:
    """Load the installed Piper voice from disk, never from the network."""
    from piper import PiperVoice

    voice_dir = piper_voice_path(config)
    name = voice_dir.name
    try:
        return PiperVoice.load(
            voice_dir / f"{name}.onnx", config_path=voice_dir / f"{name}.onnx.json"
        )
    except Exception as exc:
        raise NasSubtitlesError(
            "Piper voice could not be loaded from the local path; run `nas-subs models install`",
            code=ErrorCode.MODEL_MISSING,
            detail={"path_token": path_token(voice_dir)},
        ) from exc


def load_separation_model(config: AppConfig) -> Any:
    """Load the installed Demucs bag with the hub forced offline."""
    return _load_demucs_offline(config, separation_model_path(config))


def _install_piper(config: AppConfig) -> ModelIdentity:
    from piper.download_voices import download_voice

    voice = config.dubbing.voice
    destination = piper_voice_path(config)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        download_voice(voice, destination)
    except Exception as exc:
        raise NasSubtitlesError(
            f"failed to download Piper voice {voice}; rerun `nas-subs models install` "
            "on a networked machine",
            code=ErrorCode.MODEL_MISSING,
            detail={"voice": voice},
        ) from exc
    _load_piper_offline(config, destination)
    digest = _directory_checksum(destination)
    return ModelIdentity(
        kind=ModelKind.TTS,
        name=voice,
        path=destination,
        version=voice,
        sha256=digest,
        source=f"{_PIPER_VOICE_REPO}/{voice}",
        license=_PIPER_LICENSE,
    )


def _load_piper_offline(config: AppConfig, voice_dir: Path) -> None:
    del config  # Piper needs no config-specific device/offline flags yet.
    from piper import PiperVoice

    voice_name = voice_dir.name
    model_path = voice_dir / f"{voice_name}.onnx"
    config_path = voice_dir / f"{voice_name}.onnx.json"
    try:
        PiperVoice.load(model_path, config_path=config_path)
    except Exception as exc:
        raise NasSubtitlesError(
            "Piper voice could not be loaded offline from the local path",
            code=ErrorCode.MODEL_MISSING,
            detail={"path_token": path_token(voice_dir)},
        ) from exc


def _install_demucs(config: AppConfig) -> ModelIdentity:
    destination = separation_model_path(config)
    destination.mkdir(parents=True, exist_ok=True)
    with _hf_home(destination):
        from demucs.pretrained import get_model

        try:
            get_model(_DEMUCS_MODEL_NAME)
        except Exception as exc:
            raise NasSubtitlesError(
                f"failed to download the {_DEMUCS_MODEL_NAME} separation model; "
                "rerun `nas-subs models install` on a networked machine",
                code=ErrorCode.MODEL_MISSING,
                detail={"model": _DEMUCS_MODEL_NAME},
            ) from exc
    model = _load_demucs_offline(config, destination)
    digest = _demucs_weights_checksum(model)
    return ModelIdentity(
        kind=ModelKind.SEPARATION,
        name=_DEMUCS_MODEL_NAME,
        path=destination,
        version=_DEMUCS_MODEL_NAME,
        sha256=digest,
        source=f"hf:adefossez/HTDemucs ({_DEMUCS_MODEL_NAME})",
        license=_DEMUCS_LICENSE,
    )


def _load_demucs_offline(config: AppConfig, destination: Path) -> Any:
    del config  # Demucs needs no config-specific device/offline flags yet.
    with _hf_home(destination, offline=True):
        from demucs.pretrained import get_model

        try:
            return get_model(_DEMUCS_MODEL_NAME)
        except Exception as exc:
            raise NasSubtitlesError(
                "separation model could not be loaded offline from the local path",
                code=ErrorCode.MODEL_MISSING,
                detail={"path_token": path_token(destination)},
            ) from exc


def _demucs_weights_checksum(model: Any) -> str:
    """Hash a Demucs bag's tensor weights directly.

    Independent of the HuggingFace hub cache layout under ``destination``:
    that directory also holds lock files, a refs pointer and (depending on
    the installed ``hf_xet`` version) shared content-addressed blobs that
    get materialized lazily, so hashing the directory tree is not stable
    across runs even when the weights themselves never change.
    """
    digest = hashlib.sha256()
    sub_models = getattr(model, "models", [model])
    for sub_model in sub_models:
        for name, tensor in sorted(sub_model.state_dict().items()):
            digest.update(name.encode("utf-8"))
            digest.update(tensor.detach().cpu().numpy().tobytes())
    return digest.hexdigest()


@contextmanager
def _hf_home(path: Path, *, offline: bool = False) -> Iterator[None]:
    """Point the HuggingFace hub cache at ``path`` for the duration of a call.

    Demucs resolves ``hf_hub_download`` lazily, so the cache directory only
    needs to be correct while the call is in flight. ``offline`` forces
    ``HF_HUB_OFFLINE`` so a verify can never silently reach the network.
    """
    previous_home = os.environ.get("HF_HOME")
    previous_offline = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HOME"] = str(path)
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        yield
    finally:
        if previous_home is None:
            os.environ.pop("HF_HOME", None)
        else:
            os.environ["HF_HOME"] = previous_home
        if offline:
            if previous_offline is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = previous_offline


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
