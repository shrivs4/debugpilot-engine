"""Repository file discovery.

Derived from poc/repository_scanner.py. Differences: directory pruning instead
of rglob (node_modules is never walked), symlinks are skipped so indexing can
never read outside the clone, and each file carries a content hash so a later
phase can index incrementally.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "__pycache__",
        ".next",
        "coverage",
    }
)

IGNORED_FILES = frozenset({"package-lock.json"})

# Extension -> language label stored with files and chunks.
SUPPORTED_EXTENSIONS = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".md": "markdown",
}


@dataclass(frozen=True)
class ScannedFile:
    path: str  # repository-relative, POSIX separators
    language: str
    size_bytes: int
    line_count: int
    content_hash: str
    content: str


def detect_language(path: PurePosixPath | Path) -> str | None:
    return SUPPORTED_EXTENSIONS.get(path.suffix.lower())


def should_ignore(relative_path: PurePosixPath | Path) -> bool:
    if relative_path.name in IGNORED_FILES:
        return True
    return any(part in IGNORED_DIRECTORIES for part in relative_path.parts)


def scan_repository(repository_root: Path, max_file_size_bytes: int) -> list[ScannedFile]:
    repository_root = repository_root.resolve()
    files: list[ScannedFile] = []

    for directory, subdirectories, file_names in os.walk(repository_root, followlinks=False):
        subdirectories[:] = sorted(
            name for name in subdirectories if name not in IGNORED_DIRECTORIES
        )

        for file_name in file_names:
            absolute_path = Path(directory) / file_name
            relative_path = PurePosixPath(absolute_path.relative_to(repository_root).as_posix())

            if absolute_path.is_symlink() or should_ignore(relative_path):
                continue

            language = detect_language(relative_path)
            if language is None:
                continue

            size_bytes = absolute_path.stat().st_size
            if size_bytes > max_file_size_bytes:
                continue

            raw = absolute_path.read_bytes()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue

            files.append(
                ScannedFile(
                    path=str(relative_path),
                    language=language,
                    size_bytes=size_bytes,
                    line_count=len(content.splitlines()),
                    content_hash=hashlib.sha256(raw).hexdigest(),
                    content=content,
                )
            )

    return sorted(files, key=lambda scanned: scanned.path)
