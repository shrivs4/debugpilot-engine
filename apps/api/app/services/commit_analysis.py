"""Commit and commit-range regression analysis.

Steps (recorded in `debug_runs.current_step`):

  index -> commit_diff -> worktree -> setup_command -> analysis_command
        -> retrieve -> context -> hypothesis -> validation

The selected commit is checked out into a detached `git worktree` under
`<workspace>/worktrees/run-<id>` so approved commands measure the behavior *at
that commit* without touching the shared clone; the worktree is always removed
afterwards. Changed files are chunked at the target revision in memory (the
index reflects the branch tip), the chunks overlapping changed lines become
primary evidence, and the rest of the evidence comes from the same hybrid
retrieval and relationship expansion the issue pipeline uses.

If an approved analysis command succeeds at the commit without mismatches,
errors or a stack trace, the run ends as `no_bug_detected` and the model is
not asked for hypotheses. Without a command the analysis stays static.
"""

import hashlib
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import CodeChunk, DebugRun, EvidenceRecord, Repository, ValidationResult
from app.services import git_service
from app.services.chunker import chunk_file
from app.services.context_builder import build_commit_prompt
from app.services.git_service import GitCommandError
from app.services.hypothesis import generate_hypotheses
from app.services.hypothesis_selection import COMMIT_NO_BUG_MESSAGE, no_bug_conclusion, no_failure_observed
from app.services.indexer import embed_texts
from app.services.ollama_client import OllamaClient
from app.services.pipeline import (
    _ensure_index,
    _step,
    build_query,
    gather_code_evidence,
    hypothesis_row,
    persist_code_evidence,
    query_text,
    run_commands,
    update_structured,
)
from app.services.retrieval import hybrid_weights, score_chunk
from app.services.scanner import ScannedFile, detect_language
from app.services.validation import validate_hypothesis


MAX_DIFF_CHARS = 20_000
MAX_CHANGED_PRIMARY = 8
RESULT_ORDER = {"validated": 0, "needs_review": 1, "rejected": 2}


class CommitAnalysisError(ValueError):
    pass


def resolve_range(root: Path, commit_sha: str | None, base_sha: str | None, head_sha: str | None) -> tuple[str | None, str]:
    """(base, head) full hashes. A single commit is compared with its first parent (or the empty tree)."""
    try:
        if commit_sha:
            head = git_service.resolve_commit(root, commit_sha)
            return git_service.parent_of(root, head), head
        if not base_sha or not head_sha:
            raise CommitAnalysisError("Provide either commit_sha or both base_sha and head_sha.")
        base = git_service.resolve_commit(root, base_sha)
        head = git_service.resolve_commit(root, head_sha)
    except GitCommandError as error:
        raise CommitAnalysisError(f"Commit not found in the repository: {error}") from error
    if base == head:
        raise CommitAnalysisError("The base and head commits are the same.")
    if not git_service.is_ancestor(root, base, head):
        raise CommitAnalysisError("The base commit must be an ancestor of the head commit.")
    return base, head


def changed_chunks(
    root: Path, head: str, changed_files: list[dict], line_ranges: dict[str, list[tuple[int, int]]]
) -> tuple[list[dict], dict[str, list[str]]]:
    """Chunk changed files at the target revision. Returns (chunks, changed symbols per file).

    Chunks get negative ids: they are not rows in the index.
    """
    chunks: list[dict] = []
    symbols: dict[str, list[str]] = {}
    next_id = -1
    for item in changed_files:
        path = item["file_path"]
        language = detect_language(PurePosixPath(path))
        if item["status"] == "D" or language is None:
            continue
        content = git_service.file_at(root, head, path)
        if content is None:
            continue
        scanned = ScannedFile(
            path=path,
            language=language,
            size_bytes=len(content.encode("utf-8")),
            line_count=len(content.splitlines()),
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            content=content,
        )
        for chunk in chunk_file(scanned):
            touched = any(start <= chunk.end_line and chunk.start_line <= end for start, end in line_ranges.get(path, []))
            chunks.append(
                {
                    "id": next_id,
                    "file_path": chunk.file_path,
                    "symbol": chunk.symbol,
                    "chunk_type": chunk.chunk_type,
                    "language": chunk.language,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "content": chunk.content,
                    "changed": touched,
                }
            )
            next_id -= 1
            if touched:
                symbols.setdefault(path, []).append(chunk.symbol)
    return chunks, symbols


def _best(outcomes: list[str]) -> str:
    return min(outcomes, key=lambda result: RESULT_ORDER.get(result, 3)) if outcomes else "needs_review"


