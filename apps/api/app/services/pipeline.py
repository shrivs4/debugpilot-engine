"""Debug run pipeline.

Steps (recorded in `debug_runs.current_step` so the dashboard can poll):

  index -> retrieve -> setup_command -> analysis_command -> git_evidence
        -> context -> hypothesis -> validation

Commands only run if their exact text was approved. The LLM output is stored
as text; nothing in this pipeline writes to the repository checkout.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db as database
from app.config import Settings, get_settings
from app.models import CodeChunk, DebugRun, EvidenceRecord, Hypothesis, Repository, ValidationResult
from app.services import git_service
from app.services.command_runner import CommandResult, command_environment, run_command
from app.services.context_builder import build_context_package
from app.services.git_evidence import collect_git_evidence
from app.services.hypothesis import generate_hypothesis
from app.services.indexer import embed_texts, rebuild_index
from app.services.ollama_client import OllamaClient
from app.services.retrieval import chunks_for_frames, merge_retrieved, retrieve_chunks
from app.services.stack_trace import parse_stack_trace
from app.services.validation import validate_hypothesis


logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _step(db: Session, run: DebugRun, step: str) -> None:
    run.current_step = step
    db.commit()


def record_runtime_evidence(db: Session, run: DebugRun, phase: str, result: CommandResult, stack_trace: dict | None) -> EvidenceRecord:
    record = EvidenceRecord(
        debug_run_id=run.id,
        kind="runtime",
        phase=phase,
        command=result.command,
        exit_code=result.exit_code,
        success=result.success,
        stdout=result.stdout,
        stderr=result.stderr,
        stack_trace=stack_trace,
        data={
            "argv": result.argv,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "error": result.error,
        },
    )
    db.add(record)
    db.commit()
    return record


def runtime_dict(phase: str, result: CommandResult) -> dict:
    return {"phase": phase, **result.to_dict()}


def _ensure_index(db: Session, repository: Repository, ollama: OllamaClient, settings: Settings) -> None:
    has_chunks = db.scalar(select(CodeChunk.id).where(CodeChunk.repository_id == repository.id).limit(1))
    if has_chunks and repository.indexed_commit_sha == repository.current_commit_sha:
        return
    rebuild_index(db, repository, ollama, settings)
    db.commit()


def _execute(db: Session, run: DebugRun, phase: str, command: str, cwd: Path, timeout: int, settings: Settings) -> tuple[CommandResult, dict | None]:
    result = run_command(command, cwd, timeout, settings.max_output_chars, command_environment(settings.workspace_dir))
    stack_trace = parse_stack_trace(result.stderr, cwd) or parse_stack_trace(result.stdout, cwd)
    record_runtime_evidence(db, run, phase, result, stack_trace)
    return result, stack_trace


def execute_pipeline(db: Session, run: DebugRun, ollama: OllamaClient, settings: Settings) -> None:
    repository = db.get(Repository, run.repository_id)
    root = Path(repository.local_path)
    run.commit_sha = git_service.head_sha(root)

    _step(db, run, "index")
    _ensure_index(db, repository, ollama, settings)

    _step(db, run, "retrieve")
    query_embedding = embed_texts(ollama, [run.issue_description], settings)[0]
    retrieved = retrieve_chunks(
        db,
        repository.id,
        run.issue_description,
        query_embedding,
        settings.retrieval_top_k,
        settings.retrieval_candidate_k,
        settings.semantic_weight,
        settings.lexical_weight,
    )

    runtime: list[dict] = []
    if run.setup_command:
        _step(db, run, "setup_command")
        setup_result, _ = _execute(db, run, "setup", run.setup_command, root, settings.setup_command_timeout_seconds, settings)
        runtime.append(runtime_dict("setup", setup_result))
        if not setup_result.success:
            raise PipelineError(
                f"Setup command failed (exit code {setup_result.exit_code}): {setup_result.error or 'see runtime logs'}"
            )

    _step(db, run, "analysis_command")
    analysis_result, stack_trace = _execute(
        db, run, "analysis", run.analysis_command, root, settings.analysis_command_timeout_seconds, settings
    )
    runtime.append(runtime_dict("analysis", analysis_result))

    # Chunks containing the failing frames are direct runtime evidence; always include them.
    frame_chunks = chunks_for_frames(
        db,
        repository.id,
        (stack_trace or {}).get("frames", []),
        run.issue_description,
        query_embedding,
        settings.semantic_weight,
        settings.lexical_weight,
    )
    retrieved_dicts = [chunk.to_dict() for chunk in merge_retrieved(retrieved, frame_chunks)]
    for rank, chunk in enumerate(retrieved_dicts, start=1):
        db.add(
            EvidenceRecord(
                debug_run_id=run.id,
                kind="code_chunk",
                data={key: value for key, value in chunk.items() if key != "content"} | {"rank": rank},
            )
        )
    db.commit()

    _step(db, run, "git_evidence")
    git_evidence = collect_git_evidence(root, run.commit_sha, stack_trace, retrieved_dicts)
    db.add(EvidenceRecord(debug_run_id=run.id, kind="git", data=git_evidence))
    db.commit()

    _step(db, run, "context")
    analysis_runtime = runtime_dict("analysis", analysis_result)
    package = build_context_package(run.issue_description, runtime, stack_trace, git_evidence, retrieved_dicts)
    run.context_package = package
    db.commit()
    if settings.export_debug_json:
        settings.exports_dir.mkdir(parents=True, exist_ok=True)
        (settings.exports_dir / f"debug-run-{run.id}-context.json").write_text(json.dumps(package, indent=2), encoding="utf-8")

    _step(db, run, "hypothesis")
    generated = generate_hypothesis(ollama, package["llm_prompt"], settings)
    fields = generated.fields
    hypothesis = Hypothesis(
        debug_run_id=run.id,
        model=generated.model,
        prompt=package["llm_prompt"],
        raw_response=generated.raw_response,
        summary=fields["summary"],
        root_cause=fields["root_cause"],
        file_path=fields["file_path"],
        line_number=fields["line"],
        symbol=fields["symbol"],
        introducing_commit=fields["introducing_commit"],
        confidence=fields["confidence"],
        execution_path=fields["execution_path"],
        missing_evidence=fields["missing_evidence"],
        parse_ok=generated.parse_ok,
        duration_ms=generated.duration_ms,
    )
    db.add(hypothesis)
    db.commit()

    _step(db, run, "validation")
    indexed_symbols = set(
        db.execute(select(CodeChunk.file_path, CodeChunk.symbol).where(CodeChunk.repository_id == repository.id)).all()
    )
    outcome = validate_hypothesis(
        fields, analysis_runtime, stack_trace, git_evidence, retrieved_dicts, root, indexed_symbols
    )
    db.add(
        ValidationResult(
            debug_run_id=run.id,
            hypothesis_id=hypothesis.id,
            overall_result=outcome["overall_result"],
            checks=outcome["checks"],
        )
    )
    run.overall_result = outcome["overall_result"]
    db.commit()


def run_debug_pipeline(run_id: int, ollama: OllamaClient) -> None:
    settings = get_settings()
    with database.SessionLocal() as db:
        run = db.get(DebugRun, run_id)
        run.status = "running"
        run.started_at = _now()
        db.commit()
        try:
            execute_pipeline(db, run, ollama, settings)
            run.status = "completed"
            run.current_step = "done"
        except Exception as error:  # noqa: BLE001 - stored on the run and shown in the UI
            logger.exception("Debug run %s failed", run_id)
            db.rollback()
            run = db.get(DebugRun, run_id)
            run.status = "failed"
            run.error = str(error)
        run.finished_at = _now()
        db.commit()
