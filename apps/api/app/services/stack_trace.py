"""Stack trace extraction from command output.

Generalizes the single regex in poc/validate_hypothesis.py. Understands Node
V8 frames (`at fn (path:line:col)` / `at path:line:col`), the `path:line`
source header Node prints above uncaught errors, and Vitest's `❯ path:line:col`.
Paths are normalized to repository-relative form when they point into the clone.
"""

import re
from pathlib import Path


V8_FRAME = re.compile(r"^\s*at (?:(?P<function>.+?) \()?(?P<location>[^\s()]+):(?P<line>\d+):(?P<column>\d+)\)?\s*$")
VITEST_FRAME = re.compile(r"^\s*❯ (?P<location>[^\s:]+):(?P<line>\d+):(?P<column>\d+)")
NODE_HEADER = re.compile(r"^(?P<location>(?:file://)?/[^\s:]+):(?P<line>\d+)$")
ERROR_LINE = re.compile(r"^\s*(?:Uncaught )?(?P<type>[A-Z][A-Za-z0-9_]*(?:Error|Exception))(?: \[[^\]]+\])?: (?P<message>.+)$")


def _relative_to_repository(location: str, repository_root: Path | None) -> str | None:
    location = location.removeprefix("file://")
    if location.startswith(("node:", "internal/")) or "node_modules" in location:
        return None
    if repository_root is not None:
        root = str(repository_root.resolve()).rstrip("/") + "/"
        if location.startswith(root):
            return location[len(root):]
    if not location.startswith("/") and not location.startswith("<"):
        return location.removeprefix("./")
    return None


def parse_stack_trace(output: str, repository_root: Path | None = None) -> dict | None:
    if not output:
        return None

    lines = output.splitlines()
    error_type = error_message = None
    error_index = None
    frames: list[dict] = []
    header: dict | None = None

    for index, line in enumerate(lines):
        if error_type is None:
            error_match = ERROR_LINE.match(line)
            if error_match:
                error_type, error_message = error_match.group("type"), error_match.group("message").strip()
                error_index = index
                continue

        if header is None and not frames:
            header_match = NODE_HEADER.match(line.strip())
            if header_match:
                header = {
                    "location": header_match.group("location"),
                    "line": int(header_match.group("line")),
                    "source_excerpt": "\n".join(lines[index + 1:index + 3]).rstrip(),
                }
                continue

        frame_match = V8_FRAME.match(line) or VITEST_FRAME.match(line)
        if frame_match:
            location = frame_match.group("location")
            relative = _relative_to_repository(location, repository_root)
            groups = frame_match.groupdict()
            frames.append(
                {
                    "function": (groups.get("function") or "<anonymous>").strip(),
                    "path": location,
                    "relative_path": relative,
                    "line": int(frame_match.group("line")),
                    "column": int(frame_match.group("column")),
                    "in_repository": relative is not None,
                }
            )

    if error_type is None and not frames:
        return None

    raw_start = error_index if error_index is not None else 0
    raw_lines = [line for line in lines[raw_start:] if line.strip()]
    top_frame = next((frame for frame in frames if frame["in_repository"]), None)

    return {
        "error_type": error_type,
        "error_message": error_message,
        "header": header,
        "frames": frames,
        "top_repository_frame": top_frame,
        "raw": "\n".join(raw_lines[:40]),
    }
