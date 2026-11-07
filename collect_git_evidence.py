import json
import subprocess
import argparse
from pathlib import Path


def run_git_command(
    repository_path: Path,
    command: list[str],
) -> str:
    completed = subprocess.run(
        command,
        cwd=repository_path,
        capture_output=True,
        text=True,
        check=True,
    )

    return completed.stdout


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect Git evidence from a target repository."
    )
    parser.add_argument("repository_path", help="Target repository path")
    parser.add_argument(
        "--commit",
        required=True,
        help="Commit hash or ref to inspect",
    )
    parser.add_argument(
        "--diff-files",
        nargs="+",
        required=True,
        help="One or more repository-relative files for the relevant diff",
    )
    parser.add_argument(
        "--blame-file",
        required=True,
        help="Repository-relative file for blame",
    )
    parser.add_argument(
        "--blame-lines",
        required=True,
        help="Blame line range in START:END format",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output JSON path",
    )
    args = parser.parse_args()

    repository_path = Path(args.repository_path).expanduser().resolve()
    if not repository_path.is_dir():
        parser.error(f"repository path does not exist or is not a directory: {repository_path}")

    try:
        start_line, end_line = args.blame_lines.split(":", 1)
        if int(start_line) < 1 or int(end_line) < int(start_line):
            raise ValueError
    except ValueError:
        parser.error("--blame-lines must be a valid range in START:END format")

    commit = args.commit

    git_evidence = {
        "commit_summary": run_git_command(
            repository_path,
            ["git", "show", "--stat", commit],
        ),
        "relevant_diff": run_git_command(
            repository_path,
            [
                "git",
                "show",
                commit,
                "--",
                *args.diff_files,
            ],
        ),
        "line_history": run_git_command(
            repository_path,
            [
                "git",
                "blame",
                "-L",
                f"{start_line},{end_line}",
                "--",
                args.blame_file,
            ],
        ),
    }

    output_path = Path(args.output)

    output_path.write_text(
        json.dumps(git_evidence, indent=2),
        encoding="utf-8",
    )

    print(f"Created: {output_path.resolve()}")
    print()
    print(git_evidence["line_history"])


if __name__ == "__main__":
    main()
