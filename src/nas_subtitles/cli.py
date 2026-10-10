"""Typer command line interface.

Every command accepts ``--json`` and none of them prompts: this runs headless
inside a container. Exit codes come from :class:`~nas_subtitles.domain.ExitCode`
and nowhere else.

Only ``health`` is fully implemented at this stage, plus the skeleton of
``doctor``. The remaining commands delegate to the stage modules, which raise
``NotImplementedError`` until the matching stage of the plan lands; the
boundary below turns that into a structured ``not_implemented`` failure
instead of a traceback.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import typer

from . import dashboard, discovery, dubbing, models, output, repository, webhooks, worker
from .config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from .domain import (
    ErrorCode,
    ExitCode,
    JobKind,
    JobRecord,
    JobState,
    NasSubtitlesError,
    PipelineStage,
    PublishMode,
    PublishOutcome,
    exit_code_for,
    infer_job_kind,
)
from .health import check_health
from .logging_setup import configure_logging
from .media import FfprobeMediaProbe, select_audio_stream

__all__ = ["app", "main"]

CheckStatus = Literal["ok", "warn", "fail", "pending"]

_CONFIG_OPTION = typer.Option(
    DEFAULT_CONFIG_PATH,
    "--config",
    "-c",
    help="Path to config.yaml.",
    show_default=str(DEFAULT_CONFIG_PATH),
)
_JSON_OPTION = typer.Option(False, "--json", help="Emit a structured JSON object on stdout.")

app = typer.Typer(
    name="nas-subs",
    no_args_is_help=True,
    add_completion=False,
    help="Generate Portuguese subtitles locally from the audio of your own media.",
)
models_app = typer.Typer(no_args_is_help=True, help="Install and verify the local models.")
jobs_app = typer.Typer(no_args_is_help=True, help="Inspect and steer queued jobs.")
dub_app = typer.Typer(no_args_is_help=True, help="Generate local pt-BR dubbed audio.")
dub_plan_app = typer.Typer(no_args_is_help=True, help="Export or apply a dubbed speech plan.")
app.add_typer(models_app, name="models")
app.add_typer(jobs_app, name="jobs")
app.add_typer(dub_app, name="dub")
dub_app.add_typer(dub_plan_app, name="plan")


# --------------------------------------------------------------------------- #
# Output and error boundary
# --------------------------------------------------------------------------- #


def _emit(payload: Mapping[str, object], *, as_json: bool, text: str) -> None:
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        typer.echo(text)


def _fail(code: ErrorCode, message: str, *, as_json: bool) -> None:
    exit_code = exit_code_for(code)
    if as_json:
        typer.echo(
            json.dumps({"ok": False, "error_code": str(code), "message": message}),
            err=True,
        )
    else:
        typer.echo(f"error [{code}]: {message}", err=True)
    raise typer.Exit(int(exit_code))


@contextmanager
def _handled(*, as_json: bool) -> Iterator[None]:
    """Translate domain failures into the documented exit codes."""
    try:
        yield
    except NasSubtitlesError as exc:
        _fail(exc.code, exc.message, as_json=as_json)
    except NotImplementedError as exc:
        _fail(ErrorCode.NOT_IMPLEMENTED, str(exc), as_json=as_json)


def _load(config_path: Path) -> AppConfig:
    return load_config(config_path)


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _doctor_checks(config: AppConfig) -> list[DoctorCheck]:
    """Environment checks that need neither a model nor an external binary.

    Checks that belong to a later stage report ``pending`` rather than
    guessing a result.
    """
    checks: list[DoctorCheck] = [
        DoctorCheck(
            "python",
            "ok",
            f"{platform.python_version()} on {platform.system()} {platform.machine()}",
        ),
        DoctorCheck("cpu", "ok", f"{os.cpu_count() or 'unknown'} logical cores visible"),
        DoctorCheck("memory", *_memory_detail()),
        DoctorCheck("config", "ok", f"publish_mode={config.publish_mode}"),
    ]

    for root in config.roots:
        if not root.path.is_dir():
            checks.append(
                DoctorCheck(
                    f"media_root:{root.root_id}",
                    "fail",
                    f"{root.path} is not a directory; the worker refuses to scan it",
                )
            )
        elif not os.access(root.path, os.R_OK):
            checks.append(
                DoctorCheck(f"media_root:{root.root_id}", "fail", f"{root.path} is not readable")
            )
        else:
            writable = os.access(root.path, os.W_OK)
            if config.publish_mode is PublishMode.SIDECAR and not writable:
                checks.append(
                    DoctorCheck(
                        f"media_root:{root.root_id}",
                        "fail",
                        "sidecar publishing requires a writable media directory; "
                        "the application still never modifies video files",
                    )
                )
            else:
                checks.append(DoctorCheck(f"media_root:{root.root_id}", "ok", str(root.path)))

    for name, path, writable in (
        ("state_dir", config.state_dir, True),
        ("work_dir", config.work_dir, True),
        ("models_dir", config.models_dir, False),
        ("output_dir", config.output_dir, True),
    ):
        checks.append(_directory_check(name, path, writable=writable))

    checks.append(_free_space_check(config))

    for binary in ("ffmpeg", "ffprobe"):
        location = shutil.which(binary)
        checks.append(
            DoctorCheck(binary, "ok", location)
            if location
            else DoctorCheck(binary, "warn", f"{binary} is not on PATH")
        )

    report = check_health(config)
    checks.append(
        DoctorCheck(
            "queue_database",
            "ok" if report.database_reachable else "warn",
            report.reason,
        )
    )

    bind = dashboard.resolve_dashboard_bind(config)
    if bind.public_bind and not bind.requires_token:
        checks.append(
            DoctorCheck(
                "dashboard",
                "warn",
                "dashboard.bind is a public address without a token; "
                "do not publish this port on the internet",
            )
        )
    elif not dashboard.STATIC_DIR.joinpath("index.html").is_file():
        checks.append(DoctorCheck("dashboard", "fail", "dashboard assets are missing"))
    else:
        detail = f"{bind.host}:{bind.port}"
        if bind.requires_token:
            detail += ", token configured"
        checks.append(DoctorCheck("dashboard", "ok", detail))

    try:
        webhook_bind = webhooks.resolve_webhook_bind(config)
    except NasSubtitlesError:
        checks.append(
            DoctorCheck(
                "webhooks",
                "warn",
                "webhook token is not configured; nas-subs webhooks will refuse to start",
            )
        )
    else:
        if webhook_bind.public_bind:
            checks.append(
                DoctorCheck(
                    "webhooks",
                    "warn",
                    "webhooks.bind is a public address; keep the Compose publish on loopback "
                    "and do not expose this port on the internet",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "webhooks",
                    "ok",
                    f"{webhook_bind.host}:{webhook_bind.port}, token configured",
                )
            )

    try:
        verified = models.verify_models(config, offline=True)
        checks.append(DoctorCheck("models", "ok", f"{len(verified)} models loadable offline"))
    except NasSubtitlesError as exc:
        status: CheckStatus = "fail" if exc.code is ErrorCode.MODEL_MISSING else "warn"
        checks.append(DoctorCheck("models", status, exc.message))

    if output.supports_atomic_publish(config.output_dir):
        checks.append(DoctorCheck("atomic_publish", "ok", "hard links work on output_dir"))
    else:
        checks.append(
            DoctorCheck(
                "atomic_publish",
                "fail",
                "exclusive hard-link publish is not supported on output_dir",
            )
        )

    piper_voice = models.piper_voice_path(config)
    separation_model = models.separation_model_path(config)
    if not config.dubbing.enabled:
        checks.append(DoctorCheck("dubbing", "ok", "disabled (dubbing.enabled: false)"))
    elif piper_voice.exists() and separation_model.exists():
        checks.append(
            DoctorCheck(
                "dubbing",
                "ok",
                f"profile={config.dubbing.profile} voice={config.dubbing.voice} "
                f"separator={separation_model.name}",
            )
        )
    else:
        missing = [
            label
            for label, path in (("voice", piper_voice), ("separator", separation_model))
            if not path.exists()
        ]
        checks.append(
            DoctorCheck(
                "dubbing",
                "pending",
                f"missing: {', '.join(missing)}; run `nas-subs models install`",
            )
        )
    return checks


def _memory_detail() -> tuple[CheckStatus, str]:
    cgroup = Path("/sys/fs/cgroup/memory.max")
    if cgroup.exists():
        raw = cgroup.read_text(encoding="utf-8").strip()
        if raw != "max":
            return "ok", f"{int(raw) / 1024**3:.1f} GiB cgroup limit"
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        return "warn", "total memory could not be determined"
    return "ok", f"{total / 1024**3:.1f} GiB visible to the process"


def _directory_check(name: str, path: Path, *, writable: bool) -> DoctorCheck:
    if not path.is_dir():
        return DoctorCheck(name, "fail", f"{path} does not exist")
    if writable and not os.access(path, os.W_OK):
        return DoctorCheck(name, "fail", f"{path} is not writable")
    return DoctorCheck(name, "ok", str(path))


def _free_space_check(config: AppConfig) -> DoctorCheck:
    if not config.work_dir.is_dir():
        return DoctorCheck("free_space", "fail", f"{config.work_dir} does not exist")
    free_gib = shutil.disk_usage(config.work_dir).free / 1024**3
    required = config.minimum_free_work_gib
    status: CheckStatus = "ok" if free_gib >= required else "fail"
    return DoctorCheck("free_space", status, f"{free_gib:.1f} GiB free, {required} GiB required")


@app.command()
def doctor(
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Check binaries, resources, paths and model availability."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        checks = _doctor_checks(config)
        failed = [check for check in checks if check.status == "fail"]
        payload = {
            "ok": not failed,
            "checks": [check.to_dict() for check in checks],
        }
        text = "\n".join(f"{check.status:>7}  {check.name}: {check.detail}" for check in checks)
        _emit(payload, as_json=json_output, text=text)
        if failed:
            raise typer.Exit(int(ExitCode.PREFLIGHT_FAILED))


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #


@app.command()
def health(
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Report whether the queue database is readable and the worker is alive.

    Loads no model and runs no external binary, so it stays cheap while a
    transcription is using the CPU.
    """
    with _handled(as_json=json_output):
        config = _load(config_path)
        report = check_health(config)
        _emit(
            {"ok": report.healthy, **report.to_dict()},
            as_json=json_output,
            text=("healthy: " if report.healthy else "unhealthy: ") + report.reason,
        )
        if not report.healthy:
            raise typer.Exit(int(ExitCode.PREFLIGHT_FAILED))


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #


