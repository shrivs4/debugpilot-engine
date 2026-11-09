"""User-approved command execution.

Derived from poc/collect_runtime.py. Commands are tokenized with shlex and run
as an argument list (never `shell=True`) inside the repository clone, with a
reduced environment, a timeout that kills the whole process group, and output
size limits.
"""

import os
import shlex
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


SHELL_OPERATOR_CHARS = ";&|<>"
FORBIDDEN_SUBSTRINGS = ("`", "$(", "\n", "\r")


class CommandValidationError(ValueError):
    pass


@dataclass
class CommandResult:
    command: str
    argv: list[str]
    exit_code: int | None
    success: bool
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    duration_ms: int
    timed_out: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_command(command: str) -> list[str]:
    if command is None or not command.strip():
        raise CommandValidationError("Command is empty.")
    if any(token in command for token in FORBIDDEN_SUBSTRINGS):
        raise CommandValidationError(
            "Command substitution and multi-line commands are not supported; commands run without a shell."
        )
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise CommandValidationError(f"Command could not be parsed: {error}") from error
    # A punctuation-aware pass finds unquoted operators even when glued to words ("test;").
    lexer = shlex.shlex(command, posix=True, punctuation_chars=SHELL_OPERATOR_CHARS)
    lexer.whitespace_split = True
    operators = [token for token in lexer if token and set(token) <= set(SHELL_OPERATOR_CHARS)]
    if operators:
        raise CommandValidationError(
            f"Shell operators ({', '.join(operators)}) are not supported because commands run without a shell. "
            "Provide a single command such as `npm run repro:legacy`."
        )
    return argv


def command_environment(workspace_dir: Path) -> dict[str, str]:
    """Minimal environment: no database credentials or other server secrets leak into user commands."""
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "TMPDIR", "USER"}}
    env.update(
        {
            "CI": "1",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "npm_config_cache": str(workspace_dir / ".npm-cache"),
            "npm_config_update_notifier": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
        }
    )
    return env


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f"{text[:half]}\n\n... [{omitted} characters truncated] ...\n\n{text[-half:]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_command(
    command: str,
    cwd: Path,
    timeout_seconds: int,
    max_output_chars: int,
    env: dict[str, str] | None = None,
) -> CommandResult:
    argv = parse_command(command)
    started_at = _now()
    started = time.monotonic()

    def result(exit_code, stdout, stderr, timed_out=False, error=None) -> CommandResult:
        return CommandResult(
            command=shlex.join(argv),
            argv=argv,
            exit_code=exit_code,
            success=exit_code == 0 and not timed_out and error is None,
            stdout=_truncate(stdout or "", max_output_chars),
            stderr=_truncate(stderr or "", max_output_chars),
            started_at=started_at,
            finished_at=_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            error=error,
        )

    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,  # own process group so a timeout kills npm's children too
        )
    except FileNotFoundError:
        return result(None, "", "", error=f"Executable not found: {argv[0]}")
    except PermissionError:
        return result(None, "", "", error=f"Permission denied executing: {argv[0]}")

    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        return result(None, stdout, stderr, timed_out=True, error=f"Command timed out after {timeout_seconds}s")

    return result(process.returncode, stdout, stderr)
