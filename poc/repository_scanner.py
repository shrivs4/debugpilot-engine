from pathlib import Path
import sys


IGNORED_DIRECTORIES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".next",
}

IGNORED_FILES = {
    "package-lock.json",
}

SUPPORTED_EXTENSIONS = {
      ".ts": "TypeScript",
      ".tsx": "TypeScript React",
      ".js": "JavaScript",
      ".jsx": "JavaScript React",
      ".json": "JSON",
      ".md": "Markdown",
      ".yml": "YAML",
      ".yaml": "YAML",
      ".sql": "SQL",
      ".py": "Python",
}

MAX_FILE_SIZE_BYTES = 200_000


def detect_language(file_path: Path) -> str | None:
    return SUPPORTED_EXTENSIONS.get(file_path.suffix.lower())


def should_ignore(file_path: Path) -> bool:
    if file_path.name in IGNORED_FILES:
        return True

    return any(
        directory in IGNORED_DIRECTORIES
        for directory in file_path.parts
    )


def scan_repository(repository_path: Path) -> list[dict]:
    files = []

    for file_path in repository_path.rglob("*"):
        if not file_path.is_file():
            continue

        if should_ignore(file_path):
            continue

        language = detect_language(file_path)

        if language is None:
            continue

        file_size = file_path.stat().st_size

        if file_size > MAX_FILE_SIZE_BYTES:
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        files.append(
            {
                "path": str(file_path.relative_to(repository_path)),
                "language": language,
                "size_bytes": file_size,
                "line_count": len(content.splitlines()),
            }
        )

    return sorted(files, key=lambda file: file["path"])


def print_summary(repository_path: Path, files: list[dict]) -> None:
    print(f"Repository: {repository_path}")
    print(f"Files discovered: {len(files)}")
    print()

    for file in files:
        print(
            f"{file['language']:<20}"
            f"{file['line_count']:>5} lines  "
            f"{file['path']}"
        )

    total_lines = sum(file["line_count"] for file in files)

    print()
    print(f"Total lines: {total_lines}")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python repository_scanner.py <repository-path>")
        raise SystemExit(1)

    repository_path = Path(sys.argv[1]).expanduser().resolve()

    if not repository_path.exists():
        print(f"Repository does not exist: {repository_path}")
        raise SystemExit(1)

    if not repository_path.is_dir():
        print(f"Path is not a directory: {repository_path}")
        raise SystemExit(1)

    files = scan_repository(repository_path)
    print_summary(repository_path, files)


if __name__ == "__main__":
    main()