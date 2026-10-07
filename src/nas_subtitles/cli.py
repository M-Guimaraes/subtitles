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

from . import discovery, models, output, repository, worker
from .config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from .domain import (
    ErrorCode,
    ExitCode,
    JobState,
    NasSubtitlesError,
    PublishMode,
    exit_code_for,
)
from .health import check_health
from .logging_setup import configure_logging
from .media import FfprobeMediaProbe

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
app.add_typer(models_app, name="models")
app.add_typer(jobs_app, name="jobs")


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

    checks.append(DoctorCheck("models", "pending", "verified by `nas-subs models verify`"))
    checks.append(
        DoctorCheck(
            "atomic_publish",
            "pending",
            "hard-link probe lands with the publication stage",
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
    """Download the Whisper model and the direct en->pt Argos package.

    The only command allowed to use the network.
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
        _emit(
            {"ok": True, "root_id": root.root_id, "duration": probe_result.duration_seconds},
            as_json=json_output,
            text=f"{resolved}: {probe_result.duration_seconds:.1f}s",
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
        discovery.resolve_explicit_path(config, path)
        raise NotImplementedError("`process` lands with the pipeline stages (4 to 7)")


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
        discovery.resolve_explicit_path(config, path)
        repository.open_repository(config)


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


@app.command("worker")
def worker_command(
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Run the single daemon: periodic scan plus serial job processing."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repo = repository.open_repository(config)
        runner = worker.Worker(config, repo, owner=f"{platform.node()}:{os.getpid()}")
        raise typer.Exit(runner.run())


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
        if config.publish_mode is PublishMode.SIDECAR:
            output.supports_atomic_publish(config.output_dir)
        repository.open_repository(config)


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
        discovery.resolve_explicit_path(config, path)
        raise NotImplementedError("`benchmark` lands with the delivery stage (8)")


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
            {"ok": True, "jobs": [record.id for record in records]},
            as_json=json_output,
            text="\n".join(f"{record.id} {record.state}" for record in records),
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
        repository.open_repository(config).get_job(job_id)


@jobs_app.command("retry")
def jobs_retry(
    job_id: str = typer.Argument(..., help="Job identifier."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Send a failed job back to the queue, keeping valid checkpoints."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repository.open_repository(config).transition(job_id=job_id, state=JobState.QUEUED)


@jobs_app.command("cancel")
def jobs_cancel(
    job_id: str = typer.Argument(..., help="Job identifier."),
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Cancel a job. Blocks publication and preserves checkpoints."""
    with _handled(as_json=json_output):
        config = _load(config_path)
        repository.open_repository(config).transition(job_id=job_id, state=JobState.CANCELLED)


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
        repository.open_repository(config).approve_job(job_id=job_id)


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
