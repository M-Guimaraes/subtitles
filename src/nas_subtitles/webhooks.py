"""Optional Sonarr/Radarr webhook listener. Owned by roadmap item 004.

A separate process from the worker and the dashboard: it never takes
``worker.lock`` and never runs inference. Stopping this process leaves the
daemon scanning and processing. Requests must carry a shared secret;
unauthenticated webhooks are refused. Valid import events reuse
``discovery.enqueue_path`` so a webhook and a later scan stay idempotent.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

from .config import AppConfig, WebhookPathMap
from .discovery import enqueue_path, is_candidate_name
from .domain import (
    ErrorCode,
    JobRecord,
    JobRepository,
    NasSubtitlesError,
    exit_code_for,
)
from .logging_setup import log_event, path_token
from .media import FfprobeMediaProbe

__all__ = [
    "MAX_WEBHOOK_BODY_BYTES",
    "SUPPORTED_IMPORT_EVENTS",
    "WEBHOOK_TOKEN_ENV",
    "WebhookBind",
    "WebhookEvent",
    "create_server",
    "extract_imported_paths",
    "ingest_webhook",
    "make_handler",
    "map_imported_path",
    "parse_arr_webhook",
    "resolve_webhook_bind",
    "serve_webhooks",
    "webhook_tokens_match",
]

_LOG = logging.getLogger(__name__)

WEBHOOK_TOKEN_ENV = "NAS_SUBS_WEBHOOK_TOKEN"
MAX_WEBHOOK_BODY_BYTES = 1_048_576

_PUBLIC_NETWORK_BINDS = frozenset({"0.0.0.0", "::", "[::]"})
_TOKEN_HEADERS = ("X-Api-Key", "X-Webhook-Token")

SUPPORTED_IMPORT_EVENTS = frozenset(
    {
        "download",
        "downloadcomplete",
        "import",
        "importcomplete",
        "onimportcomplete",
    }
)
_TEST_EVENTS = frozenset({"test"})

WebhookAction = Literal[
    "enqueued",
    "already_queued",
    "skipped",
    "ignored",
    "acknowledged",
]


@dataclass(frozen=True, slots=True)
class WebhookBind:
    """Resolved listen address. A shared secret is always required."""

    host: str
    port: int
    token: str

    @property
    def public_bind(self) -> bool:
        return self.host in _PUBLIC_NETWORK_BINDS


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """A parsed Sonarr or Radarr notification, before path mapping."""

    event_type: str
    source: Literal["sonarr", "radarr", "unknown"]
    kind: Literal["import", "test", "ignored"]
    raw_paths: tuple[str, ...]


def resolve_webhook_bind(
    config: AppConfig,
    *,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
) -> WebhookBind:
    """CLI flags override config; ``NAS_SUBS_WEBHOOK_TOKEN`` overrides both.

    A token is mandatory. Missing or blank values fail closed so the listener
    never accepts unauthenticated import events.
    """
    env_token = os.environ.get(WEBHOOK_TOKEN_ENV)
    resolved = _first_nonempty(env_token, token, config.webhooks.token)
    if resolved is None:
        raise NasSubtitlesError(
            "webhook token is required; set NAS_SUBS_WEBHOOK_TOKEN or webhooks.token",
            code=ErrorCode.CONFIG_INVALID,
        )
    return WebhookBind(
        host=host or config.webhooks.bind,
        port=port if port is not None else config.webhooks.port,
        token=resolved,
    )


def webhook_tokens_match(provided: str | None, expected: str) -> bool:
    """Constant-time compare. Empty or missing values never match."""
    if provided is None or not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def create_server(
    config: AppConfig,
    repository: JobRepository,
    *,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
    probe: FfprobeMediaProbe | None = None,
) -> ThreadingHTTPServer:
    """Build a webhook server. Does not acquire the worker lock."""
    bind = resolve_webhook_bind(config, host=host, port=port, token=token)
    return ThreadingHTTPServer((bind.host, bind.port), make_handler(config, repository, bind, probe))


def serve_webhooks(
    config: AppConfig,
    repository: JobRepository,
    *,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
    ready: Callable[[ThreadingHTTPServer], None] | None = None,
) -> ThreadingHTTPServer:
    """Start the webhook listener and block until the server is shut down."""
    bind = resolve_webhook_bind(config, host=host, port=port, token=token)
    server = create_server(config, repository, host=host, port=port, token=token)
    log_event(
        _LOG,
        "webhooks listening",
        bind_host=bind.host,
        bind_port=server.server_address[1],
        auth_required=True,
        public_bind=bind.public_bind,
    )
    if ready is not None:
        ready(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return server


def make_handler(
    config: AppConfig,
    repository: JobRepository,
    bind: WebhookBind,
    probe: FfprobeMediaProbe | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler closed over config, repository and bind."""

    class WebhookHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "service": "webhooks",
                        "auth_required": True,
                        "worker_independent": True,
                    },
                )
                return
            self._send_error(HTTPStatus.NOT_FOUND, ErrorCode.JOB_NOT_FOUND, "not found")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path not in {"/hooks", "/hooks/sonarr", "/hooks/radarr"}:
                self._send_error(HTTPStatus.NOT_FOUND, ErrorCode.JOB_NOT_FOUND, "not found")
                return
            if not self._authorised(parse_qs(parsed.query)):
                self._send_error(
                    HTTPStatus.UNAUTHORIZED,
                    ErrorCode.PERMISSION_DENIED,
                    "webhook token required",
                )
                return
            try:
                payload = self._read_json_object()
                result = ingest_webhook(config, repository, payload, probe=probe)
            except NasSubtitlesError as exc:
                self._send_error(_status_for(exc.code), exc.code, exc.message)
                return
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.CONFIG_INVALID, str(exc))
                return
            self._send_json(HTTPStatus.OK, result)

        def _authorised(self, query: dict[str, list[str]]) -> bool:
            return webhook_tokens_match(_provided_token(self.headers, query), bind.token)

        def _read_json_object(self) -> dict[str, object]:
            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length or "0")
            except ValueError as exc:
                raise NasSubtitlesError(
                    "webhook Content-Length is not an integer",
                    code=ErrorCode.CONFIG_INVALID,
                ) from exc
            if length < 1:
                raise NasSubtitlesError(
                    "webhook body is missing",
                    code=ErrorCode.CONFIG_INVALID,
                )
            if length > MAX_WEBHOOK_BODY_BYTES:
                raise NasSubtitlesError(
                    "webhook body exceeds the 1 MiB limit",
                    code=ErrorCode.CONFIG_INVALID,
                )
            body = self.rfile.read(length)
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise NasSubtitlesError(
                    "webhook body is not valid JSON",
                    code=ErrorCode.CONFIG_INVALID,
                ) from exc
            if not isinstance(parsed, dict):
                raise NasSubtitlesError(
                    "webhook body must be a JSON object",
                    code=ErrorCode.CONFIG_INVALID,
                )
            return parsed

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_error(self, status: HTTPStatus, code: ErrorCode, message: str) -> None:
            self._send_json(
                status,
                {
                    "ok": False,
                    "error_code": str(code),
                    "message": message,
                    "exit_code": int(exit_code_for(code)),
                },
            )

    return WebhookHandler


