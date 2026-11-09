"""Git evidence for a debug run.

poc/collect_git_evidence.py needed the commit, diff files and blame range
passed by hand. Here the target is derived from evidence: the top in-repository
stack frame (or the best retrieved chunk), blamed to find the commit that last
changed the failing line; that commit's summary and diff are then captured.
"""

from pathlib import Path

from app.services import git_service
from app.services.git_service import GitCommandError


BLAME_CONTEXT_LINES = 3
MAX_DIFF_CHARS = 20_000


def _target(stack_trace: dict | None, retrieved: list[dict]) -> tuple[str, int, str] | None:
    frame = (stack_trace or {}).get("top_repository_frame")
    if frame:
        return frame["relative_path"], frame["line"], "stack_trace"
    if retrieved:
        return retrieved[0]["file_path"], retrieved[0]["start_line"], "retrieval"
    return None


def collect_git_evidence(repository_path: Path, commit_sha: str, stack_trace: dict | None, retrieved: list[dict]) -> dict:
    evidence: dict = {
        "head_commit": commit_sha,
        "head_summary": None,
        "target": None,
        "blame": None,
        "blame_lines": [],
        "suspect_commit": None,
        "suspect_commit_summary": None,
        "suspect_diff": None,
        "file_history": [],
        "errors": [],
    }

    try:
        evidence["head_summary"] = git_service.show_stat(repository_path, commit_sha)
    except GitCommandError as error:
        evidence["errors"].append(str(error))

    target = _target(stack_trace, retrieved)
    if target is None:
        return evidence

    file_path, line, source = target
    evidence["target"] = {"file_path": file_path, "line": line, "source": source}

    absolute = (repository_path / file_path).resolve()
    if not absolute.is_file() or repository_path.resolve() not in absolute.parents:
        evidence["errors"].append(f"Target file {file_path} is not in the repository checkout.")
        return evidence

    line_count = len(absolute.read_text(encoding="utf-8", errors="replace").splitlines())
    start = max(1, line - BLAME_CONTEXT_LINES)
    end = min(line_count, line + BLAME_CONTEXT_LINES)
    if line > line_count or start > end:
        evidence["errors"].append(f"Line {line} is outside {file_path} ({line_count} lines).")
        return evidence

    try:
        evidence["blame"] = git_service.blame(repository_path, file_path, start, end)
        evidence["blame_lines"] = git_service.blame_porcelain(repository_path, file_path, start, end)
        evidence["file_history"] = git_service.file_history(repository_path, file_path)
    except GitCommandError as error:
        evidence["errors"].append(str(error))
        return evidence

    target_entry = next((entry for entry in evidence["blame_lines"] if entry["line"] == line), None)
    if target_entry is None or set(target_entry["commit"]) == {"0"}:
        return evidence

    suspect = target_entry["commit"]
    evidence["suspect_commit"] = {
        "commit": suspect,
        "short": suspect[:8],
        "author": target_entry.get("author"),
        "summary": target_entry.get("summary"),
        "line_content": target_entry.get("content"),
    }
    try:
        evidence["suspect_commit_summary"] = git_service.show_stat(repository_path, suspect)
        evidence["suspect_diff"] = git_service.show_diff(repository_path, suspect, [file_path])[:MAX_DIFF_CHARS]
    except GitCommandError as error:
        evidence["errors"].append(str(error))

    return evidence
