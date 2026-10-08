"""Model verification and the offline Stanza wrap used by Argos."""

from __future__ import annotations

import builtins
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from nas_subtitles.config import AppConfig
from nas_subtitles.domain import ErrorCode, ModelIdentity, ModelKind, NasSubtitlesError
from nas_subtitles.models import (
    _adapt_stanza_resources,
    _download_stanza_tokenizer,
    _ensure_compatible_stanza,
    _overlay_stanza_bundle,
    _package_stanza_is_current,
    _stanza_bundle_is_current,
    configure_stanza_offline,
    to_argos_language_code,
    translation_package_path,
    verify_models,
    write_model_manifest,
)
from nas_subtitles.translation import ArgosTranslator


def _fake_stanza_module() -> tuple[types.SimpleNamespace, list[dict[str, Any]]]:
    recorded: list[dict[str, Any]] = []

    class FakePipeline:
        def __init__(self, *args: object, **kwargs: object) -> None:
            recorded.append(dict(kwargs))

    module = types.SimpleNamespace(Pipeline=FakePipeline)
    return module, recorded


def test_to_argos_language_code_maps_pt_br_and_passes_through() -> None:
    assert to_argos_language_code("pt-BR") == "pb"
    assert to_argos_language_code("en") == "en"
    assert to_argos_language_code("pt") == "pt"
    assert to_argos_language_code("pb") == "pb"
    assert to_argos_language_code("fr") == "fr"


def test_translation_package_path_resolves_pt_br_to_en_pb_not_en_pt(config: AppConfig) -> None:
    root = config.translation_models_dir
    european = root / "translate-en_pt-1_9"
    brazilian = root / "translate-en_pb-1_11"
    european.mkdir(parents=True)
    brazilian.mkdir(parents=True)
    (european / "package").write_text("european", encoding="utf-8")
    (brazilian / "package").write_text("brazilian", encoding="utf-8")

    found = translation_package_path(config, source="en", target="pt-BR")
    assert found == brazilian
    assert "en_pt" not in found.name


_CURRENT_STANZA_RESOURCES = (
    '{"en": {"tokenize": {"combined": {}}, "mwt": {"combined": {}},'
    ' "packages": {"default": {"tokenize": "combined", "mwt": "combined"}}}}'
)
_STALE_STANZA_RESOURCES = (
    '{"en": {"tokenize": {"ewt": {}}, "default_processors": {"tokenize": "ewt"}}}'
)


def _write_current_stanza_tree(root: Path) -> None:
    tokenize = root / "en" / "tokenize"
    mwt = root / "en" / "mwt"
    tokenize.mkdir(parents=True, exist_ok=True)
    mwt.mkdir(parents=True, exist_ok=True)
    (tokenize / "combined.pt").write_bytes(b"model")
    (mwt / "combined.pt").write_bytes(b"mwt")
    (root / "resources.json").write_text(_CURRENT_STANZA_RESOURCES, encoding="utf-8")


def _write_stale_en_pb(package: Path) -> None:
    tokenize = package / "stanza" / "en" / "tokenize"
    tokenize.mkdir(parents=True)
    (tokenize / "ewt.pt").write_bytes(b"old")
    (package / "stanza" / "resources.json").write_text(_STALE_STANZA_RESOURCES, encoding="utf-8")


def test_ensure_compatible_stanza_overlays_current_sibling(config: AppConfig) -> None:
    root = config.translation_models_dir
    donor = root / "translate-en_pt-1_9" / "stanza"
    tokenize = donor / "en" / "tokenize"
    tokenize.mkdir(parents=True)
    (tokenize / "combined.pt").write_bytes(b"model")
    (donor / "resources.json").write_text(
        '{"en": {"tokenize": {"combined": {}}, "packages": {"default": {"tokenize": "combined"}}}}',
        encoding="utf-8",
    )
    target = root / "translate-en_pb-1_9"
    _write_stale_en_pb(target)
    assert _package_stanza_is_current(target) is False
    _ensure_compatible_stanza(config, target)
    assert _package_stanza_is_current(target) is True
    assert (target / "stanza" / "en" / "tokenize" / "combined.pt").is_file()