def parse_arr_webhook(payload: Mapping[str, object]) -> WebhookEvent:
    """Interpret a Sonarr or Radarr notification. Malformed input fails closed."""
    raw_event = payload.get("eventType")
    if not isinstance(raw_event, str) or not raw_event.strip():
        raise NasSubtitlesError(
            "webhook eventType is missing",
            code=ErrorCode.CONFIG_INVALID,
        )
    event_type = raw_event.strip()
    folded = _fold_event_type(event_type)
    source = _source_of(payload)
    if folded in _TEST_EVENTS:
        return WebhookEvent(event_type=event_type, source=source, kind="test", raw_paths=())
    if folded not in SUPPORTED_IMPORT_EVENTS:
        return WebhookEvent(event_type=event_type, source=source, kind="ignored", raw_paths=())
    paths = extract_imported_paths(payload)
    if not paths:
        raise NasSubtitlesError(
            "webhook import event has no media path",
            code=ErrorCode.CONFIG_INVALID,
        )
    return WebhookEvent(
        event_type=event_type,
        source=source,
        kind="import",
        raw_paths=paths,
    )


def extract_imported_paths(payload: Mapping[str, object]) -> tuple[str, ...]:
    """Collect final imported file paths from Sonarr/Radarr payload shapes."""
    found: list[str] = []
    for key in ("episodeFile", "movieFile"):
        _append_file_path(found, payload.get(key))
    for key in ("episodeFiles", "movieFiles"):
        files = payload.get(key)
        if isinstance(files, list):
            for item in files:
                _append_file_path(found, item)
    unique: list[str] = []
    seen: set[str] = set()
    for path in found:
        if path not in seen:
            unique.append(path)
            seen.add(path)
    return tuple(unique)


def map_imported_path(config: AppConfig, raw_path: str) -> Path:
    """Map a payload path onto configured roots. Traversal and escapes fail closed."""
    text = raw_path.strip().replace("\\", "/")
    if not text:
        raise NasSubtitlesError(
            "webhook media path is empty",
            code=ErrorCode.MEDIA_PATH_OUTSIDE_ROOTS,
            detail={"path_token": path_token(raw_path)},
        )
    mapped = _apply_path_maps(text, config.webhooks.path_maps)
    candidate = Path(mapped)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise NasSubtitlesError(
            "webhook media path is outside the configured media roots",
            code=ErrorCode.MEDIA_PATH_OUTSIDE_ROOTS,
            detail={"path_token": path_token(raw_path)},
        )
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise NasSubtitlesError(
            "webhook media path could not be resolved",
            code=ErrorCode.INVALID_MEDIA,
            detail={"path_token": path_token(candidate)},
        ) from exc
    if ".." in resolved.parts:
        raise NasSubtitlesError(
            "webhook media path is outside the configured media roots",
            code=ErrorCode.MEDIA_PATH_OUTSIDE_ROOTS,
            detail={"path_token": path_token(resolved)},
        )
    root = config.root_for(resolved)
    if root is None:
        raise NasSubtitlesError(
            "webhook media path is outside the configured media roots",
            code=ErrorCode.MEDIA_PATH_OUTSIDE_ROOTS,
            detail={"path_token": path_token(resolved)},
        )
    return resolved


def ingest_webhook(
    config: AppConfig,
    repository: JobRepository,
    payload: Mapping[str, object],
    *,
    probe: FfprobeMediaProbe | None = None,
) -> dict[str, object]:
    """Parse one notification and enqueue through the same path as a scan."""
    event = parse_arr_webhook(payload)
    if event.kind == "test":
        log_event(_LOG, "webhook test acknowledged", source=event.source)
        return _ingest_payload(
            action="acknowledged",
            event=event,
            results=(),
        )
    if event.kind == "ignored":
        log_event(
            _LOG,
            "webhook event ignored",
            source=event.source,
            event_type=event.event_type,
        )
        return _ingest_payload(action="ignored", event=event, results=())

    known_ids = {job.id for job in repository.list_jobs(limit=10_000)}
    results: list[dict[str, object]] = []
    for raw in event.raw_paths:
        results.append(
            _enqueue_imported_path(
                config,
                repository,
                raw,
                known_ids=known_ids,
                probe=probe,
            )
        )
    actions = {str(item["action"]) for item in results}
    action: WebhookAction
    if "enqueued" in actions:
        action = "enqueued"
    elif "already_queued" in actions:
        action = "already_queued"
    else:
        action = "skipped"
    return _ingest_payload(action=action, event=event, results=tuple(results))