@models_app.command("install")
def models_install(
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Download the ASR/translation models and the dubbing models.

    Whisper, the direct en->pt Argos package, the configured Piper voice and
    the Demucs separation baseline (roadmap 006 fase 2). The only command
    allowed to use the network.
    """
    with _handled(as_json=json_output):
        config = _load(config_path)
        installed = models.install_models(config)
        _emit(
            {"ok": True, "models": [m.name for m in installed]},
            as_json=json_output,
            text=f"installed {len(installed)} models",
        )


@models_app.command("verify")
def models_verify(
    config_path: Path = _CONFIG_OPTION,
    offline: bool = typer.Option(
        True, "--offline/--allow-network", help="Fail instead of reaching the network."
    ),
    json_output: bool = _JSON_OPTION,
) -> None:
    """Check that every model in the manifest is present and loadable."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        verified = models.verify_models(config, offline=offline)
        _emit(
            {"ok": True, "models": [m.name for m in verified]},
            as_json=json_output,
            text=f"verified {len(verified)} models",
        )


# --------------------------------------------------------------------------- #
# per-file commands
# --------------------------------------------------------------------------- #


@app.command()
def inspect(
    path: Path = typer.Argument(..., help="Video file inside a configured media root."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Show streams, durations and the audio track that would be selected."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        root, resolved = discovery.resolve_explicit_path(config, path)
        probe_result = FfprobeMediaProbe().probe(resolved)
        selected = select_audio_stream(probe_result, config=config)
        existing = discovery.find_existing_subtitles(path=resolved, probe_result=probe_result)
        payload = {
            "ok": True,
            "root_id": root.root_id,
            "relative_path": root.relative_path_for(resolved),
            "duration_seconds": probe_result.duration_seconds,
            "selected_audio_stream_index": selected.index,
            "selected_audio_language": selected.language,
            "target_language": config.target_language,
            "target_languages": list(config.target_languages),
            "audio_streams": [
                {
                    "index": stream.index,
                    "language": stream.language,
                    "raw_language_tag": stream.raw_language_tag,
                    "title": stream.title,
                    "codec": stream.codec_name,
                    "channels": stream.channels,
                    "is_default": stream.is_default,
                    "is_commentary": stream.is_commentary,
                    "start_time_seconds": stream.start_time_seconds,
                }
                for stream in probe_result.audio_streams
            ],
            "has_portuguese_subtitle": discovery.has_portuguese_subtitle(existing),
        }
        _emit(
            payload,
            as_json=json_output,
            text=(
                f"{root.relative_path_for(resolved)}: {probe_result.duration_seconds:.1f}s "
                f"audio={selected.index} lang={selected.language or 'unknown'}"
            ),
        )


@app.command()
def process(
    path: Path = typer.Argument(..., help="Video file inside a configured media root."),
    preview_seconds: int | None = typer.Option(
        None, "--preview-seconds", help="Transcribe only this many seconds, into staging."
    ),
    preview_offset_seconds: int = typer.Option(
        0, "--preview-offset-seconds", help="Where the preview window starts."
    ),
    source_language: str | None = typer.Option(
        None, "--source-language", help="Skip detection and force the source language."
    ),
    audio_stream_index: int | None = typer.Option(
        None,
        "--audio-stream-index",
        help="Global ffprobe stream index, not the relative a:N ordinal.",
    ),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Run one file ahead of the queue, under the same lock and rules.

    A preview is written to staging with a `.preview` marker and never
    satisfies the library.
    """
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        with repository.StateDirLock(config.lock_path):
            queued = discovery.enqueue_targets(
                config,
                repo,
                path,
                source_language=source_language,
                audio_stream_index=audio_stream_index,
                preview_seconds=None if preview_seconds is None else float(preview_seconds),
                preview_offset_seconds=float(preview_offset_seconds),
                require_stability=preview_seconds is None,
            )
            if not queued.jobs:
                reason = queued.skip_reasons[0] if queued.skip_reasons else "nothing to enqueue"
                _emit(
                    {
                        "ok": True,
                        "skipped": True,
                        "reason": reason,
                        "skipped_targets": [
                            {
                                "target_language": item.target_language,
                                "reason": item.skip_reason,
                            }
                            for item in queued.outcomes
                            if item.skip_reason is not None
                        ],
                    },
                    as_json=json_output,
                    text=reason,
                )
                return
            from .pipeline import build_context, run_job

            results = [run_job(build_context(config, repo, job)) for job in queued.jobs]
            first = results[0]
            _emit(
                {
                    "ok": True,
                    "job_id": first.job_id,
                    "state": str(first.state),
                    "cues": first.cue_count,
                    "output": str(first.output_path) if first.output_path else None,
                    "jobs": [
                        {
                            "job_id": item.job_id,
                            "state": str(item.state),
                            "cues": item.cue_count,
                            "output": str(item.output_path) if item.output_path else None,
                            "target_language": job.target_language,
                        }
                        for item, job in zip(results, queued.jobs, strict=True)
                    ],
                    "skipped_targets": [
                        {
                            "target_language": item.target_language,
                            "reason": item.skip_reason,
                        }
                        for item in queued.outcomes
                        if item.skip_reason is not None
                    ],
                },
                as_json=json_output,
                text=f"{first.job_id} {first.state} cues={first.cue_count}",
            )


@app.command()
def enqueue(
    path: Path = typer.Argument(..., help="Video file inside a configured media root."),
    source_language: str | None = typer.Option(None, "--source-language"),
    audio_stream_index: int | None = typer.Option(None, "--audio-stream-index"),
    priority: int = typer.Option(0, "--priority", help="Higher runs first."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Add one file to the queue after the stability checks."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        queued = discovery.enqueue_targets(
            config,
            repo,
            path,
            source_language=source_language,
            audio_stream_index=audio_stream_index,
            priority=priority,
        )
        if not queued.jobs:
            reason = queued.skip_reasons[0] if queued.skip_reasons else "nothing to enqueue"
            _emit(
                {
                    "ok": True,
                    "enqueued": False,
                    "reason": reason,
                    "skipped_targets": [
                        {
                            "target_language": item.target_language,
                            "reason": item.skip_reason,
                        }
                        for item in queued.outcomes
                        if item.skip_reason is not None
                    ],
                },
                as_json=json_output,
                text=reason,
            )
            return
        first = queued.jobs[0]
        _emit(
            {
                "ok": True,
                "enqueued": True,
                "job": _job_payload(first),
                "jobs": [_job_payload(job) for job in queued.jobs],
                "skipped_targets": [
                    {
                        "target_language": item.target_language,
                        "reason": item.skip_reason,
                    }
                    for item in queued.outcomes
                    if item.skip_reason is not None
                ],
            },
            as_json=json_output,
            text=f"{first.id} {first.state}",
        )


@dub_app.command("process")
def dub_process(
    path: Path = typer.Argument(..., help="Video file inside a configured media root."),
    preview_seconds: int | None = typer.Option(
        None, "--preview-seconds", help="Dub only this many seconds, into staging."
    ),
    preview_offset_seconds: int = typer.Option(
        0, "--preview-offset-seconds", help="Where the preview window starts."
    ),
    source_language: str | None = typer.Option(
        None, "--source-language", help="Skip detection and force the source language."
    ),
    audio_stream_index: int | None = typer.Option(
        None,
        "--audio-stream-index",
        help="Global ffprobe stream index, not the relative a:N ordinal.",
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="cpu-fixed (MVP) or mac-clone (experimental)."
    ),
    stop_after: str | None = typer.Option(
        None, "--stop-after", help="Stop after this pipeline stage (probe, extract, ...)."
    ),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Enqueue and run one dubbing job. Existing subtitles do not skip this."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        resolved_profile = dubbing.parse_dubbing_profile(profile or config.dubbing.profile)
        stop_stage = _optional_stage(stop_after)
        with repository.StateDirLock(config.lock_path):
            queued = dubbing.enqueue_dubbing(
                config,
                repo,
                path,
                source_language=source_language,
                audio_stream_index=audio_stream_index,
                preview_seconds=None if preview_seconds is None else float(preview_seconds),
                preview_offset_seconds=float(preview_offset_seconds),
                profile=resolved_profile,
                require_stability=preview_seconds is None,
            )
            from .pipeline import build_context, run_job

            result = run_job(
                build_context(config, repo, queued.job),
                stop_after=stop_stage,
            )
            _emit(
                {
                    "ok": True,
                    "job_id": result.job_id,
                    "state": str(result.state),
                    "job_kind": str(JobKind.DUBBING),
                    "profile": str(resolved_profile),
                    "preview": queued.job.preview_seconds is not None,
                    "output": str(result.output_path) if result.output_path else None,
                    "last_stage": str(result.last_stage),
                },
                as_json=json_output,
                text=f"{result.job_id} {result.state} kind=dubbing",
            )


@dub_app.command("enqueue")
def dub_enqueue(
    path: Path = typer.Argument(..., help="Video file inside a configured media root."),
    source_language: str | None = typer.Option(None, "--source-language"),
    audio_stream_index: int | None = typer.Option(None, "--audio-stream-index"),
    profile: str | None = typer.Option(None, "--profile"),
    priority: int = typer.Option(0, "--priority", help="Higher runs first."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Add one dubbing job to the queue. Existing subtitles do not skip this."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        queued = dubbing.enqueue_dubbing(
            config,
            repo,
            path,
            source_language=source_language,
            audio_stream_index=audio_stream_index,
            priority=priority,
            profile=profile or config.dubbing.profile,
        )
        _emit(
            {
                "ok": True,
                "enqueued": True,
                "job": _job_payload(queued.job),
            },
            as_json=json_output,
            text=f"{queued.job.id} {queued.job.state} kind=dubbing",
        )


@dub_plan_app.command("export")
def dub_plan_export(
    job_id: str = typer.Argument(..., help="Dubbing job identifier."),
    destination: Path = typer.Option(..., "--output", help="JSON file to create, never overwrite."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Write the latest speech plan. Fails with output_conflict if the file exists."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        job, written = dubbing.export_plan(repo, job_id, destination=destination)
        segments = repo.list_dub_segments(job_id=job.id)
        _emit(
            {
                "ok": True,
                "job_id": job.id,
                "output": str(written),
                "revision": segments[0].revision if segments else 0,
            },
            as_json=json_output,
            text=f"{job.id} plan -> {written}",
        )


@dub_plan_app.command("apply")
def dub_plan_apply(
    job_id: str = typer.Argument(..., help="Dubbing job identifier."),
    source: Path = typer.Option(..., "--input", help="Edited plan JSON."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Validate and store the next revision of a speech plan."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        job, segments = dubbing.apply_plan(repo, job_id, source=source)
        _emit(
            {
                "ok": True,
                "job_id": job.id,
                "revision": segments[0].revision if segments else 0,
                "segments": len(segments),
            },
            as_json=json_output,
            text=f"{job.id} plan revision={segments[0].revision if segments else 0}",
        )


@app.command()
def scan(
    once: bool = typer.Option(False, "--once", help="One pass instead of the periodic loop."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be queued without queueing it."
    ),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Walk the configured roots and queue stable, unsubtitled videos."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        summary = discovery.scan(config, repo, dry_run=dry_run)
        _emit(
            {"ok": True, "enqueued": summary.enqueued, "examined": summary.examined},
            as_json=json_output,
            text=f"examined {summary.examined}, enqueued {summary.enqueued}",
        )


def _run_daemon(config_path: Path, json_output: bool) -> None:
    del json_output
    config = _load(config_path)
    repo = repository.open_repository(config)
    runner = worker.Worker(config, repo, owner=f"{platform.node()}:{os.getpid()}")
    raise typer.Exit(runner.run())


@app.command("worker")
def worker_command(
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Run the single daemon: periodic scan plus serial job processing."""
    with _handled(as_json=json_output):
        _run_daemon(config_path, json_output)


@app.command("daemon")
def daemon_command(
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Long-lived automatic processing service (same process as ``worker``)."""
    with _handled(as_json=json_output):
        _run_daemon(config_path, json_output)


@app.command("dashboard")
def dashboard_command(
    host: str | None = typer.Option(
        None, "--host", help="Override dashboard.bind. Defaults to loopback."
    ),
    port: int | None = typer.Option(None, "--port", help="Override dashboard.port."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Serve the operator dashboard. Does not process jobs or take the worker lock."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        bind = dashboard.resolve_dashboard_bind(config, host=host, port=port)
        _emit(
            {
                "ok": True,
                "bind": bind.host,
                "port": bind.port,
                "auth_required": bind.requires_token,
                "worker_independent": True,
            },
            as_json=json_output,
            text=f"dashboard on {bind.host}:{bind.port}",
        )
        dashboard.serve_dashboard(
            config,
            repo,
            host=bind.host,
            port=bind.port,
            token=bind.token,
            config_path=config_path,
        )


@app.command("webhooks")
def webhooks_command(
    host: str | None = typer.Option(
        None, "--host", help="Override webhooks.bind. Defaults to loopback."
    ),
    port: int | None = typer.Option(None, "--port", help="Override webhooks.port."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Listen for Sonarr/Radarr import events. Does not process jobs or take the worker lock."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        bind = webhooks.resolve_webhook_bind(config, host=host, port=port)
        _emit(
            {
                "ok": True,
                "bind": bind.host,
                "port": bind.port,
                "auth_required": True,
                "worker_independent": True,
            },
            as_json=json_output,
            text=f"webhooks on {bind.host}:{bind.port}",
        )
        webhooks.serve_webhooks(config, repo, host=bind.host, port=bind.port, token=bind.token)


@app.command()
def publish(
    job_id: str = typer.Argument(..., help="Job identifier."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Publish an approved job without re-transcribing it.

    Re-validates the fingerprint and any file already at the target name.
    """
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        job = repo.require_job(job_id)
        result = output.publish_job(config, repo, job)
        ok = result.outcome is PublishOutcome.PUBLISHED
        _emit(
            {
                "ok": ok,
                "outcome": str(result.outcome),
                "target": str(result.target_path),
            },
            as_json=json_output,
            text=f"{result.outcome} {result.target_path}",
        )
        if result.outcome is PublishOutcome.CONFLICT:
            raise typer.Exit(int(ExitCode.REVIEW_REQUIRED))
        if not ok:
            raise typer.Exit(int(ExitCode.PROCESSING_FAILED))


@app.command()
def benchmark(
    path: Path = typer.Argument(..., help="Video file inside a configured media root."),
    seconds: int = typer.Option(300, "--seconds", help="Length of the measured window."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Measure throughput, RTF and peak memory on a short window."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        with repository.StateDirLock(config.lock_path):
            job, skipped = discovery.enqueue_path(
                config,
                repo,
                path,
                preview_seconds=float(seconds),
                preview_offset_seconds=0.0,
                require_stability=False,
            )
            if skipped is not None:
                _emit(
                    {"ok": False, "reason": skipped},
                    as_json=json_output,
                    text=skipped,
                )
                raise typer.Exit(int(ExitCode.REVIEW_REQUIRED))
            assert job is not None
            from time import perf_counter

            from .pipeline import build_context, run_job

            started = perf_counter()
            result = run_job(build_context(config, repo, job))
            elapsed = perf_counter() - started
            rtf = elapsed / result.media_seconds if result.media_seconds else None
            payload = {
                "ok": True,
                "job_id": result.job_id,
                "elapsed_seconds": round(elapsed, 3),
                "media_seconds": result.media_seconds,
                "realtime_factor": None if rtf is None else round(rtf, 3),
                "cues": result.cue_count,
                "architecture": platform.machine(),
                "note": "measured on this host only; not a NAS result",
            }
            _emit(
                payload,
                as_json=json_output,
                text=(
                    f"{elapsed:.1f}s for {result.media_seconds:.1f}s audio "
                    f"RTF={rtf if rtf is not None else 'n/a'} on {platform.machine()}"
                ),
            )


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #


@jobs_app.command("list")
def jobs_list(
    state: JobState | None = typer.Option(None, "--state", help="Filter by job state."),
    limit: int = typer.Option(50, "--limit", min=1, max=1000),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """List jobs, optionally filtered by state."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        records = repo.list_jobs(state=state, limit=limit)
        _emit(
            {"ok": True, "jobs": [_job_payload(record) for record in records]},
            as_json=json_output,
            text="\n".join(f"{record.id} {record.state}" for record in records) or "(no jobs)",
        )


@jobs_app.command("show")
def jobs_show(
    job_id: str = typer.Argument(..., help="Job identifier."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Show one job with its stage, attempts, artifacts and quality flags."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        record = repo.require_job(job_id)
        artifacts = repo.list_artifacts(job_id=job_id)
        metrics = repo.get_metrics(job_id)
        manifest = output.read_manifest_payload(config, job_id)
        payload = {
            "ok": True,
            "job": _job_payload(record),
            "language": None
            if manifest is None
            else {
                "selected_audio_stream_index": manifest.get("selected_audio_stream_index"),
                "stream_language": manifest.get("stream_language"),
                "detected_language": manifest.get("detected_language"),
                "detection_probability": manifest.get("detection_probability"),
                "source_language": manifest.get("source_language"),
                "source_language_source": manifest.get("source_language_source"),
                "target_language": manifest.get("target_language"),
                "translation_executed": manifest.get("translation_executed"),
                "models": manifest.get("models"),
            },
            "artifacts": [
                {"stage": str(item.stage), "chunk_index": item.chunk_index, "sha256": item.sha256}
                for item in artifacts
            ],
            "metrics": None
            if metrics is None
            else {
                "output_cues": metrics.output_cues,
                "total_seconds": metrics.total_seconds,
                "quality_flags": [str(flag.code) for flag in metrics.quality_flags],
            },
        }
        text = (
            f"{record.id} {record.state} stage={record.current_stage} "
            f"attempts={record.attempt_count}"
        )
        _emit(payload, as_json=json_output, text=text)


@jobs_app.command("retry")
def jobs_retry(
    job_id: str = typer.Argument(..., help="Job identifier."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Send a failed job back to the queue, keeping valid checkpoints."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        record = repo.transition(job_id=job_id, state=JobState.QUEUED)
        _emit(
            {"ok": True, "job": _job_payload(record)},
            as_json=json_output,
            text=f"{record.id} {record.state}",
        )


@jobs_app.command("cancel")
def jobs_cancel(
    job_id: str = typer.Argument(..., help="Job identifier."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Cancel a job. Blocks publication and preserves checkpoints."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        record = repo.transition(job_id=job_id, state=JobState.CANCELLED)
        _emit(
            {"ok": True, "job": _job_payload(record)},
            as_json=json_output,
            text=f"{record.id} {record.state}",
        )


@jobs_app.command("approve")
def jobs_approve(
    job_id: str = typer.Argument(..., help="Job identifier."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Authorise publication of a reviewed job.

    Only works once the structural errors are gone; the approval is stamped
    with a timestamp.
    """
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        record = repo.approve_job(job_id=job_id)
        _emit(
            {"ok": True, "job": _job_payload(record)},
            as_json=json_output,
            text=f"{record.id} {record.state} approved_at={record.approved_at}",
        )


# --------------------------------------------------------------------------- #
# maintenance
# --------------------------------------------------------------------------- #


@app.command()
def cleanup(
    older_than_days: int = typer.Option(
        30, "--older-than-days", min=0, help="Keep transcripts and manifests this long."
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="Dry run by default; pass --apply to actually delete.",
    ),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Remove stale intermediates inside work_dir. Never touches the library."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        summary = worker.cleanup_work_dir(
            config, repo, older_than_days=older_than_days, dry_run=dry_run
        )
        _emit(
            {
                "ok": True,
                "dry_run": summary.dry_run,
                "removed": list(summary.removed_paths),
                "reclaimed_bytes": summary.reclaimed_bytes,
            },
            as_json=json_output,
            text=f"{len(summary.removed_paths)} paths, dry_run={summary.dry_run}",
        )


@app.command()
def backup(
    destination: Path = typer.Option(..., "--destination", help="Directory for the backup."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Back up config, model manifest and the SQLite database consistently."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        written = worker.backup_state(config, destination)
        _emit(
            {"ok": True, "destination": str(written)},
            as_json=json_output,
            text=f"backup written to {written}",
        )


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def _optional_stage(value: str | None) -> PipelineStage | None:
    if value is None or value.strip() == "":
        return None
    try:
        return PipelineStage(value.strip())
    except ValueError as exc:
        raise NasSubtitlesError(
            f"unknown pipeline stage {value!r}",
            code=ErrorCode.CONFIG_INVALID,
            detail={"stage": value},
        ) from exc


def _job_payload(record: JobRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "state": str(record.state),
        "root_id": record.root_id,
        "relative_path": record.relative_path,
        "current_stage": str(record.current_stage) if record.current_stage else None,
        "attempt_count": record.attempt_count,
        "error_code": str(record.error_code) if record.error_code else None,
        "priority": record.priority,
        "approved_at": record.approved_at.isoformat() if record.approved_at else None,
        "output_path": str(record.output_path) if record.output_path else None,
        "target_language": record.target_language,
        "job_kind": str(infer_job_kind(record.job_kind)),
        "dubbing_profile": record.dubbing_profile,
    }


def main() -> None:
    """Console script entry point."""
    configure_logging(level=logging.INFO)
    try:
        app()
    except NasSubtitlesError as exc:  # pragma: no cover - defensive outer boundary
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        raise SystemExit(int(exc.exit_code)) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
