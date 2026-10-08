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
    _stanza_bundle_is_current,
    configure_stanza_offline,
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
