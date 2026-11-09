import shlex
import sys
from pathlib import Path

import pytest

from app.services.command_runner import CommandValidationError, command_environment, parse_command, run_command


PYTHON = shlex.quote(sys.executable)


def test_parse_command_tokenizes_without_a_shell():
    assert parse_command("npm run repro:legacy") == ["npm", "run", "repro:legacy"]
    assert parse_command("node -e 'console.log(1)'") == ["node", "-e", "console.log(1)"]
    assert parse_command("node -e process.exit(4)") == ["node", "-e", "process.exit(4)"]
    assert parse_command("node -e 'a && b; c | d'") == ["node", "-e", "a && b; c | d"]


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "npm install && npm test",
        "npm test | tee out",
        "npm test; rm -rf /",
        "npm test>out",
        "npm test 2>&1",
        "npm test &",
        "echo `id`",
        "echo $(id)",
        "npm test > out",
    ],
)
def test_parse_command_rejects_shell_syntax(command):
    with pytest.raises(CommandValidationError):
        parse_command(command)


def test_run_command_captures_exit_code_stdout_stderr(tmp_path: Path):
    script = "import sys; print('out-line'); print('err-line', file=sys.stderr); sys.exit(3)"
    result = run_command(f"{PYTHON} -c {shlex.quote(script)}", tmp_path, timeout_seconds=30, max_output_chars=10_000)

    assert result.exit_code == 3
    assert result.success is False
    assert result.stdout.strip() == "out-line"
    assert result.stderr.strip() == "err-line"
    assert result.argv[0] == sys.executable
    assert result.started_at and result.finished_at and result.duration_ms >= 0


def test_run_command_runs_in_repository_directory(tmp_path: Path):
    result = run_command(f"{PYTHON} -c 'import os; print(os.getcwd())'", tmp_path, 30, 10_000)

    assert result.success is True
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_shell_metacharacters_inside_arguments_are_not_interpreted(tmp_path: Path):
    marker = tmp_path / "should-not-exist"
    result = run_command(f"{PYTHON} -c 'import sys; print(sys.argv[1:])' 'x;touch {marker}'", tmp_path, 30, 10_000)

    assert result.success is True
    assert not marker.exists()


def test_run_command_times_out(tmp_path: Path):
    result = run_command(f"{PYTHON} -c 'import time; time.sleep(10)'", tmp_path, timeout_seconds=1, max_output_chars=10_000)

    assert result.timed_out is True
    assert result.success is False
    assert "timed out" in result.error


def test_missing_executable_is_reported(tmp_path: Path):
    result = run_command("definitely-not-a-real-binary --help", tmp_path, 30, 10_000)

    assert result.success is False
    assert result.exit_code is None
    assert "Executable not found" in result.error


def test_output_is_truncated(tmp_path: Path):
    result = run_command(f"{PYTHON} -c 'print(\"x\" * 5000)'", tmp_path, 30, max_output_chars=1000)

    assert "characters truncated" in result.stdout
    assert len(result.stdout) < 1200


def test_command_environment_excludes_server_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    env = command_environment(tmp_path)

    assert "DATABASE_URL" not in env
    assert env["CI"] == "1"
    assert env["npm_config_cache"] == str(tmp_path / ".npm-cache")
