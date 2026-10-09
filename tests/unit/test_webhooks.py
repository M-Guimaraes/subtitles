"""Sonarr/Radarr webhooks: auth, path safety, and the shared enqueue path."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nas_subtitles.cli import app
from nas_subtitles.config import AppConfig, WebhookPathMap, load_config
from nas_subtitles.discovery import observe, scan
from nas_subtitles.domain import AudioStreamInfo, ErrorCode, NasSubtitlesError, ProbeResult
from nas_subtitles.repository import StateDirLock, open_repository
from nas_subtitles.webhooks import (
    WEBHOOK_TOKEN_ENV,
    create_server,
    extract_imported_paths,
    ingest_webhook,
    map_imported_path,
    parse_arr_webhook,
    resolve_webhook_bind,
)

runner = CliRunner()

_TOKEN = "webhook-secret"


class _FakeProbe:
    def probe(self, path: Path) -> ProbeResult:
        size = path.stat().st_size if path.is_file() else 0
        return ProbeResult(
            path=path,
            duration_seconds=2.0,
            size_bytes=size,
            audio_streams=(AudioStreamInfo(index=1, codec_name="aac", language="en"),),
        )


def _fast_config(config: AppConfig) -> AppConfig:
    return config.model_copy(update={"stability_window_seconds": 60, "minimum_file_age_seconds": 0})


def _with_token(config: AppConfig, token: str = _TOKEN) -> AppConfig:
    return config.model_copy(update={"webhooks": config.webhooks.model_copy(update={"token": token})})


def _with_maps(config: AppConfig, host_prefix: Path, container_prefix: Path) -> AppConfig:
    mapping = WebhookPathMap(host_prefix=host_prefix, container_prefix=container_prefix)
    return config.model_copy(
        update={
            "webhooks": config.webhooks.model_copy(
                update={"token": _TOKEN, "path_maps": (mapping,)}
            )
        }
    )


def _stabilize(config: AppConfig, repo: object, path: Path, *, now: datetime) -> None:
    root = config.roots[0]
    age = config.stability_window_seconds + 5
    stamp = now.timestamp() - age
    os.utime(path, (stamp, stamp))
    first = now - timedelta(seconds=config.stability_window_seconds + 1)
    observe(repo, root=root, path=path, now=first)  # type: ignore[arg-type]
    observe(repo, root=root, path=path, now=now)  # type: ignore[arg-type]


def _sonarr_payload(path: Path, *, event_type: str = "Download") -> dict[str, object]:
    return {
        "eventType": event_type,
        "instanceName": "Sonarr",
        "series": {"title": "Show", "path": str(path.parent)},
        "episodeFile": {
            "relativePath": path.name,
            "path": str(path),
            "size": 32,
        },
    }


def _radarr_payload(path: Path, *, event_type: str = "Download") -> dict[str, object]:
    return {
        "eventType": event_type,
        "instanceName": "Radarr",
        "movie": {"title": "Film", "folderPath": str(path.parent)},
        "movieFile": {
            "relativePath": path.name,
            "path": str(path),
            "size": 32,
        },
    }


def test_parse_sonarr_and_radarr_download_events(media_root: Path) -> None:
    episode = media_root / "Show.S01E01.mkv"
    movie = media_root / "Film.mkv"
    sonarr = parse_arr_webhook(_sonarr_payload(episode))
    radarr = parse_arr_webhook(_radarr_payload(movie))
    assert sonarr.kind == "import"
    assert sonarr.source == "sonarr"
    assert sonarr.raw_paths == (str(episode),)
    assert radarr.kind == "import"
    assert radarr.source == "radarr"
    assert radarr.raw_paths == (str(movie),)


def test_test_event_is_acknowledged_without_a_path() -> None:
    event = parse_arr_webhook({"eventType": "Test"})
    assert event.kind == "test"
    assert event.raw_paths == ()


def test_unsupported_event_is_ignored() -> None:
    event = parse_arr_webhook({"eventType": "Grab", "series": {"title": "Show"}})
    assert event.kind == "ignored"


def test_missing_event_type_fails_closed() -> None:
    with pytest.raises(NasSubtitlesError) as raised:
        parse_arr_webhook({"episodeFile": {"path": "/media/a.mkv"}})
    assert raised.value.code is ErrorCode.CONFIG_INVALID


def test_import_without_path_fails_closed() -> None:
    with pytest.raises(NasSubtitlesError) as raised:
        parse_arr_webhook({"eventType": "Download", "series": {"title": "Show"}})
    assert raised.value.code is ErrorCode.CONFIG_INVALID


def test_extracts_episode_files_array(media_root: Path) -> None:
    first = media_root / "a.mkv"
    second = media_root / "b.mkv"
    paths = extract_imported_paths(
        {
            "eventType": "Download",
            "episodeFiles": [{"path": str(first)}, {"path": str(second)}],
        }
    )
    assert paths == (str(first), str(second))


def test_valid_sonarr_import_uses_the_same_enqueue_path(
    config: AppConfig, media_root: Path
) -> None:
    video = media_root / "Show.S01E01.mkv"
    video.write_bytes(b"x" * 32)
    repo = open_repository(config)
    result = ingest_webhook(config, repo, _sonarr_payload(video), probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert result["ok"] is True
    assert result["action"] == "enqueued"
    assert result["source"] == "sonarr"
    assert result["enqueued"] is True
    assert len(jobs) == 1
    assert jobs[0].id == result["job_id"]
    assert jobs[0].relative_path == "Show.S01E01.mkv"


def test_valid_radarr_import_enqueues(config: AppConfig, media_root: Path) -> None:
    video = media_root / "Film.2020.mkv"
    video.write_bytes(b"y" * 32)
    repo = open_repository(config)
    result = ingest_webhook(config, repo, _radarr_payload(video), probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert result["action"] == "enqueued"
    assert result["source"] == "radarr"
    assert jobs[0].relative_path == "Film.2020.mkv"


def test_duplicate_webhook_is_idempotent(config: AppConfig, media_root: Path) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"z" * 32)
    repo = open_repository(config)
    first = ingest_webhook(config, repo, _sonarr_payload(video), probe=_FakeProbe())
    second = ingest_webhook(config, repo, _sonarr_payload(video), probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert first["action"] == "enqueued"
    assert second["action"] == "already_queued"
    assert first["job_id"] == second["job_id"]
    assert len(jobs) == 1


def test_scan_still_finds_files_the_webhook_missed(
    config: AppConfig, media_root: Path
) -> None:
    config = _fast_config(config)
    hooked = media_root / "hooked.mkv"
    missed = media_root / "missed.mkv"
    hooked.write_bytes(b"h" * 32)
    missed.write_bytes(b"m" * 32)
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    ingest_webhook(config, repo, _sonarr_payload(hooked), probe=_FakeProbe())
    _stabilize(config, repo, hooked, now=now)
    _stabilize(config, repo, missed, now=now)
    summary = scan(config, repo, now=now, probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert summary.enqueued == 1
    assert summary.already_queued == 1
    relative = {job.relative_path for job in jobs}
    assert relative == {"hooked.mkv", "missed.mkv"}


def test_path_outside_roots_is_rejected(config: AppConfig, tmp_path: Path) -> None:
    outsider = tmp_path / "other" / "film.mkv"
    outsider.parent.mkdir()
    outsider.write_bytes(b"x")
    repo = open_repository(config)
    with pytest.raises(NasSubtitlesError) as raised:
        ingest_webhook(config, repo, _radarr_payload(outsider), probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert raised.value.code is ErrorCode.MEDIA_PATH_OUTSIDE_ROOTS
    assert jobs == ()


def test_path_traversal_is_rejected(config: AppConfig, media_root: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.mkv"
    secret.write_bytes(b"no")
    raw = str(media_root / ".." / secret.name)
    repo = open_repository(config)
    with pytest.raises(NasSubtitlesError) as raised:
        map_imported_path(config, raw)
    repo.close()
    assert raised.value.code is ErrorCode.MEDIA_PATH_OUTSIDE_ROOTS


def test_path_map_rewrites_host_prefix(config: AppConfig, media_root: Path, tmp_path: Path) -> None:
    video = media_root / "mapped.mkv"
    video.write_bytes(b"map")
    host_root = tmp_path / "host-library"
    config = _with_maps(config, host_root, media_root)
    mapped = map_imported_path(config, str(host_root / "mapped.mkv"))
    assert mapped == video.resolve()
    repo = open_repository(config)
    result = ingest_webhook(
        config,
        repo,
        _sonarr_payload(host_root / "mapped.mkv"),
        probe=_FakeProbe(),
    )
    jobs = repo.list_jobs()
    repo.close()
    assert result["action"] == "enqueued"
    assert jobs[0].relative_path == "mapped.mkv"


def test_mapped_path_that_escapes_roots_is_rejected(
    config: AppConfig, media_root: Path, tmp_path: Path
) -> None:
    config = _with_maps(config, tmp_path / "host-library", media_root)
    with pytest.raises(NasSubtitlesError) as raised:
        map_imported_path(config, str(tmp_path / "host-library" / ".." / "etc" / "passwd"))
    assert raised.value.code is ErrorCode.MEDIA_PATH_OUTSIDE_ROOTS


def test_token_is_required_to_start(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WEBHOOK_TOKEN_ENV, raising=False)
    with pytest.raises(NasSubtitlesError) as raised:
        resolve_webhook_bind(config)
    assert raised.value.code is ErrorCode.CONFIG_INVALID


def test_env_token_overrides_config(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WEBHOOK_TOKEN_ENV, "from-env")
    bind = resolve_webhook_bind(_with_token(config, "from-file"))
    assert bind.token == "from-env"


def test_ingest_does_not_take_the_worker_lock(config: AppConfig, media_root: Path) -> None:
    video = media_root / "locked.mkv"
    video.write_bytes(b"lock")
    repo = open_repository(config)
    held = StateDirLock(config.lock_path)
    held.__enter__()
    try:
        result = ingest_webhook(config, repo, _sonarr_payload(video), probe=_FakeProbe())
    finally:
        held.__exit__(None, None, None)
        repo.close()
    assert result["action"] == "enqueued"


def test_http_valid_sonarr_event_enqueues(config: AppConfig, media_root: Path) -> None:
    video = media_root / "http-sonarr.mkv"
    video.write_bytes(b"s" * 32)
    repo = open_repository(config)
    server = create_server(
        _with_token(config), repo, host="127.0.0.1", port=0, probe=_FakeProbe()
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        health = _request(host, port, "GET", "/health")
        assert health.status == 200
        response = _request_json(
            host,
            port,
            "POST",
            "/hooks/sonarr",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            body=_sonarr_payload(video),
        )
        assert response["action"] == "enqueued"
        assert repo.list_jobs()[0].relative_path == "http-sonarr.mkv"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        repo.close()


def test_http_valid_radarr_event_enqueues(config: AppConfig, media_root: Path) -> None:
    video = media_root / "http-radarr.mkv"
    video.write_bytes(b"r" * 32)
    repo = open_repository(config)
    server = create_server(
        _with_token(config), repo, host="127.0.0.1", port=0, probe=_FakeProbe()
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        response = _request_json(
            host,
            port,
            "POST",
            "/hooks/radarr",
            headers={"X-Api-Key": _TOKEN},
            body=_radarr_payload(video),
        )
        assert response["action"] == "enqueued"
        assert response["source"] == "radarr"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        repo.close()


def test_http_rejects_bad_token(config: AppConfig, media_root: Path) -> None:
    video = media_root / "denied.mkv"
    video.write_bytes(b"no")
    repo = open_repository(config)
    server = create_server(
        _with_token(config), repo, host="127.0.0.1", port=0, probe=_FakeProbe()
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        denied = _request(
            host,
            port,
            "POST",
            "/hooks/sonarr",
            headers={"Authorization": "Bearer wrong-token"},
            body=_sonarr_payload(video),
        )
        assert denied.status == 401
        payload = json.loads(denied.body.decode("utf-8"))
        assert payload["error_code"] == "permission_denied"
        assert repo.list_jobs() == ()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        repo.close()


def test_http_rejects_malformed_payload(config: AppConfig) -> None:
    repo = open_repository(config)
    server = create_server(
        _with_token(config), repo, host="127.0.0.1", port=0, probe=_FakeProbe()
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        denied = _request(
            host,
            port,
            "POST",
            "/hooks",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            body={"hello": "world"},
        )
        assert denied.status == 400
        payload = json.loads(denied.body.decode("utf-8"))
        assert payload["error_code"] == "config_invalid"
        assert repo.list_jobs() == ()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        repo.close()


def test_http_rejects_path_outside_roots(config: AppConfig, tmp_path: Path) -> None:
    outsider = tmp_path / "escape.mkv"
    outsider.write_bytes(b"x")
    repo = open_repository(config)
    server = create_server(
        _with_token(config), repo, host="127.0.0.1", port=0, probe=_FakeProbe()
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        denied = _request(
            host,
            port,
            "POST",
            "/hooks/radarr",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            body=_radarr_payload(outsider),
        )
        assert denied.status == 400
        payload = json.loads(denied.body.decode("utf-8"))
        assert payload["error_code"] == "media_path_outside_roots"
        assert repo.list_jobs() == ()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        repo.close()


def test_http_test_event_does_not_enqueue(config: AppConfig) -> None:
    repo = open_repository(config)
    server = create_server(
        _with_token(config), repo, host="127.0.0.1", port=0, probe=_FakeProbe()
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        response = _request_json(
            host,
            port,
            "POST",
            "/hooks",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            body={"eventType": "Test"},
        )
        assert response["action"] == "acknowledged"
        assert response["enqueued"] is False
        assert repo.list_jobs() == ()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        repo.close()


def test_webhooks_help_offers_json() -> None:
    result = runner.invoke(app, ["webhooks", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.output


def test_webhooks_command_refuses_to_start_without_a_token(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(WEBHOOK_TOKEN_ENV, raising=False)
    result = runner.invoke(app, ["webhooks", "--config", str(config_path), "--json"])
    assert result.exit_code != 0
    payload = json.loads(result.stderr)
    assert payload["error_code"] == "config_invalid"


def test_config_without_webhooks_key_still_loads(tmp_path: Path) -> None:
    media = tmp_path / "media"
    for name in ("media", "state", "work", "models", "output"):
        (tmp_path / name).mkdir()
    destination = tmp_path / "legacy.yaml"
    destination.write_text(
        "\n".join(
            [
                f"media_roots: [{media}]",
                f"state_dir: {tmp_path / 'state'}",
                f"work_dir: {tmp_path / 'work'}",
                f"models_dir: {tmp_path / 'models'}",
                f"output_dir: {tmp_path / 'output'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = load_config(destination)
    assert loaded.webhooks.bind == "127.0.0.1"
    assert loaded.webhooks.port == 8788
    assert loaded.webhooks.token is None


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, object] | bytes | None = None,
) -> object:
    connection = HTTPConnection(host, port, timeout=5)
    try:
        payload: bytes | None
        outgoing = dict(headers or {})
        if isinstance(body, dict):
            payload = json.dumps(body).encode("utf-8")
            outgoing.setdefault("Content-Type", "application/json")
        else:
            payload = body
        if payload is not None:
            outgoing.setdefault("Content-Length", str(len(payload)))
        connection.request(method, path, body=payload, headers=outgoing)
        response = connection.getresponse()
        return type("Response", (), {"status": response.status, "body": response.read()})()
    finally:
        connection.close()


def _request_json(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    response = _request(host, port, method, path, headers=headers, body=body)
    assert response.status == 200, response.body
    payload = json.loads(response.body.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload
