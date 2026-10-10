"""Optional LAN dashboard. Owned by roadmap item 003.

A separate process from the worker: it never takes ``worker.lock`` and never
runs inference. Stopping this process leaves the daemon scanning and
processing. Authentication is a single shared token so a later remote-auth
layer can sit in front without rewriting routes.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .api import DashboardService, JobView, OperatorRepository
from .config import AppConfig
from .domain import (
    ErrorCode,
    JobKind,
    JobState,
    NasSubtitlesError,
    exit_code_for,
)
from .logging_setup import log_event

__all__ = [
    "DASHBOARD_TOKEN_ENV",
    "STATIC_DIR",
    "DashboardBind",
    "create_server",
    "make_handler",
    "resolve_dashboard_bind",
    "serve_dashboard",
]

_LOG = logging.getLogger(__name__)

DASHBOARD_TOKEN_ENV = "NAS_SUBS_DASHBOARD_TOKEN"
STATIC_DIR = Path(__file__).resolve().parent / "static"

_PUBLIC_NETWORK_BINDS = frozenset({"0.0.0.0", "::", "[::]"})


@dataclass(frozen=True, slots=True)
class DashboardBind:
    """Resolved listen address and optional shared secret."""

    host: str
    port: int
    token: str | None

    @property
    def requires_token(self) -> bool:
        return bool(self.token)

    @property
    def public_bind(self) -> bool:
        return self.host in _PUBLIC_NETWORK_BINDS


def resolve_dashboard_bind(
    config: AppConfig,
    *,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
) -> DashboardBind:
    """CLI flags override config; ``NAS_SUBS_DASHBOARD_TOKEN`` overrides both."""
    env_token = os.environ.get(DASHBOARD_TOKEN_ENV)
    resolved_token = _first_nonempty(env_token, token, config.dashboard.token)
    return DashboardBind(
        host=host or config.dashboard.bind,
        port=port if port is not None else config.dashboard.port,
        token=resolved_token,
    )


def create_server(
    config: AppConfig,
    repository: OperatorRepository,
    *,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
) -> ThreadingHTTPServer:
    """Build a dashboard server. Does not acquire the worker lock."""
    bind = resolve_dashboard_bind(config, host=host, port=port, token=token)
    service = DashboardService(config, repository)
    return ThreadingHTTPServer((bind.host, bind.port), make_handler(service, bind))


def serve_dashboard(
    config: AppConfig,
    repository: OperatorRepository,
    *,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
    ready: Callable[[ThreadingHTTPServer], None] | None = None,
) -> ThreadingHTTPServer:
    """Start the dashboard and block until the server is shut down."""
    bind = resolve_dashboard_bind(config, host=host, port=port, token=token)
    server = create_server(config, repository, host=host, port=port, token=token)
    log_event(
        _LOG,
        "dashboard listening",
        bind_host=bind.host,
        bind_port=server.server_address[1],
        auth_required=bind.requires_token,
        public_bind=bind.public_bind,
    )
    if ready is not None:
        ready(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return server


def make_handler(service: DashboardService, bind: DashboardBind) -> type[BaseHTTPRequestHandler]:
    """Build a request handler closed over the service and bind settings."""

    class DashboardHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_OPTIONS(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self._send(
                    HTTPStatus.NO_CONTENT,
                    b"",
                    content_type="text/plain",
                    extra_headers=_cors_headers(),
                )
                return
            self._send_error(HTTPStatus.NOT_FOUND, ErrorCode.JOB_NOT_FOUND, "not found")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if method == "GET" and not path.startswith("/api/"):
                self._serve_static(path)
                return
            if not self._authorised():
                self._send_error(
                    HTTPStatus.UNAUTHORIZED,
                    ErrorCode.PERMISSION_DENIED,
                    "dashboard token required",
                )
                return
            try:
                payload = self._route(method, path, parse_qs(parsed.query))
            except NasSubtitlesError as exc:
                self._send_error(_status_for(exc.code), exc.code, exc.message)
                return
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.CONFIG_INVALID, str(exc))
                return
            self._send_json(HTTPStatus.OK, payload)

        def _route(self, method: str, path: str, query: dict[str, list[str]]) -> dict[str, object]:
            if method == "GET" and path == "/api/health":
                return service.overview()
            if method == "GET" and path == "/api/overview":
                return service.dashboard_overview()
            if method == "GET" and path == "/api/jobs":
                return service.list_jobs(
                    view=_view_param(query),
                    states=_states_param(query),
                    search=_search_param(query),
                    job_kind=_kind_param(query),
                    target_language=_target_language_param(query),
                    sort=_sort_param(query),
                    limit=_limit_param(query),
                    offset=_offset_param(query),
                )
            if method == "GET" and path.startswith("/api/jobs/"):
                return service.get_job(_job_id_from_path(path, suffix=""))
            if method == "POST" and path.endswith("/retry"):
                return service.retry_job(_job_id_from_path(path, suffix="/retry"))
            if method == "POST" and path.endswith("/cancel"):
                return service.cancel_job(_job_id_from_path(path, suffix="/cancel"))
            if method == "POST" and path.endswith("/reprocess"):
                return service.reprocess_job(_job_id_from_path(path, suffix="/reprocess"))
            if method == "POST" and path == "/api/scan":
                return service.rescan()
            if method == "GET" and path == "/api/settings":
                return service.settings()
            raise NasSubtitlesError("not found", code=ErrorCode.JOB_NOT_FOUND)

        def _authorised(self) -> bool:
            if not bind.requires_token:
                return True
            header = self.headers.get("Authorization", "")
            expected = f"Bearer {bind.token}"
            return header == expected

        def _serve_static(self, path: str) -> None:
            relative = "index.html" if path in {"", "/"} else path.lstrip("/")
            if relative.startswith("static/"):
                relative = relative[len("static/") :]
            candidate = (STATIC_DIR / relative).resolve()
            try:
                candidate.relative_to(STATIC_DIR.resolve())
            except ValueError:
                self._send_error(HTTPStatus.NOT_FOUND, ErrorCode.JOB_NOT_FOUND, "not found")
                return
            if not candidate.is_file():
                # SPA fallback so /jobs/<id> reloads still serve the shell.
                candidate = STATIC_DIR / "index.html"
            if not candidate.is_file():
                self._send_error(
                    HTTPStatus.NOT_FOUND,
                    ErrorCode.JOB_NOT_FOUND,
                    "dashboard assets are missing",
                )
                return
            content_type = _content_type(candidate)
            body = candidate.read_bytes()
            extra = {"Cache-Control": "no-store"} if candidate.name == "index.html" else {}
            self._send(HTTPStatus.OK, body, content_type=content_type, extra_headers=extra)

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._send(
                status,
                body,
                content_type="application/json; charset=utf-8",
                extra_headers=_cors_headers(),
            )

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

        def _send(
            self,
            status: HTTPStatus,
            body: bytes,
            *,
            content_type: str,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(int(status))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    return DashboardHandler


def _view_param(query: dict[str, list[str]]) -> JobView:
    raw = (query.get("view") or ["all"])[0]
    if raw not in {"queue", "history", "all"}:
        raise ValueError("view must be queue, history or all")
    return raw  # type: ignore[return-value]


def _states_param(query: dict[str, list[str]]) -> tuple[JobState, ...] | None:
    """Dashboard v2 §8.4: ``state`` still takes a single value; a comma-separated
    list (``state=failed,needs_review``) is additive."""
    raw = (query.get("state") or [""])[0]
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        return None
    try:
        return tuple(JobState(value) for value in values)
    except ValueError as exc:
        raise ValueError(f"unknown job state in {raw!r}") from exc


def _search_param(query: dict[str, list[str]]) -> str | None:
    """Dashboard v2 §8.2: filename search, forwarded as-is — the SQL layer
    (``repository._job_filters``) does the escaping and parameter binding."""
    raw = (query.get("search") or [""])[0].strip()
    return raw or None


def _kind_param(query: dict[str, list[str]]) -> JobKind | None:
    """Dashboard v2 §8.3."""
    raw = (query.get("kind") or [""])[0].strip()
    if not raw:
        return None
    try:
        return JobKind(raw)
    except ValueError as exc:
        raise ValueError(f"unknown job kind {raw!r}") from exc


def _target_language_param(query: dict[str, list[str]]) -> str | None:
    """No fixed enum here: target languages come from config, not domain."""
    raw = (query.get("target_language") or [""])[0].strip()
    return raw or None


def _sort_param(query: dict[str, list[str]]) -> str:
    """Dashboard v2 §8.5: the whitelist itself lives in ``repository.py``
    (``_JOB_SORT_COLUMNS``), which raises ``ValueError`` -> HTTP 400 on an
    unknown key instead of silently falling back."""
    return (query.get("sort") or ["updated_desc"])[0]


def _offset_param(query: dict[str, list[str]]) -> int:
    raw = (query.get("offset") or ["0"])[0]
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("offset must be an integer") from exc
    if value < 0:
        raise ValueError("offset must be >= 0")
    return value


def _limit_param(query: dict[str, list[str]]) -> int:
    raw = (query.get("limit") or ["100"])[0]
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if value < 1 or value > 10_000:
        raise ValueError("limit must be between 1 and 10000")
    return value


def _job_id_from_path(path: str, *, suffix: str) -> str:
    prefix = "/api/jobs/"
    if not path.startswith(prefix):
        raise NasSubtitlesError("not found", code=ErrorCode.JOB_NOT_FOUND)
    rest = path[len(prefix) :]
    if suffix:
        if not rest.endswith(suffix):
            raise NasSubtitlesError("not found", code=ErrorCode.JOB_NOT_FOUND)
        rest = rest[: -len(suffix)]
    if not rest or "/" in rest:
        raise NasSubtitlesError("not found", code=ErrorCode.JOB_NOT_FOUND)
    return rest


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".html":
        return "text/html; charset=utf-8"
    if suffix == ".css":
        return "text/css; charset=utf-8"
    if suffix == ".js":
        return "text/javascript; charset=utf-8"
    if suffix == ".svg":
        return "image/svg+xml"
    return "application/octet-stream"


def _cors_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    }


def _first_nonempty(*values: str | None) -> str | None:
    for value in values:
        if value is not None and value.strip():
            return value.strip()
    return None


def _status_for(code: ErrorCode) -> HTTPStatus:
    if code is ErrorCode.JOB_NOT_FOUND:
        return HTTPStatus.NOT_FOUND
    if code is ErrorCode.PERMISSION_DENIED:
        return HTTPStatus.UNAUTHORIZED
    if code is ErrorCode.INVALID_STATE_TRANSITION:
        return HTTPStatus.CONFLICT
    if code is ErrorCode.LOCK_BUSY:
        return HTTPStatus.CONFLICT
    return HTTPStatus.BAD_REQUEST
