"""Deterministic hypothesis validation.

Derived from poc/validate_hypothesis.py. The POC checks (command failed, the
runtime file exists, the hypothesis names the runtime location, the failing
line contains `discountType`, the hypothesis names the introducing commit) are
kept but generalized: the expected identifier comes from the error message and
the expected commit from `git blame`, not from hardcoded fixture values.

Phase 2 adds checks that ask whether the hypothesis *explains the observed
behavior*, not just whether it points at something that exists:

  location_in_evidence               the suspect appears in the gathered evidence
  explains_observed_behavior         it names the mismatched fields / error
  execution_path_connects            the suspect is reachable from what actually ran
  affects_mismatched_field           the suspect can change the root mismatched value
  focused_reproduction               runtime evidence places execution in the suspect
  cited_file_exists                  (conflict) the cited file exists
  consistent_with_stronger_evidence  (conflict) it does not blame a value that only
                                     propagates an upstream mismatch

Each check is `passed`, `failed` or `skipped` and has a category
(`runtime`, `behavior`, `evidence`, `conflict`). The overall result is:

  rejected      a conflict check failed
  needs_review  a required check failed, the reproduction was not observed, or
                no behavior check passed (nothing confirms the claim at runtime)
  validated     everything required passed and runtime behavior supports it
"""

import re
from collections.abc import Callable
from pathlib import Path

from app.services.hypothesis_selection import is_strong
from app.services.code_graph import (
    FORWARD_EDGES,
    CodeGraph,
    field_influence,
    identifier_parts,
    name_matches_field,
    suspect_node,
)


JS_KEYWORDS = frozenset(
    "const let var function return if else for while new this typeof instanceof await async "
    "import export from class extends true false null undefined throw try catch".split()
)
EXECUTION_PATH_DEPTH = 4


def _check(name: str, status: str, details: str, required: bool = True, category: str = "evidence") -> dict:
    return {"name": name, "status": status, "details": details, "required": required, "category": category}


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


def _file_exists(repository_root: Path, relative_path: str) -> bool:
    absolute = (repository_root / relative_path).resolve()
    return repository_root.resolve() in absolute.parents and absolute.is_file()


def error_identifiers(error_message: str | None) -> list[str]:
    """Identifiers the runtime error names, e.g. `toUpperCase` in "(reading 'toUpperCase')"."""
    if not error_message:
        return []
    quoted = re.findall(r"['\"`]([A-Za-z_$][\w$]*)['\"`]", error_message)
    return list(dict.fromkeys(quoted))


def _text_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for word in re.findall(r"[A-Za-z_$][\w$]*", text):
        tokens.add(word.lower())
        tokens.update(identifier_parts(word))
    return tokens


def _resolve_path(path: str | None, repository_root: Path, known_paths: set[str]) -> str | None:
    """A cited path as it exists in the repository, accepting bare file names that match one indexed file."""
    if not path:
        return None
    if _file_exists(repository_root, path):
        return path
    matches = [known for known in known_paths if _paths_match(known, path)]
    return matches[0] if len(matches) == 1 else None


def _overall(checks: list[dict]) -> str:
    if any(check["category"] == "conflict" and check["status"] == "failed" for check in checks):
        return "rejected"
    if any(check["required"] and check["status"] == "failed" for check in checks):
        return "needs_review"
    reproduced = any(check["name"] == "reproduction_failed" and check["status"] == "passed" for check in checks)
    supported = any(check["category"] == "behavior" and check["status"] == "passed" for check in checks)
    return "validated" if reproduced and supported else "needs_review"


def validate_hypothesis(
    hypothesis: dict,
    analysis_runtime: dict | None,
    stack_trace: dict | None,
    git_evidence: dict,
    retrieved: list[dict],
    repository_root: Path,
    indexed_symbols: set[tuple[str, str]],
    *,
    runtime_structured: dict | None = None,
    graph: CodeGraph | None = None,
    entry_chunk_ids: list[int] | None = None,
    blame_lookup: Callable[[str, int, int], list[dict]] | None = None,
    commit_range: dict | None = None,
) -> dict:
    checks: list[dict] = []
    hypothesis_path = _normalize_path(hypothesis.get("file_path"), repository_root)
    hypothesis_line = hypothesis.get("line")
    frame = (stack_trace or {}).get("top_repository_frame")
    structured = runtime_structured or {}
    mismatches = structured.get("mismatches") or []

    # 1. The reproduction command must actually reproduce a failure (a non-zero exit, a timeout,
    #    or output that reports mismatched values or errors). Necessary, never sufficient.
    if analysis_runtime is None:
        if commit_range is not None:
            checks.append(_check("reproduction_failed", "skipped", "No analysis command was run for this commit analysis.", category="runtime"))
        else:
            checks.append(_check("reproduction_failed", "failed", "No analysis command result was captured.", category="runtime"))
    else:
        failed = analysis_runtime.get("exit_code") not in (0, None) or bool(analysis_runtime.get("timed_out"))
        observed = bool(mismatches) or bool(structured.get("errors"))
        details = f"Exit code: {analysis_runtime.get('exit_code')}" + (" (timed out)" if analysis_runtime.get("timed_out") else "")
        if mismatches:
            details += f"; {len(mismatches)} mismatched value(s) reported"
        checks.append(_check("reproduction_failed", "passed" if failed or observed else "failed", details, category="runtime"))

    # 2. The runtime-reported location exists in the checkout.
    if frame is None:
        checks.append(_check("runtime_location_exists", "skipped", "No in-repository stack frame was found.", category="runtime"))
    else:
        source = _source_line(repository_root, frame["relative_path"], frame["line"])
        checks.append(
            _check(
                "runtime_location_exists",
                "passed" if source is not None else "failed",
                f"{frame['relative_path']}:{frame['line']}:{frame['column']}",
                category="runtime",
            )
        )

    # 3 + 4. The hypothesis names the runtime file and line.
    if frame is None:
        checks.append(_check("hypothesis_file_matches_runtime", "skipped", "No in-repository stack frame.", category="behavior"))
        checks.append(_check("hypothesis_line_matches_runtime", "skipped", "No in-repository stack frame.", category="behavior"))
    else:
        file_ok = _paths_match(hypothesis_path, frame["relative_path"])
        checks.append(
            _check(
                "hypothesis_file_matches_runtime",
                "passed" if file_ok else "failed",
                f"Hypothesis: {hypothesis_path or 'none'}; runtime: {frame['relative_path']}",
                category="behavior",
            )
        )
        line_ok = file_ok and hypothesis_line == frame["line"]
        checks.append(
            _check(
                "hypothesis_line_matches_runtime",
                "passed" if line_ok else "failed",
                f"Hypothesis line: {hypothesis_line}; runtime line: {frame['line']}",
                category="behavior",
            )
        )

    # A bare file name that matches the runtime frame resolves to the frame's full path.
    if frame is not None and _paths_match(hypothesis_path, frame["relative_path"]):
        hypothesis_path = frame["relative_path"]

    known_paths = {path for path, _ in indexed_symbols} | {chunk["file_path"] for chunk in retrieved}
    resolved_path = _resolve_path(hypothesis_path, repository_root, known_paths)
    if resolved_path:
        hypothesis_path = resolved_path

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
        checks.append(_check("failing_line_matches_error", "skipped", "The error message names no identifier.", category="behavior"))
    elif line_text is None:
        checks.append(_check("failing_line_matches_error", "failed", "The cited line does not exist in the checkout.", category="behavior"))
    else:
        present = [identifier for identifier in identifiers if identifier in line_text]
        checks.append(
            _check(
                "failing_line_matches_error",
                "passed" if present else "failed",
                f"Line {hypothesis_line}: {line_text.strip()} (error names: {', '.join(identifiers)})",
                category="behavior",
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

    # 8. The introducing commit is consistent with Git history.
    claimed_match = re.search(r"\b[0-9a-f]{7,40}\b", (hypothesis.get("introducing_commit") or "").lower())
    claimed = claimed_match.group(0) if claimed_match else ""
    checks.append(_commit_check(claimed, frame, git_evidence, hypothesis_path, hypothesis_line, blame_lookup, commit_range))

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

    # ----- Phase 2: does the hypothesis explain the observed behavior? -----

    # 10. A cited file that does not exist contradicts the repository itself.
    if not hypothesis.get("file_path"):
        checks.append(_check("cited_file_exists", "skipped", "Hypothesis named no file.", category="conflict"))
    else:
        exists = resolved_path is not None
        checks.append(
            _check(
                "cited_file_exists",
                "passed" if exists else "failed",
                f"{hypothesis_path} exists in the checkout" if exists else f"{hypothesis.get('file_path')} is not a file in the repository",
                category="conflict",
            )
        )

    suspect = suspect_node(graph, hypothesis_path, hypothesis_line, symbol) if graph is not None else None
    suspect_range = (suspect.file_path, suspect.start_line, suspect.end_line) if suspect else _retrieved_range(
        retrieved, hypothesis_path, hypothesis_line, symbol
    )

    # 11. The suspect appears in the gathered evidence (retrieved, related or runtime).
    checks.append(_location_in_evidence(hypothesis_path, hypothesis_line, symbol, retrieved, stack_trace, structured))

    # 11b. ...and in the strong evidence, not only in lower-ranked semantic/keyword matches.
    grounded = _grounded_in_strong_evidence(hypothesis_path, hypothesis_line, symbol, retrieved, bool(mismatches))
    if grounded is not None:
        checks.append(grounded)

    # 12. The hypothesis mentions what was actually observed.
    explains = _explains_observed(hypothesis, mismatches, stack_trace, structured, identifiers, line_text)
    if explains is not None:
        checks.append(explains)

    # 13. The suspect is on the path the reproduction actually executed.
    checks.append(_execution_path(hypothesis, graph, suspect, suspect_range, stack_trace, entry_chunk_ids))

    analysis = structured.get("mismatch_analysis") or {}
    root_fields = analysis.get("root_fields") or [item["field"] for item in mismatches[:1]]
    propagated = set(analysis.get("propagated_fields") or [])

    # 14. The suspect can change the (root) mismatched value.
    if mismatches:
        checks.append(_affects_field(hypothesis, graph, suspect, root_fields, mismatches))

    # 15. Runtime evidence places execution inside the suspect (not required: often unavailable).
    checks.append(_focused_reproduction(suspect_range, stack_trace, structured))

    # 16. The hypothesis does not blame a value that only propagates an upstream mismatch.
    checks.append(_consistency(graph, suspect, root_fields, propagated, analysis, hypothesis, mismatches))

    return {"overall_result": _overall(checks), "checks": checks}


def _commit_check(claimed, frame, git_evidence, hypothesis_path, hypothesis_line, blame_lookup, commit_range) -> dict:
    if commit_range is not None:
        commits = commit_range.get("commits") or []
        if not claimed:
            return _check("introducing_commit_in_range", "failed", "Hypothesis named no commit from the analyzed range.")
        match = next((commit for commit in commits if commit["sha"].startswith(claimed)), None)
        return _check(
            "introducing_commit_in_range",
            "passed" if match else "failed",
            f"Hypothesis: {claimed[:12]}; range: {', '.join(commit['short'] for commit in commits) or 'none'}",
        )

    suspect = (git_evidence or {}).get("suspect_commit")
    if frame is None and blame_lookup is not None and hypothesis_path and hypothesis_line:
        # No stack frame: blame the cited location itself instead of whichever chunk ranked first.
        try:
            entries = blame_lookup(hypothesis_path, max(1, hypothesis_line - 2), hypothesis_line + 2)
        except Exception:  # noqa: BLE001 - blame of an invalid range is simply unavailable
            entries = []
        commits = {entry["commit"] for entry in entries if set(entry["commit"]) != {"0"}}
        if commits:
            matches = bool(claimed) and any(commit.startswith(claimed) for commit in commits)
            return _check(
                "introducing_commit_matches_blame",
                "passed" if matches else "failed",
                f"Hypothesis: {claimed[:12] or 'none'}; blame of {hypothesis_path}:{hypothesis_line}±2: "
                + ", ".join(sorted(commit[:8] for commit in commits)),
            )
    if suspect is None:
        return _check("introducing_commit_matches_blame", "skipped", "git blame produced no commit.")
    matches = bool(claimed) and suspect["commit"].startswith(claimed)
    return _check(
        "introducing_commit_matches_blame",
        "passed" if matches else "failed",
        f"Hypothesis: {claimed[:12] or 'none'}; blame: {suspect['short']} ({suspect.get('summary')})",
    )


def _retrieved_range(retrieved, path, line, symbol) -> tuple[str, int, int] | None:
    for chunk in retrieved:
        if _paths_match(path, chunk["file_path"]) and line and chunk["start_line"] <= line <= chunk["end_line"]:
            return chunk["file_path"], chunk["start_line"], chunk["end_line"]
    for chunk in retrieved:
        if symbol and chunk.get("symbol") == symbol and (not path or _paths_match(path, chunk["file_path"])):
            return chunk["file_path"], chunk["start_line"], chunk["end_line"]
    return None


def _location_in_evidence(path, line, symbol, retrieved, stack_trace, structured) -> dict:
    if not path and not symbol:
        return _check("location_in_evidence", "failed", "Hypothesis named neither a file nor a symbol.")
    for chunk in retrieved:
        role = chunk.get("role", "primary")
        if symbol and chunk.get("symbol") == symbol and (not path or _paths_match(path, chunk["file_path"])):
            return _check("location_in_evidence", "passed", f"{symbol} is in the {role} evidence ({chunk['file_path']}:{chunk['start_line']})")
        if path and line and _paths_match(path, chunk["file_path"]) and chunk["start_line"] <= line <= chunk["end_line"]:
            return _check("location_in_evidence", "passed", f"{path}:{line} is in the {role} evidence ({chunk['symbol']})")
    frames = (stack_trace or {}).get("frames", [])
    if symbol and any((frame.get("function") or "").split(".")[-1] == symbol for frame in frames):
        return _check("location_in_evidence", "passed", f"{symbol} appears on the stack trace")
    if path and any(_paths_match(path, location["file_path"]) for location in structured.get("locations", [])):
        return _check("location_in_evidence", "passed", f"{path} is reported in the command output")
    return _check(
        "location_in_evidence",
        "failed",
        f"{symbol or path} does not appear in the retrieved, related or runtime evidence",
    )


def _grounded_in_strong_evidence(path, line, symbol, retrieved, has_mismatches) -> dict | None:
    """Applies when the evidence carries strength annotations (stack frames or execution/data-flow kinds)."""
    if not any("stack_trace" in chunk.get("sources", []) or chunk.get("flow_kind") for chunk in retrieved):
        return None
    cited = [
        chunk for chunk in retrieved
        if (path and line and _paths_match(path, chunk["file_path"]) and chunk["start_line"] <= line <= chunk["end_line"])
        or (symbol and chunk.get("symbol") == symbol and (not path or _paths_match(path, chunk["file_path"])))
    ]
    strong = [chunk for chunk in cited if is_strong(chunk, has_mismatches)]
    if strong:
        chunk = strong[0]
        why = "on the failing stack trace" if "stack_trace" in chunk.get("sources", []) else (
            f"in the execution/data flow ({chunk['flow_kind'].replace('_', ' ')})"
        )
        return _check("grounded_in_strong_evidence", "passed", f"{chunk['symbol']} is {why}.")
    if cited:
        return _check(
            "grounded_in_strong_evidence",
            "failed",
            f"{cited[0]['symbol']} appears only in lower-ranked evidence, not on the failing stack or in the "
            "execution/data flow of the root mismatch.",
        )
    return _check("grounded_in_strong_evidence", "failed", f"{symbol or path} is not in the gathered evidence.")


def _explains_observed(hypothesis, mismatches, stack_trace, structured, identifiers, line_text) -> dict | None:
    text = " ".join(
        str(part) for part in (hypothesis.get("summary"), hypothesis.get("root_cause"), *(hypothesis.get("affected_fields") or [])) if part
    )
    tokens = _text_tokens(text)
    if mismatches:
        fields = [item["field"] for item in mismatches if any(name_matches_field(token, item["field"]) for token in tokens)
                  or item["field"].replace("_", " ") in text.lower()]
        values = [item["actual"] for item in mismatches if item["actual"] and re.search(rf"(?<![\w.]){re.escape(item['actual'])}(?![\w])", text)]
        mentioned = list(dict.fromkeys(fields)) + [f"value {value}" for value in values]
        return _check(
            "explains_observed_behavior",
            "passed" if mentioned else "failed",
            f"Mentions {', '.join(mentioned)}" if mentioned
            else f"Does not mention the mismatched fields ({', '.join(item['field'] for item in mismatches)}) or observed values",
            category="behavior",
        )
    error_type = (stack_trace or {}).get("error_type")
    if identifiers or error_type or structured.get("errors"):
        named = [identifier for identifier in identifiers if identifier in text]
        if error_type and error_type in text:
            named.append(error_type)
        if line_text and any(identifier in line_text for identifier in identifiers):
            named.append("the cited line contains the identifier the error names")
        return _check(
            "explains_observed_behavior",
            "passed" if named else "failed",
            f"Explains the error via: {', '.join(named)}" if named else "Does not reference the observed error",
            category="behavior",
        )
    return None


def _execution_path(hypothesis, graph, suspect, suspect_range, stack_trace, entry_chunk_ids) -> dict:
    frames = [frame for frame in (stack_trace or {}).get("frames", []) if frame.get("in_repository")]
    if suspect_range and any(
        _paths_match(frame["relative_path"], suspect_range[0]) and suspect_range[1] <= frame["line"] <= suspect_range[2]
        for frame in frames
    ):
        return _check("execution_path_connects", "passed", "The suspect is on the failing stack trace.")
    if graph is None or suspect is None:
        return _check("execution_path_connects", "skipped", "No code relationship graph or suspect chunk to trace.")

    entries = list(entry_chunk_ids or [])
    for frame in frames:
        node = graph.chunk_at(frame["relative_path"], frame["line"])
        if node is not None:
            entries.append(node.id)
    if entries:
        if suspect.id in entries:
            return _check("execution_path_connects", "passed", f"{suspect.symbol} is part of the executed entry point.")
        paths = graph.reachable(entries, EXECUTION_PATH_DEPTH, FORWARD_EDGES)
        if suspect.id in paths:
            source = next(entry for entry in entries if suspect.id in graph.reachable([entry], EXECUTION_PATH_DEPTH, FORWARD_EDGES))
            path = graph.reachable([source], EXECUTION_PATH_DEPTH, FORWARD_EDGES)[suspect.id]
            return _check("execution_path_connects", "passed", f"Reachable from the executed code: {graph.describe_path(source, path)}")
        return _check(
            "execution_path_connects",
            "failed",
            f"{suspect.symbol} is not reachable from the code the reproduction executed "
            f"(within {EXECUTION_PATH_DEPTH} calls/data dependencies).",
        )

    # No entry point known: check that consecutive steps of the claimed execution path are connected.
    steps = []
    for step in hypothesis.get("execution_path") or []:
        names = [name for name in re.findall(r"[A-Za-z_$][\w$]*", step) if name in graph.definitions]
        if names:
            steps.append(graph.definitions[names[-1]][0])
    if len(steps) < 2:
        return _check("execution_path_connects", "skipped", "No runtime entry point and too few resolvable execution-path steps.")
    broken = [
        (graph.nodes[left].symbol, graph.nodes[right].symbol)
        for left, right in zip(steps, steps[1:])
        if left != right and right not in graph.reachable([left], 2, tuple(graph_edge_types(graph)))
    ]
    return _check(
        "execution_path_connects",
        "failed" if broken else "passed",
        "Unconnected steps: " + ", ".join(f"{a} -> {b}" for a, b in broken) if broken else "Every execution-path step is connected in the code graph.",
    )


def graph_edge_types(graph: CodeGraph) -> set[str]:
    return {edge.type for edges in graph.edges.values() for edge in edges}


def _affects_field(hypothesis, graph, suspect, root_fields, mismatches) -> dict:
    if graph is not None and suspect is not None:
        for field_name in root_fields:
            influence = field_influence(graph, field_name)
            if suspect.id in influence:
                return _check("affects_mismatched_field", "passed", f"{influence[suspect.id]}", category="behavior")
        others = [item["field"] for item in mismatches if item["field"] not in root_fields]
        affected_other = [name for name in others if suspect.id in field_influence(graph, name)]
        detail = f"{suspect.symbol} cannot be shown to influence the root mismatched field(s) {', '.join(root_fields)}"
        if affected_other:
            detail += f"; it only influences {', '.join(affected_other)}"
        return _check("affects_mismatched_field", "failed", detail, category="behavior")

    claimed = set(hypothesis.get("affected_fields") or [])
    text_tokens = _text_tokens(f"{hypothesis.get('root_cause') or ''} {' '.join(claimed)}")
    covered = [name for name in root_fields if any(name_matches_field(token, name) for token in text_tokens)]
    return _check(
        "affects_mismatched_field",
        "passed" if covered else "failed",
        f"Hypothesis claims to explain {', '.join(covered)} (not verified against code relationships)"
        if covered else f"Hypothesis does not address the root mismatched field(s) {', '.join(root_fields)}",
        category="behavior",
    )


def _focused_reproduction(suspect_range, stack_trace, structured) -> dict:
    if suspect_range:
        path, start, end = suspect_range
        for frame in (stack_trace or {}).get("frames", []):
            if frame.get("in_repository") and _paths_match(frame["relative_path"], path) and start <= frame["line"] <= end:
                return _check(
                    "focused_reproduction", "passed",
                    f"Stack frame {frame['relative_path']}:{frame['line']} is inside the suspect ({start}-{end}).",
                    required=False, category="behavior",
                )
        for location in structured.get("locations", []):
            if _paths_match(location["file_path"], path) and start <= location["line"] <= end:
                return _check(
                    "focused_reproduction", "passed",
                    f"The command output reports {location['file_path']}:{location['line']} inside the suspect.",
                    required=False, category="behavior",
                )
    return _check(
        "focused_reproduction",
        "skipped",
        "No runtime evidence executes the suspect directly; a focused test of the suspect would confirm the claim.",
        required=False,
        category="behavior",
    )


def _consistency(graph, suspect, root_fields, propagated, analysis, hypothesis, mismatches) -> dict:
    if not mismatches:
        return _check("consistent_with_stronger_evidence", "passed", "No mismatch analysis contradicts the hypothesis.", category="conflict")
    if analysis.get("method") != "code_dependencies" or not propagated:
        return _check(
            "consistent_with_stronger_evidence", "skipped",
            "Mismatch dependencies could not be derived from the code; propagation was not assessed.",
            required=False, category="conflict",
        )
    if graph is not None and suspect is not None:
        if any(suspect.id in field_influence(graph, field_name) for field_name in root_fields):
            return _check(
                "consistent_with_stronger_evidence", "passed",
                f"{suspect.symbol} is upstream of the root mismatched field(s) {', '.join(root_fields)}.",
                category="conflict",
            )
        influenced = [item["field"] for item in mismatches if suspect.id in field_influence(graph, item["field"])]
        if influenced and set(influenced) <= propagated:
            upstream = sorted({dependency for name in influenced for dependency in analysis.get("dependencies", {}).get(name, [])})
            return _check(
                "consistent_with_stronger_evidence", "failed",
                f"{suspect.symbol} only affects {', '.join(influenced)}, but those deviations are explained by the upstream "
                f"mismatch in {', '.join(upstream) or ', '.join(root_fields)} (same deviation factor); "
                "the hypothesis blames a downstream symptom.",
                category="conflict",
            )
        return _check(
            "consistent_with_stronger_evidence", "passed",
            "No stronger evidence contradicts the hypothesis (its effect on the root field is checked separately).",
            category="conflict",
        )
    claimed = set(hypothesis.get("affected_fields") or [])
    if claimed and claimed <= propagated:
        return _check(
            "consistent_with_stronger_evidence", "failed",
            f"The hypothesis only explains {', '.join(sorted(claimed))}, which propagate from {', '.join(root_fields)}.",
            category="conflict",
        )
    return _check("consistent_with_stronger_evidence", "passed", "No stronger evidence contradicts the hypothesis.", category="conflict")