def execute_commit_analysis(db: Session, run: DebugRun, ollama: OllamaClient, settings: Settings) -> None:
    repository = db.get(Repository, run.repository_id)
    root = Path(repository.local_path)
    run.commit_sha = git_service.head_sha(root)
    base, head = run.base_commit, run.target_commit
    notes = (run.commit_analysis or {}).get("notes")

    _step(db, run, "index")
    _ensure_index(db, repository, ollama, settings)

    _step(db, run, "commit_diff")
    commits = git_service.range_commits(root, base, head)
    changed_files = git_service.range_changed_files(root, base, head)
    line_ranges = git_service.changed_line_ranges(root, base, head)
    diff = git_service.range_diff(root, base, head)
    target_chunks, changed_symbols = changed_chunks(root, head, changed_files, line_ranges)
    for item in changed_files:
        item["symbols"] = changed_symbols.get(item["file_path"], [])
        item["line_ranges"] = line_ranges.get(item["file_path"], [])
    commit_context = {
        "base": base,
        "head": head,
        "commits": commits,
        "changed_files": changed_files,
        "diff": diff[:MAX_DIFF_CHARS],
        "notes": notes,
    }
    run.commit_analysis = {key: value for key, value in commit_context.items() if key != "diff"} | {
        "diff_truncated": len(diff) > MAX_DIFF_CHARS
    }
    db.commit()

    worktree = settings.worktrees_dir / f"run-{run.id}"
    _step(db, run, "worktree")
    git_service.add_worktree(root, worktree, head)
    try:
        runtime, analysis_runtime, stack_trace, structured, analysis_record = run_commands(db, run, worktree, settings)

        # The approved analysis command ran at this commit and nothing failed: report no regression
        # instead of asking the model to speculate. (Without a command, static analysis continues.)
        if run.analysis_command and no_failure_observed(analysis_runtime, structured, stack_trace):
            run.context_package = {
                "conclusion": no_bug_conclusion(analysis_runtime, COMMIT_NO_BUG_MESSAGE),
                "commit": run.commit_analysis,
                "runtime_evidence": runtime,
            }
            run.overall_result = "no_bug_detected"
            db.commit()
            return

        _step(db, run, "retrieve")
        base_text = "\n".join(
            [*(commit["subject"] for commit in commits), *(symbol for symbols in changed_symbols.values() for symbol in symbols)]
            + ([notes] if notes else [])
        )
        query_embedding = embed_texts(ollama, [query_text(base_text, structured, stack_trace)], settings)[0]
        query = build_query(
            base_text,
            structured,
            stack_trace,
            (analysis_runtime or {}).get("argv"),
            worktree,
            {item["file_path"]: 1.0 for item in changed_files},
            line_ranges,
        )
        terms, weights = query.terms(), hybrid_weights(settings)
        pinned = sorted(
            (
                score_chunk(SimpleNamespace(**chunk), None, query, terms, weights).to_dict()
                for chunk in target_chunks
                if chunk["changed"]
            ),
            key=lambda chunk: chunk["combined_score"],
            reverse=True,
        )[:MAX_CHANGED_PRIMARY]
        for chunk in pinned:
            chunk["sources"].insert(0, "changed_in_commit")
        primary, related, graph, entry_ids = gather_code_evidence(
            db, repository, worktree, query, query_embedding, structured, settings,
            extra_chunks=target_chunks, pinned_primary=pinned,
        )
        update_structured(db, analysis_record, structured)
        persist_code_evidence(db, run, primary, related)

        git_evidence = {
            "head_commit": head,
            "head_summary": git_service.show_stat(root, head),
            "target": None,
            "blame": None,
            "blame_lines": [],
            "suspect_commit": (
                {"commit": commits[0]["sha"], "short": commits[0]["short"], "author": commits[0]["author"], "summary": commits[0]["subject"]}
                if len(commits) == 1 else None
            ),
            "suspect_commit_summary": None,
            "suspect_diff": diff[:MAX_DIFF_CHARS],
            "file_history": [
                {"commit": commit["sha"], "author": commit["author"], "date": commit["date"], "subject": commit["subject"]}
                for commit in commits
            ],
            "errors": [],
        }
        db.add(EvidenceRecord(debug_run_id=run.id, kind="git", data=git_evidence))
        db.commit()

        _step(db, run, "context")
        prompt = build_commit_prompt(
            commit_context, runtime, primary, related, settings.max_commit_hypotheses, settings.context_max_chars
        )
        run.context_package = {
            "query": base_text,
            "commit": run.commit_analysis,
            "runtime_evidence": runtime,
            "stack_trace": stack_trace,
            "primary_chunks": primary,
            "related_chunks": related,
            "llm_prompt": prompt,
        }
        db.commit()

        _step(db, run, "hypothesis")
        generated = generate_hypotheses(ollama, prompt, settings, settings.max_commit_hypotheses)
        rows = [
            hypothesis_row(run, generated.model, prompt, generated.raw_response, fields, generated.parse_ok, generated.duration_ms, rank)
            for rank, fields in enumerate(generated.items, start=1)
        ]
        db.add_all(rows)
        db.commit()

        _step(db, run, "validation")
        replaced = {chunk["file_path"] for chunk in target_chunks}
        indexed_symbols = {
            (path, symbol)
            for path, symbol in db.execute(
                select(CodeChunk.file_path, CodeChunk.symbol).where(CodeChunk.repository_id == repository.id)
            ).all()
            if path not in replaced
        } | {(chunk["file_path"], chunk["symbol"]) for chunk in target_chunks}
        outcomes = []
        for fields, row in zip(generated.items, rows):
            outcome = validate_hypothesis(
                fields,
                analysis_runtime,
                stack_trace,
                git_evidence,
                [*primary, *related],
                worktree,
                indexed_symbols,
                runtime_structured=structured,
                graph=graph,
                entry_chunk_ids=entry_ids,
                commit_range={"commits": commits},
            )
            outcomes.append(outcome["overall_result"])
            db.add(
                ValidationResult(
                    debug_run_id=run.id, hypothesis_id=row.id, overall_result=outcome["overall_result"], checks=outcome["checks"]
                )
            )
        run.overall_result = _best(outcomes)
        db.commit()
    finally:
        git_service.remove_worktree(root, worktree)
