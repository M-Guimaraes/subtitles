"""CLI surface: the documented commands, --json everywhere, and exit codes."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nas_subtitles.cli import app
from nas_subtitles.config import AppConfig
from nas_subtitles.domain import HEARTBEAT_EVENT_CODE, ExitCode

runner = CliRunner()

DOCUMENTED_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("doctor",),
    ("models", "install"),
    ("models", "verify"),
    ("inspect",),
    ("process",),
    ("enqueue",),
    ("scan",),
    ("worker",),
    ("jobs", "list"),
    ("jobs", "show"),
    ("jobs", "retry"),
    ("jobs", "cancel"),
    ("jobs", "approve"),
    ("publish",),
    ("benchmark",),
    ("health",),
    ("cleanup",),
    ("backup",),
)


@pytest.mark.parametrize("command", DOCUMENTED_COMMANDS, ids=lambda c: " ".join(c))
def test_command_exists_and_offers_json(command: tuple[str, ...]) -> None:
    result = runner.invoke(app, [*command, "--help"])
    assert result.exit_code == 0, result.output
    assert "--json" in result.output


def _seed_heartbeat(config: AppConfig, *, age_seconds: int) -> None:
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.database_path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            job_id TEXT,
            level TEXT,
            code TEXT NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    moment = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    connection.execute(
        "INSERT INTO events (code, level, created_at) VALUES (?, 'info', ?)",
        (HEARTBEAT_EVENT_CODE, moment.isoformat()),
    )
    connection.commit()
    connection.close()


def test_health_is_unhealthy_without_a_database(config_path: Path) -> None:
    result = runner.invoke(app, ["health", "--config", str(config_path), "--json"])
    assert result.exit_code == int(ExitCode.PREFLIGHT_FAILED)
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["database_reachable"] is False


def test_health_is_healthy_with_a_recent_heartbeat(config: AppConfig, config_path: Path) -> None:
    _seed_heartbeat(config, age_seconds=5)
    result = runner.invoke(app, ["health", "--config", str(config_path), "--json"])
    assert result.exit_code == int(ExitCode.SUCCESS)
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["heartbeat_age_seconds"] < config.worker.stale_lease_seconds


def test_health_is_unhealthy_with_a_stale_heartbeat(config: AppConfig, config_path: Path) -> None:
    _seed_heartbeat(config, age_seconds=config.worker.stale_lease_seconds + 60)
    result = runner.invoke(app, ["health", "--config", str(config_path), "--json"])
    assert result.exit_code == int(ExitCode.PREFLIGHT_FAILED)
    assert json.loads(result.stdout)["ok"] is False


def test_health_does_not_create_the_database(config: AppConfig, config_path: Path) -> None:
    runner.invoke(app, ["health", "--config", str(config_path)])
    assert not config.database_path.exists()


def test_invalid_configuration_exits_with_two(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("media_roots: []\n", encoding="utf-8")
    result = runner.invoke(app, ["health", "--config", str(broken), "--json"])
    assert result.exit_code == int(ExitCode.INVALID_INPUT)


def test_missing_configuration_exits_with_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["health", "--config", str(tmp_path / "absent.yaml")])
    assert result.exit_code == int(ExitCode.INVALID_INPUT)


def test_unimplemented_command_reports_a_structured_failure(config_path: Path) -> None:
    """Stubs must fail with a code, not a traceback."""
    result = runner.invoke(app, ["scan", "--once", "--config", str(config_path), "--json"])
    assert result.exit_code == int(ExitCode.PROCESSING_FAILED)
    payload = json.loads(result.stderr)
    assert payload["error_code"] == "not_implemented"


def test_doctor_reports_every_check(config_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--config", str(config_path), "--json"])
    payload = json.loads(result.stdout)
    names = {check["name"] for check in payload["checks"]}
    assert {"python", "cpu", "memory", "state_dir", "work_dir", "free_space"} <= names


def test_no_command_prompts_for_input() -> None:
    """Every command must run unattended; an empty stdin must not block."""
    result = runner.invoke(app, ["health", "--config", "/nonexistent/config.yaml"], input="")
    assert result.exit_code == int(ExitCode.INVALID_INPUT)