def test_ensure_compatible_stanza_downloads_when_no_donor(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = config.translation_models_dir / "translate-en_pb-1_9"
    _write_stale_en_pb(target)
    recorded: list[dict[str, Any]] = []

    def fake_download(*args: object, **kwargs: object) -> None:
        recorded.append({"args": args, "kwargs": kwargs})
        model_dir = Path(str(kwargs["model_dir"]))
        _write_current_stanza_tree(model_dir)

    module = types.SimpleNamespace(download=fake_download)
    monkeypatch.setitem(sys.modules, "stanza", module)

    _ensure_compatible_stanza(config, target, allow_download=True)

    assert _package_stanza_is_current(target) is True
    assert (target / "stanza" / "en" / "tokenize" / "combined.pt").is_file()
    assert (target / "stanza" / "en" / "mwt" / "combined.pt").is_file()
    assert recorded
    assert recorded[0]["args"][0] == "en"
    assert recorded[0]["kwargs"]["package"] is None
    assert recorded[0]["kwargs"]["processors"] == {"tokenize": "combined", "mwt": "combined"}


def test_ensure_compatible_stanza_download_failure_does_not_accept_ewt(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = config.translation_models_dir / "translate-en_pb-1_9"
    _write_stale_en_pb(target)

    def fail_download(_model_dir: Path) -> None:
        raise NasSubtitlesError(
            "could not prepare compatible Stanza tokenizer for Argos package",
            code=ErrorCode.MODEL_MISSING,
        )

    monkeypatch.setattr("nas_subtitles.models._download_stanza_tokenizer", fail_download)

    with pytest.raises(NasSubtitlesError) as raised:
        _ensure_compatible_stanza(config, target, allow_download=True)
    assert raised.value.code is ErrorCode.MODEL_MISSING
    assert "could not prepare compatible Stanza tokenizer for Argos package" in raised.value.message
    assert _package_stanza_is_current(target) is False
    assert (target / "stanza" / "en" / "tokenize" / "ewt.pt").is_file()
    assert not (target / "stanza" / "en" / "tokenize" / "combined.pt").is_file()


def test_ensure_compatible_stanza_runtime_does_not_download(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = config.translation_models_dir / "translate-en_pb-1_9"
    _write_stale_en_pb(target)
    called = {"download": False}

    def unexpected(_model_dir: Path) -> None:
        called["download"] = True

    monkeypatch.setattr("nas_subtitles.models._download_stanza_tokenizer", unexpected)
    _ensure_compatible_stanza(config, target)
    assert called["download"] is False
    assert _package_stanza_is_current(target) is False


def test_download_stanza_tokenizer_requests_combined_processors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[dict[str, Any]] = []

    def fake_download(*args: object, **kwargs: object) -> None:
        recorded.append({"args": args, "kwargs": kwargs})

    monkeypatch.setitem(sys.modules, "stanza", types.SimpleNamespace(download=fake_download))
    _download_stanza_tokenizer(tmp_path / "stanza-dl")
    assert recorded[0]["args"][0] == "en"
    assert recorded[0]["kwargs"]["package"] is None
    assert recorded[0]["kwargs"]["processors"] == {"tokenize": "combined", "mwt": "combined"}


def test_overlay_stanza_bundle_copies_combined_files(tmp_path: Path) -> None:
    source = tmp_path / "downloaded"
    _write_current_stanza_tree(source)
    package = tmp_path / "translate-en_pb-1_9"
    _write_stale_en_pb(package)
    _overlay_stanza_bundle(source, package)
    assert _package_stanza_is_current(package) is True
    assert (package / "stanza" / "en" / "tokenize" / "combined.pt").is_file()
    assert (package / "stanza" / "en" / "mwt" / "combined.pt").is_file()


def test_translation_package_path_does_not_accept_en_pt_for_pt_br(config: AppConfig) -> None:
    root = config.translation_models_dir
    european = root / "translate-en_pt-1_9"
    european.mkdir(parents=True)
    (european / "package").write_text("european", encoding="utf-8")

    with pytest.raises(NasSubtitlesError) as raised:
        translation_package_path(config, source="en", target="pt-BR")
    assert raised.value.code is ErrorCode.TRANSLATION_PAIR_MISSING


def test_configure_stanza_offline_forces_download_method_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, recorded = _fake_stanza_module()
    monkeypatch.setitem(sys.modules, "stanza", module)

    configure_stanza_offline()
    module.Pipeline(lang="en", dir="/models/argos/pkg/stanza", processors="tokenize")

    assert recorded
    assert recorded[-1]["download_method"] is None


def test_stanza_bundle_is_current_requires_packages_and_model(tmp_path: Path) -> None:
    root = tmp_path / "argos"
    stanza = root / "translate-en_pt-1_9" / "stanza"
    tokenize = stanza / "en" / "tokenize"
    tokenize.mkdir(parents=True)
    (tokenize / "combined.pt").write_bytes(b"model")
    (stanza / "resources.json").write_text(
        '{"en": {"tokenize": {"combined": {}}, "packages": {"default": {"tokenize": "combined"}}}}',
        encoding="utf-8",
    )
    assert _stanza_bundle_is_current(root) is True
    stale = tmp_path / "stale"
    old = stale / "translate-en_pt-1_9" / "stanza" / "en" / "tokenize"
    old.mkdir(parents=True)
    (old / "ewt.pt").write_bytes(b"old")
    (stale / "translate-en_pt-1_9" / "stanza" / "resources.json").write_text(
        '{"en": {"tokenize": {"ewt": {}}, "default_processors": {"tokenize": "ewt"}}}',
        encoding="utf-8",
    )
    assert _stanza_bundle_is_current(stale) is False


def test_adapt_stanza_resources_adds_packages_default() -> None:
    resources = {
        "en": {
            "tokenize": {"ewt": {"md5": "abc"}},
            "default_processors": {"tokenize": "ewt"},
        }
    }
    adapted = _adapt_stanza_resources(resources)
    assert adapted["en"]["packages"]["default"]["tokenize"] == "ewt"
    _adapt_stanza_resources(adapted)
    assert adapted["en"]["packages"]["default"]["tokenize"] == "ewt"


def test_configure_stanza_offline_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    module, recorded = _fake_stanza_module()
    monkeypatch.setitem(sys.modules, "stanza", module)

    configure_stanza_offline()
    first = module.Pipeline
    configure_stanza_offline()
    second = module.Pipeline

    assert first is second
    assert getattr(second, "_nas_subtitles_offline", False) is True
    second(lang="en")
    assert recorded[-1]["download_method"] is None


def test_argos_translator_init_does_not_import_argos(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    imported: list[str] = []
    real_import = builtins.__import__

    def spy(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        imported.append(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", spy)
    ArgosTranslator(
        config,
        model_identity=ModelIdentity(
            kind=ModelKind.TRANSLATION, name="en-pt", path=Path("/models/argos/en_pt")
        ),
    )
    assert "argostranslate.translate" not in imported
    assert "stanza" not in imported


def test_ensure_loaded_configures_stanza_before_argos_import(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        "nas_subtitles.translation.configure_stanza_offline",
        lambda: order.append("stanza"),
    )
    monkeypatch.setattr(
        "nas_subtitles.translation.configure_argos_environment",
        lambda _config: order.append("env"),
    )
    monkeypatch.setattr(
        "nas_subtitles.translation.translation_package_path",
        lambda *_args, **_kwargs: Path("/models/argos/en_pt"),
    )
    real_import = builtins.__import__

    def spy(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "argostranslate.translate" or name.startswith("argostranslate"):
            order.append(f"import:{name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", spy)
    translator = ArgosTranslator(
        config,
        model_identity=ModelIdentity(
            kind=ModelKind.TRANSLATION, name="en-pt", path=Path("/models/argos/en_pt")
        ),
    )
    translator._ensure_loaded()

    assert "stanza" in order
    import_at = next(index for index, item in enumerate(order) if item.startswith("import:"))
    assert order.index("stanza") < import_at


def test_verify_models_does_not_pass_when_only_the_directory_exists(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    dummy = config.translation_models_dir / "en_pt"
    dummy.mkdir(parents=True)
    (dummy / "placeholder.txt").write_text("not an argos package", encoding="utf-8")
    write_model_manifest(
        config,
        (
            ModelIdentity(
                kind=ModelKind.TRANSLATION,
                name="en-pt",
                path=dummy,
            ),
        ),
    )
    monkeypatch.setattr("nas_subtitles.models.configure_stanza_offline", lambda: None)

    class _NoPair:
        @staticmethod
        def get_installed_languages() -> list[object]:
            return []

        @staticmethod
        def translate(*_args: object, **_kwargs: object) -> str:
            return "Olá."

    monkeypatch.setattr("nas_subtitles.models._import_argos_translate", lambda: _NoPair)

    with pytest.raises(NasSubtitlesError) as raised:
        verify_models(config, offline=True)
    assert raised.value.code is ErrorCode.TRANSLATION_PAIR_MISSING
