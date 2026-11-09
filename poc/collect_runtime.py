import json
import shlex
import subprocess
import sys
from pathlib import Path


def main() -> None:
    if "--" not in sys.argv:
        print(
            "Usage: python collect_runtime.py "
            "<repository-path> "
            "[--output <output-file>] "
            "-- <command>"
        )
        raise SystemExit(1)

    separator_index = sys.argv.index("--")

    if separator_index < 2:
        print("A repository path is required.")
        raise SystemExit(1)

    repository_path = (
        Path(sys.argv[1])
        .expanduser()
        .resolve()
    )

    options = sys.argv[2:separator_index]
    output_path = Path("runtime_evidence.json")

    if options:
        if (
            len(options) != 2
            or options[0] != "--output"
        ):
            print(
                "Optional arguments must be: "
                "--output <output-file>"
            )
            raise SystemExit(1)

        output_path = Path(options[1])

    command = sys.argv[separator_index + 1:]

    if not repository_path.is_dir():
        print(
            f"Repository does not exist: "
            f"{repository_path}"
        )
        raise SystemExit(1)

    if not command:
        print("No command was provided after '--'.")
        raise SystemExit(1)

    completed = subprocess.run(
        command,
        cwd=repository_path,
        capture_output=True,
        text=True,
        timeout=60,
    )

    runtime_evidence = {
        "repository": str(repository_path),
        "command": shlex.join(command),
        "exit_code": completed.returncode,
        "success": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }

    output_path.write_text(
        json.dumps(
            runtime_evidence,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"Command: "
        f"{runtime_evidence['command']}"
    )
    print(
        f"Exit code: "
        f"{runtime_evidence['exit_code']}"
    )
    print(f"Created: {output_path.resolve()}")


if __name__ == "__main__":
    main()