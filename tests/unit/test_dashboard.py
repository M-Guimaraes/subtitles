"""Web dashboard: service layer, HTTP API, and independence from the worker."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.client import HTTPConnection

import pytest
from typer.testing import CliRunner

from nas_subtitles.api import DashboardService
from nas_subtitles.cli import app
from nas_subtitles.config import AppConfig
from nas_subtitles.dashboard import DASHBOARD_TOKEN_ENV, create_server
from nas_subtitles.domain import (
    ErrorCode,
    EventLevel,
    JobEvent,
    JobState,
    MediaFingerprint,
    NasSubtitlesError,
    PipelineStage,
)
from nas_subtitles.repository import StateDirLock, open_repository

runner = CliRunner()


def _fingerprint(relative_path: str = "show/episode.mkv") -> MediaFingerprint:
    return MediaFingerprint(
        root_id="library-aaaa1111",
        relative_path=relative_path,
        size_bytes=1024,
        mtime_ns=1_700_000_000_000_000_000,
        head_sha256="a" * 64,
        tail_sha256="b" * 64,
        audio_stream_index=1,
    )


def _service(config: AppConfig) -> tuple[DashboardService, object]:
    repo = open_repository(config)
    return DashboardService(config, repo), repo


def _write_manifest(config: AppConfig, job_id: str) -> None:
    config.manifests_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job_id,
        "source_language": "en",
        "target_language": "pt-BR",
        "selected_audio_stream_index": 1,
        "stream_language": "en",
        "detected_language": "en",
        "detection_probability": 0.93,
        "source_language_source": "detection",
        "source_language_confident": True,
        "source_language_reason": "asr samples agreed",
        "translation_executed": True,
        "models": [{"kind": "asr", "name": "small", "version": None, "identity": "abc"}],
    }
    (config.manifests_dir / f"{job_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_queue_and_history_views_separate_states(config: AppConfig) -> None:
    service, repo = _service(config)
    failed = repo.enqueue(fingerprint=_fingerprint("b.mkv"), pipeline_config_hash="h")
    claimed = repo.claim_next_job(owner="w", lease_seconds=60)
    assert claimed is not None
    repo.transition(job_id=failed.id, state=JobState.FAILED, error_code=ErrorCode.IO_ERROR)
    queued = repo.enqueue(fingerprint=_fingerprint("a.mkv"), pipeline_config_hash="h")
    queue = service.list_jobs(view="queue")
    history = service.list_jobs(view="history")
    repo.close()
    queue_ids = {job["id"] for job in queue["jobs"]}
    history_ids = {job["id"] for job in history["jobs"]}
    assert queued.id in queue_ids
    assert failed.id not in queue_ids
    assert failed.id in history_ids
    assert queued.id not in history_ids


def test_job_detail_exposes_language_and_retry(config: AppConfig) -> None:
    service, repo = _service(config)
    job = repo.enqueue(fingerprint=_fingerprint(), pipeline_config_hash="h")
    repo.claim_next_job(owner="w", lease_seconds=60)
    repo.transition(
        job_id=job.id,
        state=JobState.FAILED,
        stage=PipelineStage.TRANSLATE,
        error_code=ErrorCode.IO_ERROR,
        error_detail="disk full",
    )
    _write_manifest(config, job.id)
    detail = service.get_job(job.id)
    retried = service.retry_job(job.id)
    repo.close()
    assert detail["job"]["error_code"] == "io_error"
    assert detail["job"]["error_detail"] == "disk full"
    assert detail["language"]["detected_language"] == "en"
    assert detail["language"]["detection_probability"] == pytest.approx(0.93)
    assert detail["language"]["target_language"] == "pt-BR"
    assert "retry" in detail["actions"]
    assert retried["job"]["state"] == "queued"


def test_retry_rejects_non_failed_jobs(config: AppConfig) -> None:
    service, repo = _service(config)
    job = repo.enqueue(fingerprint=_fingerprint(), pipeline_config_hash="h")
    with pytest.raises(NasSubtitlesError) as raised:
        service.retry_job(job.id)
    repo.close()
    assert raised.value.code is ErrorCode.INVALID_STATE_TRANSITION


def test_cancel_and_reprocess_use_the_state_machine(config: AppConfig) -> None:
    service, repo = _service(config)
    job = repo.enqueue(fingerprint=_fingerprint(), pipeline_config_hash="h")
    cancelled = service.cancel_job(job.id)
    reprocessed = service.reprocess_job(job.id)
    repo.close()
    assert cancelled["job"]["state"] == "cancelled"
    assert reprocessed["job"]["state"] == "queued"
    assert reprocessed["action"] == "reprocess"


def test_settings_are_read_only_and_include_language_config(config: AppConfig) -> None:
    service, repo = _service(config)
    payload = service.settings()
    repo.close()
    assert payload["writable"] is False
    assert payload["languages"]["source"] == "auto"
    assert payload["languages"]["target"] == "pt-BR"
    assert payload["languages"]["targets"] == ["pt-BR"]
    assert payload["existing_subtitle_policy"] == "skip"
    assert payload["asr_model"] == "small"
    assert payload["dashboard"]["bind"] == "127.0.0.1"
    assert payload["dashboard"]["token_configured"] is False
    assert payload["webhooks"]["bind"] == "127.0.0.1"
    assert payload["webhooks"]["port"] == 8788
    assert payload["webhooks"]["token_configured"] is False


def test_rescan_does_not_take_the_worker_lock(config: AppConfig) -> None:
    service, repo = _service(config)
    held = StateDirLock(config.lock_path)
    held.__enter__()
    try:
        payload = service.rescan()
    finally:
        held.__exit__(None, None, None)
        repo.close()
    assert payload["ok"] is True
    assert payload["scan"]["examined"] == 0


def test_http_lists_jobs_and_retries(config: AppConfig) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint(), pipeline_config_hash="h")
    repo.claim_next_job(owner="w", lease_seconds=60)
    repo.transition(job_id=job.id, state=JobState.FAILED, error_code=ErrorCode.IO_ERROR)
    server = create_server(config, repo, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        page = _request(host, port, "GET", "/")
        assert page.status == 200
        assert b"nas-subs dashboard" in page.body
        listed = _request_json(host, port, "GET", "/api/jobs?view=history")
        assert listed["jobs"][0]["id"] == job.id
        retried = _request_json(host, port, "POST", f"/api/jobs/{job.id}/retry")
        assert retried["job"]["state"] == "queued"
        settings = _request_json(host, port, "GET", "/api/settings")
        assert settings["languages"]["target"] == "pt-BR"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        repo.close()


def test_http_requires_token_when_configured(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_ENV, "secret-token")
    repo = open_repository(config)
    server = create_server(config, repo, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        denied = _request(host, port, "GET", "/api/jobs")
        assert denied.status == 401
        allowed = _request(
            host,
            port,
            "GET",
            "/api/jobs",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert allowed.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        repo.close()


def test_dashboard_help_offers_json() -> None:
    result = runner.invoke(app, ["dashboard", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.output


def test_language_event_fills_in_before_manifest(config: AppConfig) -> None:
    service, repo = _service(config)
    job = repo.enqueue(fingerprint=_fingerprint(), pipeline_config_hash="h")
    repo.append_event(
        JobEvent(
            level=EventLevel.INFO,
            code="language_decision",
            job_id=job.id,
            payload={
                "detected_language": "pt",
                "detection_probability": 0.88,
                "confident": True,
                "source": "detection",
                "target_language": "pt-BR",
                "selected_audio_stream_index": 2,
                "reason": "asr samples agreed",
            },
            created_at=datetime.now(tz=UTC),
        )
    )
    detail = service.get_job(job.id)
    repo.close()
    assert detail["language"]["detected_language"] == "pt"
    assert detail["job"]["selected_audio_stream_index"] == 2


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> object:
    connection = HTTPConnection(host, port, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return type("Response", (), {"status": response.status, "body": body})()
    finally:
        connection.close()


def _request_json(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    response = _request(host, port, method, path, headers=headers)
    assert response.status == 200, response.body
    payload = json.loads(response.body.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload
