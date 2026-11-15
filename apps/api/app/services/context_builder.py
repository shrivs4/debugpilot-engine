"""Structured context package and LLM prompt.

Derived from poc/build_context.py. The prompt keeps the POC's instructions
(evidence only, cite files/lines/commits, no fix, no false certainty) but asks
for a JSON object so the hypothesis can be validated field by field.

Phase 2 adds the structured runtime extraction (mismatches and their
analysis), why each chunk was retrieved, related evidence reached through
code relationships, a size budget, and a commit-review variant of the prompt.
"""

MAX_STDOUT_CHARS = 4_000
MAX_STDERR_CHARS = 6_000
MAX_DIFF_CHARS = 6_000
MAX_CHUNK_CHARS = 3_000


def _tail(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else f"... [truncated]\n{text[-limit:]}"


def _runtime_section(runtime: dict) -> str:
    return (
        f"Phase: {runtime['phase']}\n"
        f"Command: {runtime['command']}\n"
        f"Exit code: {runtime['exit_code']}\n"
        f"Success: {runtime['success']}\n"
        f"Standard output:\n{_tail(runtime.get('stdout'), MAX_STDOUT_CHARS)}\n"
        f"Error output and stack trace:\n{_tail(runtime.get('stderr'), MAX_STDERR_CHARS)}"
    )


def _git_section(git: dict) -> str:
    suspect = git.get("suspect_commit") or {}
    target = git.get("target") or {}
    return (
        f"Current commit: {git.get('head_commit')}\n"
        f"Blame target: {target.get('file_path')}:{target.get('line')} (from {target.get('source')})\n"
        f"Commit that last changed the target line: {suspect.get('commit')} - {suspect.get('summary')}\n\n"
        f"Line history (git blame):\n{git.get('blame') or 'unavailable'}\n"
        f"Commit summary:\n{git.get('suspect_commit_summary') or git.get('head_summary') or 'unavailable'}\n"
        f"Relevant diff:\n{_tail(git.get('suspect_diff'), MAX_DIFF_CHARS) or 'unavailable'}"
    )


def _structured_section(structured: dict | None) -> str:
    if not structured or not structured.get("extracted"):
        return "Structured extraction: no expected/actual values or structured fields were found in the output."
    lines = []
    mismatches = structured.get("mismatches") or []
    if mismatches:
        lines.append("Mismatched values (expected -> actual):")
        for item in mismatches:
            ratio = f", actual/expected = {item['ratio']}" if item.get("ratio") is not None else ""
            lines.append(f"- {item['label']}: expected {item['expected']}, actual {item['actual']}{ratio}")
    analysis = structured.get("mismatch_analysis") or {}
    if mismatches:
        lines.append(f"Root mismatched field(s): {', '.join(analysis.get('root_fields') or []) or 'unknown'}"
                     f" (derived from {analysis.get('method', 'output_order').replace('_', ' ')})")
        if analysis.get("propagated_fields"):
            lines.append(f"Likely propagated (downstream) fields: {', '.join(analysis['propagated_fields'])}")
        lines.extend(f"Note: {note}" for note in analysis.get("notes", []))
    if structured.get("errors"):
        lines.append("Error lines:")
        lines.extend(f"- {error}" for error in structured["errors"][:8])
    if structured.get("locations"):
        lines.append("Reported locations: " + ", ".join(
            f"{location['file_path']}:{location['line']}" for location in structured["locations"][:10]
        ))
    if structured.get("structured_logs"):
        lines.append(f"Structured log lines: {len(structured['structured_logs'])} (first: {structured['structured_logs'][0]['data']})")
    return "\n".join(lines) or "Structured extraction: nothing beyond the exit code."


def _chunk_section(index: int, chunk: dict) -> str:
    content = chunk["content"]
    if len(content) > MAX_CHUNK_CHARS:
        content = content[:MAX_CHUNK_CHARS] + "\n... [truncated]"
    numbered = "\n".join(
        f"{chunk['start_line'] + offset:>4} | {line}" for offset, line in enumerate(content.splitlines())
    )
    header = (
        f"Evidence {index}\n"
        f"File: {chunk['file_path']}\n"
        f"Symbol: {chunk['symbol']}\n"
        f"Lines: {chunk['start_line']}-{chunk['end_line']}\n"
        f"Retrieved via: {', '.join(chunk['sources'])}\n"
        f"Similarity score: {chunk['semantic_score']} (keyword score {chunk['lexical_score']})\n"
    )
    if chunk.get("relation"):
        header += f"Related through: {chunk['relation']['path']}\n"
    if chunk.get("reasons"):
        header += f"Combined score: {chunk['combined_score']}; why selected: " + "; ".join(
            reason["detail"] for reason in chunk["reasons"][:4]
        ) + "\n"
    return f"{header}\n{numbered}"


def _budgeted(chunks: list[dict], start_index: int, budget: int) -> tuple[list[str], int, int]:
    sections: list[str] = []
    used = 0
    for offset, chunk in enumerate(chunks):
        section = _chunk_section(start_index + offset, chunk)
        if sections and used + len(section) > budget:
            return sections, used, len(chunks) - offset
        sections.append(section)
        used += len(section)
    return sections, used, 0


def _code_sections(retrieved: list[dict], related: list[dict], max_chars: int) -> str:
    primary_sections, used, primary_omitted = _budgeted(retrieved, 1, int(max_chars * 0.7))
    related_sections, _, related_omitted = _budgeted(related, len(primary_sections) + 1, max_chars - used)
    text = "\n\n".join(primary_sections) or "No source evidence retrieved."
    if primary_omitted:
        text += f"\n\n[{primary_omitted} lower-ranked primary chunks omitted for length]"
    if related:
        text += "\n\n=== Related evidence (code connected to the primary evidence) ===\n" + (
            "\n\n".join(related_sections) or "omitted for length"
        )
        if related_omitted:
            text += f"\n\n[{related_omitted} related chunks omitted for length]"
    return text


def _retrieval_summary(retrieved: list[dict], related: list[dict]) -> str:
    rows = []
    for chunk in [*retrieved, *related]:
        why = (
            f"related: {chunk['relation']['type']} via {chunk['relation']['via']}" if chunk.get("relation")
            else ", ".join(reason["signal"] for reason in chunk.get("reasons", [])) or ", ".join(chunk["sources"])
        )
        rows.append(f"- {chunk['file_path']}:{chunk['start_line']} {chunk['symbol']} score={chunk['combined_score']} ({why})")
    return "\n".join(rows)


def _flow_section(retrieved: list[dict], related: list[dict]) -> str:
    """Execution/data-flow candidates, most upstream first (issue runs only)."""
    flow = sorted(
        (chunk for chunk in [*retrieved, *related] if "data_flow" in chunk["sources"]),
        key=lambda chunk: max((reason["score"] for reason in chunk.get("reasons", []) if reason["signal"] == "data_flow"), default=0),
        reverse=True,
    )
    if not flow:
        return ""
    rows = []
    for chunk in flow:
        details = [reason["detail"] for reason in chunk.get("reasons", []) if reason["signal"] == "data_flow"]
        rows.append(f"- {chunk['file_path']}:{chunk['start_line']} {chunk['symbol']}: " + "; ".join(details))
    return (
        "\n=== Execution and data-flow analysis (most upstream first) ===\n"
        "Code reached from the reproduction entry point and code that produces the mismatched values. "
        "Upstream writers of root fields come before downstream symptoms.\n" + "\n".join(rows) + "\n"
    )


RESPONSE_FIELDS = """- "summary": one sentence describing the failure.
- "root_cause": the most likely root-cause hypothesis.
- "file_path": repository-relative path of the file responsible (as shown in the evidence).
- "line": the exact line number responsible (integer).
- "symbol": the function or symbol containing that line.
- "execution_path": ordered list of "file:line function" steps that lead to the failure.
- "introducing_commit": the full hash of the Git commit that introduced or changed the behavior, or "" if unknown.
- "confidence": "low", "medium", or "high".
- "missing_evidence": list of evidence still needed to confirm the hypothesis.
- "affected_fields": the mismatched output fields (if any) this root cause explains."""


def _observed_section(runtime: list[dict], stack_trace: dict | None) -> str:
    """What the reproduction actually showed, stated first and treated as authoritative."""
    analysis = next((item for item in runtime if item.get("phase") == "analysis"), None)
    if analysis is None:
        return "No reproduction was run."
    structured = analysis.get("structured") or {}
    lines = [f"Reproduction `{analysis.get('command')}` exited with code {analysis.get('exit_code')}."]
    mismatches = structured.get("mismatches") or []
    if mismatches:
        mismatch_analysis = structured.get("mismatch_analysis") or {}
        roots = mismatch_analysis.get("root_fields") or [mismatches[0]["field"]]
        propagated = set(mismatch_analysis.get("propagated_fields") or [])
        for item in mismatches:
            role = "ROOT mismatch" if item["field"] in roots else (
                "downstream (follows from the root)" if item["field"] in propagated else "mismatch"
            )
            lines.append(f"- {item['label']}: expected {item['expected']}, actual {item['actual']} [{role}]")
    if stack_trace and stack_trace.get("error_type"):
        lines.append(f"- Error: {stack_trace['error_type']}: {stack_trace.get('error_message') or ''}".rstrip())
        frame = stack_trace.get("top_repository_frame")
        if frame:
            lines.append(f"- Failing location: {frame['relative_path']}:{frame['line']} in {frame['function']}")
    elif structured.get("errors"):
        lines.extend(f"- Error line: {error}" for error in structured["errors"][:3])
    if len(lines) == 1:
        lines.append("- No mismatched values or errors were extracted from the output.")
    return "\n".join(lines)


def build_prompt(
    issue: str,
    runtime: list[dict],
    git: dict,
    retrieved: list[dict],
    related: list[dict] | None = None,
    max_chars: int = 24_000,
    stack_trace: dict | None = None,
    max_hypotheses: int = 3,
) -> str:
    runtime_text = "\n\n".join(
        _runtime_section(item) + "\n" + _structured_section(item.get("structured")) for item in runtime
    ) or "No runtime evidence."
    related = related or []

    return f"""You are debugging a TypeScript/JavaScript codebase.

Debugging question:
{issue}

=== Observed behavior (authoritative) ===
{_observed_section(runtime, stack_trace)}

The observed behavior above is what actually happened; every hypothesis must
explain it. Use only the source-code, runtime, and Git evidence below. Source
lines are prefixed with their real line numbers. The primary evidence and the
execution/data-flow analysis are the strongest code evidence; related and
lower-ranked chunks are background only.

=== Runtime evidence ===
{runtime_text}

=== Git history evidence ===
{_git_section(git)}

=== Primary source-code evidence ===
{_code_sections(retrieved, related, max_chars)}

=== Retrieval summary (why each chunk was selected) ===
{_retrieval_summary(retrieved, related)}
{_flow_section(retrieved, related)}
Rules for every hypothesis:
1. Explain the ROOT mismatched field and its actual versus expected value (or the observed error).
2. Cite a file and line from the primary evidence or the execution/data-flow analysis.
3. Give the execution path from the reproduction to that line.
4. Explain how the data flows from that line to the observed value.
5. Do not introduce concepts (features, flags, legacy behavior, data types) that appear only in
   lower-ranked chunks or nowhere in the evidence.
When several output values are wrong, explain the most upstream one: a value
computed from another wrong value is a symptom, not the cause. Follow the
related evidence (callers, callees, code that writes the data being read) to
find where the wrong value first appears.

Respond with a single JSON object {{"hypotheses": [ ... ]}} containing up to {max_hypotheses}
hypotheses, most likely first, where each item has these fields:
{RESPONSE_FIELDS}

Cite only file paths, line numbers and commit hashes that appear in the evidence.
Do not propose a code fix. Do not claim certainty if the evidence is incomplete.
"""


def build_commit_prompt(
    commit_context: dict,
    runtime: list[dict],
    retrieved: list[dict],
    related: list[dict],
    max_hypotheses: int,
    max_chars: int = 24_000,
) -> str:
    commits = "\n".join(f"- {commit['sha']} {commit['subject']} ({commit['author']})" for commit in commit_context["commits"])
    changed = "\n".join(
        f"- {item['status']} {item['file_path']}" + (f" (symbols: {', '.join(item['symbols'])})" if item.get("symbols") else "")
        for item in commit_context["changed_files"]
    )
    runtime_text = "\n\n".join(
        _runtime_section(item) + "\n" + _structured_section(item.get("structured")) for item in runtime
    ) or "No command was run; reason from the diff and code only and keep confidence low."
    notes = f"\nReviewer notes: {commit_context['notes']}\n" if commit_context.get("notes") else ""

    return f"""You are reviewing Git commits of a TypeScript/JavaScript codebase for regressions.
{notes}
Commits under analysis (oldest base excluded):
{commits}

Changed files:
{changed}

=== Diff ===
{_tail(commit_context.get('diff'), MAX_DIFF_CHARS)}

=== Runtime evidence (commands run at the analyzed commit) ===
{runtime_text}

=== Changed and surrounding source-code evidence ===
{_code_sections(retrieved, related, max_chars)}

=== Retrieval summary (why each chunk was selected) ===
{_retrieval_summary(retrieved, related)}

Identify up to {max_hypotheses} regressions these commits could introduce, most likely first.
Respond with a single JSON object {{"hypotheses": [ ... ]}} where each item has:
{RESPONSE_FIELDS}

"introducing_commit" must be one of the commits listed above. If the evidence
shows no regression, return one hypothesis with confidence "low" that says so.
Cite only file paths, line numbers and commit hashes that appear in the evidence.
Do not propose a code fix. Do not claim certainty if the evidence is incomplete.
"""


def build_context_package(
    issue: str,
    runtime: list[dict],
    stack_trace: dict | None,
    git: dict,
    retrieved: list[dict],
    related: list[dict] | None = None,
    max_chars: int = 24_000,
    max_hypotheses: int = 3,
) -> dict:
    related = related or []
    return {
        "query": issue,
        "runtime_evidence": runtime,
        "stack_trace": stack_trace,
        "git_evidence": git,
        "retrieved_chunks": retrieved,
        "primary_chunks": retrieved,
        "related_chunks": related,
        "retrieval_reasons": [
            {
                "chunk_id": chunk["chunk_id"],
                "file_path": chunk["file_path"],
                "symbol": chunk["symbol"],
                "role": chunk.get("role", "primary"),
                "combined_score": chunk["combined_score"],
                "reasons": chunk.get("reasons", []),
                "relation": chunk.get("relation"),
            }
            for chunk in [*retrieved, *related]
        ],
        "llm_prompt": build_prompt(issue, runtime, git, retrieved, related, max_chars, stack_trace, max_hypotheses),
    }