def _enqueue_imported_path(
    config: AppConfig,
    repository: JobRepository,
    raw_path: str,
    *,
    known_ids: set[str],
    probe: FfprobeMediaProbe | None,
) -> dict[str, object]:
    resolved = map_imported_path(config, raw_path)
    if not is_candidate_name(resolved):
        raise NasSubtitlesError(
            "webhook media is not a supported video",
            code=ErrorCode.INVALID_MEDIA,
            detail={"path_token": path_token(resolved)},
        )
    job, skip_reason = enqueue_path(
        config,
        repository,
        resolved,
        require_stability=False,
        probe=probe,
    )
    if skip_reason is not None:
        return {
            "action": "skipped",
            "reason": skip_reason,
            "job_id": None,
            "root_id": None,
            "path_token": path_token(resolved),
        }
    assert job is not None
    already = job.id in known_ids
    known_ids.add(job.id)
    action: WebhookAction = "already_queued" if already else "enqueued"
    log_event(
        _LOG,
        "webhook import accepted",
        job_id=job.id,
        root_id=job.root_id,
        path_token=path_token(resolved),
        already_queued=already,
    )
    return {
        "action": action,
        "reason": None,
        "job_id": job.id,
        "root_id": job.root_id,
        "path_token": path_token(resolved),
        "state": str(job.state),
        "job": _job_summary(job),
    }


def _ingest_payload(
    *,
    action: WebhookAction,
    event: WebhookEvent,
    results: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    first = results[0] if results else {}
    return {
        "ok": True,
        "action": action,
        "enqueued": action == "enqueued",
        "event_type": event.event_type,
        "source": event.source,
        "job_id": first.get("job_id"),
        "root_id": first.get("root_id"),
        "reason": first.get("reason"),
        "results": [dict(item) for item in results],
    }


def _job_summary(job: JobRecord) -> dict[str, object]:
    return {
        "id": job.id,
        "state": str(job.state),
        "root_id": job.root_id,
        "relative_path": job.relative_path,
    }


def _apply_path_maps(path: str, maps: tuple[WebhookPathMap, ...]) -> str:
    ordered = sorted(maps, key=lambda item: len(str(item.host_prefix)), reverse=True)
    for mapping in ordered:
        prefix = str(mapping.host_prefix).replace("\\", "/").rstrip("/")
        if path == prefix or path.startswith(f"{prefix}/"):
            destination = str(mapping.container_prefix).replace("\\", "/").rstrip("/")
            return destination + path[len(prefix) :]
    return path


def _append_file_path(found: list[str], item: object) -> None:
    if not isinstance(item, dict):
        return
    raw = item.get("path")
    if isinstance(raw, str) and raw.strip():
        found.append(raw.strip())


def _source_of(payload: Mapping[str, object]) -> Literal["sonarr", "radarr", "unknown"]:
    if isinstance(payload.get("series"), dict):
        return "sonarr"
    if isinstance(payload.get("movie"), dict):
        return "radarr"
    return "unknown"


def _fold_event_type(event_type: str) -> str:
    return event_type.strip().lower().replace("_", "").replace("-", "")


def _provided_token(headers: Mapping[str, str], query: dict[str, list[str]]) -> str | None:
    authorization = headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :].strip() or None
    for name in _TOKEN_HEADERS:
        value = headers.get(name)
        if value is not None and value.strip():
            return value.strip()
    query_values = query.get("token") or []
    if query_values and query_values[0].strip():
        return query_values[0].strip()
    return None


def _first_nonempty(*values: str | None) -> str | None:
    for value in values:
        if value is not None and value.strip():
            return value.strip()
    return None


def _status_for(code: ErrorCode) -> HTTPStatus:
    if code is ErrorCode.PERMISSION_DENIED:
        return HTTPStatus.UNAUTHORIZED
    if code is ErrorCode.JOB_NOT_FOUND:
        return HTTPStatus.NOT_FOUND
    return HTTPStatus.BAD_REQUEST
