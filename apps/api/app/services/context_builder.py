"""Structured context package and LLM prompt.

Derived from poc/build_context.py. The prompt keeps the POC's instructions
(evidence only, cite files/lines/commits, no fix, no false certainty) but asks
for a JSON object so the hypothesis can be validated field by field.
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


def _chunk_section(index: int, chunk: dict) -> str:
    content = chunk["content"]
    if len(content) > MAX_CHUNK_CHARS:
        content = content[:MAX_CHUNK_CHARS] + "\n... [truncated]"
    numbered = "\n".join(
        f"{chunk['start_line'] + offset:>4} | {line}" for offset, line in enumerate(content.splitlines())
    )
    return (
        f"Evidence {index}\n"
        f"File: {chunk['file_path']}\n"
        f"Symbol: {chunk['symbol']}\n"
        f"Lines: {chunk['start_line']}-{chunk['end_line']}\n"
        f"Retrieved via: {', '.join(chunk['sources'])}\n"
        f"Similarity score: {chunk['semantic_score']} (keyword score {chunk['lexical_score']})\n\n"
        f"{numbered}"
    )


def build_prompt(issue: str, runtime: list[dict], git: dict, retrieved: list[dict]) -> str:
    runtime_text = "\n\n".join(_runtime_section(item) for item in runtime) or "No runtime evidence."
    code_text = "\n\n".join(_chunk_section(index, chunk) for index, chunk in enumerate(retrieved, start=1))

    return f"""You are debugging a TypeScript/JavaScript codebase.

Debugging question:
{issue}

Use only the source-code, runtime, and Git evidence below. Source lines are
prefixed with their real line numbers.

=== Runtime evidence ===
{runtime_text}

=== Git history evidence ===
{_git_section(git)}

=== Source-code evidence ===
{code_text}

Respond with a single JSON object with these fields:
- "summary": one sentence describing the failure.
- "root_cause": the most likely root-cause hypothesis.
- "file_path": repository-relative path of the file responsible (as shown in the evidence).
- "line": the exact line number responsible (integer).
- "symbol": the function or symbol containing that line.
- "execution_path": ordered list of "file:line function" steps that lead to the failure.
- "introducing_commit": the full hash of the Git commit that introduced or changed the behavior, or "" if unknown.
- "confidence": "low", "medium", or "high".
- "missing_evidence": list of evidence still needed to confirm the hypothesis.

Cite only file paths, line numbers and commit hashes that appear in the evidence.
Do not propose a code fix. Do not claim certainty if the evidence is incomplete.
"""


def build_context_package(issue: str, runtime: list[dict], stack_trace: dict | None, git: dict, retrieved: list[dict]) -> dict:
    return {
        "query": issue,
        "runtime_evidence": runtime,
        "stack_trace": stack_trace,
        "git_evidence": git,
        "retrieved_chunks": retrieved,
        "llm_prompt": build_prompt(issue, runtime, git, retrieved),
    }
