"""Deterministic hypothesis validation.

Derived from poc/validate_hypothesis.py. The POC checks (command failed, the
runtime file exists, the hypothesis names the runtime location, the failing
line contains `discountType`, the hypothesis names the introducing commit) are
kept but generalized: the expected identifier comes from the error message and
the expected commit from `git blame`, not from hardcoded fixture values.

Each check is `passed`, `failed` or `skipped` (not applicable). A run is
`validated` only when every required, applicable check passes.
"""

import re
from pathlib import Path


JS_KEYWORDS = frozenset(
    "const let var function return if else for while new this typeof instanceof await async "
    "import export from class extends true false null undefined throw try catch".split()
)


def _check(name: str, status: str, details: str, required: bool = True) -> dict:
    return {"name": name, "status": status, "details": details, "required": required}


def _normalize_path(path: str | None, repository_root: Path) -> str | None:
    if not path:
        return None
    path = path.strip().strip("`").removeprefix("file://")
    root = str(repository_root.resolve()).rstrip("/") + "/"
    if path.startswith(root):
        path = path[len(root):]
    return path.removeprefix("./")


def _paths_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left == right or left.endswith("/" + right) or right.endswith("/" + left)


def _source_line(repository_root: Path, relative_path: str, line: int) -> str | None:
    absolute = (repository_root / relative_path).resolve()
    if repository_root.resolve() not in absolute.parents or not absolute.is_file():
        return None
    lines = absolute.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[line - 1] if 1 <= line <= len(lines) else None


def error_identifiers(error_message: str | None) -> list[str]:
    """Identifiers the runtime error names, e.g. `toUpperCase` in "(reading 'toUpperCase')"."""
    if not error_message:
        return []
    quoted = re.findall(r"['\"`]([A-Za-z_$][\w$]*)['\"`]", error_message)
    return list(dict.fromkeys(quoted))


def validate_hypothesis(
    hypothesis: dict,
    analysis_runtime: dict | None,
    stack_trace: dict | None,
    git_evidence: dict,
    retrieved: list[dict],
    repository_root: Path,
    indexed_symbols: set[tuple[str, str]],
) -> dict:
    checks: list[dict] = []
    hypothesis_path = _normalize_path(hypothesis.get("file_path"), repository_root)
    hypothesis_line = hypothesis.get("line")
    frame = (stack_trace or {}).get("top_repository_frame")

    # 1. The reproduction command must actually reproduce a failure.
    if analysis_runtime is None:
        checks.append(_check("reproduction_failed", "failed", "No analysis command result was captured."))
    else:
        failed = analysis_runtime.get("exit_code") not in (0, None) or bool(analysis_runtime.get("timed_out"))
        checks.append(
            _check(
                "reproduction_failed",
                "passed" if failed else "failed",
                f"Exit code: {analysis_runtime.get('exit_code')}"
                + (" (timed out)" if analysis_runtime.get("timed_out") else ""),
            )
        )

    # 2. The runtime-reported location exists in the checkout.
    if frame is None:
        checks.append(_check("runtime_location_exists", "skipped", "No in-repository stack frame was found."))
    else:
        source = _source_line(repository_root, frame["relative_path"], frame["line"])
        checks.append(
            _check(
                "runtime_location_exists",
                "passed" if source is not None else "failed",
                f"{frame['relative_path']}:{frame['line']}:{frame['column']}",
            )
        )

    # 3 + 4. The hypothesis names the runtime file and line.
    if frame is None:
        checks.append(_check("hypothesis_file_matches_runtime", "skipped", "No in-repository stack frame."))
        checks.append(_check("hypothesis_line_matches_runtime", "skipped", "No in-repository stack frame."))
    else:
        file_ok = _paths_match(hypothesis_path, frame["relative_path"])
        checks.append(
            _check(
                "hypothesis_file_matches_runtime",
                "passed" if file_ok else "failed",
                f"Hypothesis: {hypothesis_path or 'none'}; runtime: {frame['relative_path']}",
            )
        )
        line_ok = file_ok and hypothesis_line == frame["line"]
        checks.append(
            _check(
                "hypothesis_line_matches_runtime",
                "passed" if line_ok else "failed",
                f"Hypothesis line: {hypothesis_line}; runtime line: {frame['line']}",
            )
        )

    # A bare file name that matches the runtime frame resolves to the frame's full path.
    if frame is not None and _paths_match(hypothesis_path, frame["relative_path"]):
        hypothesis_path = frame["relative_path"]

    # 5. The cited location is covered by retrieved evidence (no uncited claims).
    if not hypothesis_path or not hypothesis_line:
        checks.append(_check("hypothesis_location_cited", "failed", "Hypothesis did not name a file and line."))
    else:
        cited = next(
            (
                chunk
                for chunk in retrieved
                if _paths_match(hypothesis_path, chunk["file_path"])
                and chunk["start_line"] <= hypothesis_line <= chunk["end_line"]
            ),
            None,
        )
        checks.append(
            _check(
                "hypothesis_location_cited",
                "passed" if cited else "failed",
                f"{hypothesis_path}:{hypothesis_line} within {cited['symbol']} ({cited['start_line']}-{cited['end_line']})"
                if cited
                else f"{hypothesis_path}:{hypothesis_line} is not inside any retrieved chunk",
            )
        )

    # 6. The source line at the cited location contains what the error names.
    identifiers = error_identifiers((stack_trace or {}).get("error_message"))
    line_text = (
        _source_line(repository_root, hypothesis_path, hypothesis_line) if hypothesis_path and hypothesis_line else None
    )
    if not identifiers:
        checks.append(_check("failing_line_matches_error", "skipped", "The error message names no identifier."))
    elif line_text is None:
        checks.append(_check("failing_line_matches_error", "failed", "The cited line does not exist in the checkout."))
    else:
        present = [identifier for identifier in identifiers if identifier in line_text]
        checks.append(
            _check(
                "failing_line_matches_error",
                "passed" if present else "failed",
                f"Line {hypothesis_line}: {line_text.strip()} (error names: {', '.join(identifiers)})",
            )
        )

    # 7. The root cause discusses something that is actually on the failing line (advisory).
    if line_text is None or not hypothesis.get("root_cause"):
        checks.append(_check("root_cause_references_line", "skipped", "No cited source line.", required=False))
    else:
        tokens = [
            token
            for token in dict.fromkeys(re.findall(r"[A-Za-z_$][\w$]{3,}", line_text))
            if token not in JS_KEYWORDS
        ]
        referenced = [token for token in tokens if token in hypothesis["root_cause"]]
        checks.append(
            _check(
                "root_cause_references_line",
                "passed" if referenced else "failed",
                f"Referenced: {', '.join(referenced) or 'none'} of {', '.join(tokens) or 'none'}",
                required=False,
            )
        )

    # 8. The introducing commit matches the commit git blame attributes the line to.
    suspect = (git_evidence or {}).get("suspect_commit")
    claimed_match = re.search(r"\b[0-9a-f]{7,40}\b", (hypothesis.get("introducing_commit") or "").lower())
    claimed = claimed_match.group(0) if claimed_match else ""
    if suspect is None:
        checks.append(_check("introducing_commit_matches_blame", "skipped", "git blame produced no commit."))
    else:
        matches = bool(claimed) and suspect["commit"].startswith(claimed)
        checks.append(
            _check(
                "introducing_commit_matches_blame",
                "passed" if matches else "failed",
                f"Hypothesis: {claimed[:12] or 'none'}; blame: {suspect['short']} ({suspect.get('summary')})",
            )
        )

    # 9. The named symbol exists in the index for that file (advisory).
    symbol = hypothesis.get("symbol")
    if not symbol or not hypothesis_path:
        checks.append(_check("symbol_exists_in_index", "skipped", "No symbol named.", required=False))
    else:
        exists = any(_paths_match(hypothesis_path, path) and indexed == symbol for path, indexed in indexed_symbols)
        checks.append(
            _check(
                "symbol_exists_in_index",
                "passed" if exists else "failed",
                f"{symbol} in {hypothesis_path}",
                required=False,
            )
        )

    required_failed = [check for check in checks if check["required"] and check["status"] == "failed"]
    return {"overall_result": "needs_review" if required_failed else "validated", "checks": checks}
