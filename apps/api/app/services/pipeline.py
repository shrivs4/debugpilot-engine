"""Debug run pipeline.

Steps (recorded in `debug_runs.current_step` so the dashboard can poll):

  index -> setup_command -> analysis_command -> retrieve -> git_evidence
        -> context -> hypothesis -> validation

Retrieval runs after the commands so the runtime evidence (mismatched fields,
error identifiers, reported locations, the executed script) can steer it.
Commit-analysis runs (`run_type == "commit_analysis"`) are handled by
`app.services.commit_analysis`, which reuses the helpers below.

Commands only run if their exact text was approved. The LLM output is stored
as text; nothing in this pipeline writes to the repository checkout.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app import db as database
from app.config import Settings, get_settings
from app.models import CodeChunk, DebugRun, EvidenceRecord, Hypothesis, Repository, ValidationResult
from app.services import git_service
from app.services.code_graph import (
    CodeGraph,
    build_graph,
    entry_files_from_command,
    expand_related,
    field_dependencies,
    focus_properties,
    link_field_data_flow,
    load_chunks,
    trace_issue_flow,
)
from app.services.command_runner import CommandResult, command_environment, run_command
from app.services.context_builder import build_context_package
from app.services.git_evidence import collect_git_evidence
from app.services.git_service import GitCommandError
from app.services.hypothesis import generate_hypotheses
from app.services.hypothesis_selection import (
    EVIDENCE_MODEL,
    evidence_hypothesis,
    no_bug_conclusion,
    no_failure_observed,
    order_candidates,
)
from app.services.indexer import embed_texts, rebuild_index
from app.services.ollama_client import OllamaClient
from app.services.retrieval import RetrievalQuery, hybrid_retrieve
from app.services.runtime_extraction import extract_runtime_evidence
from app.services.stack_trace import parse_stack_trace
from app.services.validation import error_identifiers, validate_hypothesis


logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _step(db: Session, run: DebugRun, step: str) -> None:
    run.current_step = step
    db.commit()


def record_runtime_evidence(
    db: Session, run: DebugRun, phase: str, result: CommandResult, stack_trace: dict | None, structured: dict | None = None
) -> EvidenceRecord:
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
            "structured": structured,
        },
    )
    db.add(record)
    db.commit()
    return record


def runtime_dict(phase: str, result: CommandResult, structured: dict | None = None) -> dict:
    return {"phase": phase, **result.to_dict(), "structured": structured}


def _ensure_index(db: Session, repository: Repository, ollama: OllamaClient, settings: Settings) -> None:
    has_chunks = db.scalar(select(CodeChunk.id).where(CodeChunk.repository_id == repository.id).limit(1))
    if has_chunks and repository.indexed_commit_sha == repository.current_commit_sha:
        return
    rebuild_index(db, repository, ollama, settings)
    db.commit()


def execute_command(
    db: Session, run: DebugRun, phase: str, command: str, cwd: Path, timeout: int, settings: Settings
) -> tuple[CommandResult, dict | None, dict, EvidenceRecord]:
    result = run_command(command, cwd, timeout, settings.max_output_chars, command_environment(settings.workspace_dir))
    stack_trace = parse_stack_trace(result.stderr, cwd) or parse_stack_trace(result.stdout, cwd)
    structured = extract_runtime_evidence(result.stdout, result.stderr, result.exit_code, result.timed_out, stack_trace, cwd)
    record = record_runtime_evidence(db, run, phase, result, stack_trace, structured)
    return result, stack_trace, structured, record


def run_commands(
    db: Session, run: DebugRun, cwd: Path, settings: Settings
) -> tuple[list[dict], dict | None, dict | None, dict | None, EvidenceRecord | None]:
    """Setup (must succeed) then analysis. Returns runtime dicts, analysis runtime, stack trace, structured, record."""
    runtime: list[dict] = []
    if run.setup_command:
        _step(db, run, "setup_command")
        setup_result, _, setup_structured, _ = execute_command(
            db, run, "setup", run.setup_command, cwd, settings.setup_command_timeout_seconds, settings
        )
        runtime.append(runtime_dict("setup", setup_result, setup_structured))
        if not setup_result.success:
            raise PipelineError(
                f"Setup command failed (exit code {setup_result.exit_code}): {setup_result.error or 'see runtime logs'}"
            )
    if not run.analysis_command:
        return runtime, None, None, None, None

    _step(db, run, "analysis_command")
    result, stack_trace, structured, record = execute_command(
        db, run, "analysis", run.analysis_command, cwd, settings.analysis_command_timeout_seconds, settings
    )
    analysis_runtime = runtime_dict("analysis", result, structured)
    runtime.append(analysis_runtime)
    return runtime, analysis_runtime, stack_trace, structured, record


def query_text(base: str, structured: dict | None, stack_trace: dict | None) -> str:
    """The text embedded for semantic search: the question plus what the runtime reported."""
    parts = [base]
    if structured and structured.get("mismatches"):
        parts.append("Mismatched values: " + ", ".join(item["label"] for item in structured["mismatches"]))
    if stack_trace and stack_trace.get("error_message"):
        parts.append(f"{stack_trace.get('error_type') or 'Error'}: {stack_trace['error_message']}")
    return "\n".join(parts)


def build_query(
    text: str,
    structured: dict | None,
    stack_trace: dict | None,
    argv: list[str] | None,
    root: Path,
    changed_files: dict[str, float],
    changed_lines: dict[str, list[tuple[int, int]]] | None = None,
) -> RetrievalQuery:
    structured = structured or {}
    runtime_terms = [*structured.get("mismatched_fields", []), *error_identifiers((stack_trace or {}).get("error_message"))]
    frames = [frame for frame in (stack_trace or {}).get("frames", []) if frame.get("in_repository")]
    return RetrievalQuery(
        text=text,
        runtime_terms=runtime_terms,
        frame_functions=[frame["function"] for frame in frames if frame.get("function") != "<anonymous>"],
        locations=structured.get("locations", []),
        entry_files=entry_files_from_command(argv or [], root),
        changed_files=changed_files,
        changed_lines=changed_lines or {},
    )


def flow_dicts(graph: CodeGraph, flow: list[dict], rank_offset: int = 0) -> list[dict]:
    """Execution/data-flow candidates as related evidence; the reasons say why each was included."""
    items = []
    for rank, entry in enumerate(flow, start=1):
        node = graph.nodes[entry["chunk_id"]]
        items.append(
            {
                "chunk_id": node.id,
                "file_path": node.file_path,
                "symbol": node.symbol,
                "chunk_type": node.chunk_type,
                "language": node.language,
                "start_line": node.start_line,
                "end_line": node.end_line,
                "content": node.content,
                "semantic_score": 0.0,
                "lexical_score": 0.0,
                "combined_score": entry["score"],
                "matched_keywords": [],
                "sources": ["data_flow"],
                "signals": {"data_flow": entry["score"]},
                "reasons": [
                    {"signal": "data_flow", "score": entry["score"], "weight": None, "contribution": entry["score"], "detail": reason}
                    for reason in entry["reasons"]
                ],
                "role": "related",
                "relation": {
                    "type": "data_flow",
                    "via": "execution and data flow",
                    "depth": 0,
                    "from_chunk_id": None,
                    "from_symbol": None,
                    "from_file": None,
                    "path": "; ".join(entry["reasons"]),
                },
                "flow_rank": rank + rank_offset,
                "flow_kind": entry["kind"],
            }
        )
    return items


def related_dicts(graph: CodeGraph, related: list[dict]) -> list[dict]:
    items = []
    for entry in related:
        node = graph.nodes[entry["chunk_id"]]
        items.append(
            {
                "chunk_id": node.id,
                "file_path": node.file_path,
                "symbol": node.symbol,
                "chunk_type": node.chunk_type,
                "language": node.language,
                "start_line": node.start_line,
                "end_line": node.end_line,
                "content": node.content,
                "semantic_score": 0.0,
                "lexical_score": 0.0,
                "combined_score": entry["score"],
                "matched_keywords": [],
                "sources": ["relationship"],
                "signals": {"relationship": entry["score"]},
                "reasons": [
                    {
                        "signal": "relationship",
                        "score": entry["score"],
                        "weight": None,
                        "contribution": entry["score"],
                        "detail": f"{entry['relation']['type']} via {entry['relation']['via']}: {entry['relation']['path']}",
                    }
                ],
                "role": "related",
                "relation": entry["relation"],
            }
        )
    return items


def gather_code_evidence(
    db: Session,
    repository: Repository,
    root: Path,
    query: RetrievalQuery,
    query_embedding: list[float],
    structured: dict | None,
    settings: Settings,
    extra_chunks: list[dict] | None = None,
    pinned_primary: list[dict] | None = None,
    trace_flow: bool = False,
) -> tuple[list[dict], list[dict], CodeGraph, list[int]]:
    """Hybrid retrieval + relationship expansion. Returns (primary, related, graph, entry chunk ids).

    `extra_chunks` replace the indexed chunks of their files in the graph (used
    for files read at another revision); `pinned_primary` are always primary.
    `trace_flow` (issue runs) adds execution/data-flow analysis from the
    reproduction entry point: the code that produces the mismatched values,
    its upstream data writers first, with downstream symptoms ranked lower.
    """
    graph_chunks = load_chunks(db, repository.id)
    if extra_chunks:
        replaced = {chunk["file_path"] for chunk in extra_chunks}
        graph_chunks = [chunk for chunk in graph_chunks if chunk["file_path"] not in replaced] + extra_chunks
    graph = build_graph(graph_chunks, root, focus_properties(structured))
    if structured and structured.get("mismatches"):
        structured["mismatch_analysis"] = field_dependencies(
            graph, structured["mismatch_analysis"], structured["mismatches"]
        )

    retrieved = [chunk.to_dict() for chunk in hybrid_retrieve(db, repository.id, query, query_embedding, settings)]
    if extra_chunks:
        replaced = {chunk["file_path"] for chunk in extra_chunks}
        retrieved = [chunk for chunk in retrieved if chunk["file_path"] not in replaced]
    pinned = pinned_primary or []
    pinned_ids = {chunk["chunk_id"] for chunk in pinned}
    primary = pinned + [chunk for chunk in retrieved if chunk["chunk_id"] not in pinned_ids]
    primary = primary[: max(settings.retrieval_top_k, len(pinned))]

    entry_ids = [node.id for path in query.entry_files for node in graph.chunks_in_file(path)]
    flow: list[dict] = []
    if trace_flow:
        mismatches = (structured or {}).get("mismatches") or []
        if mismatches:
            link_field_data_flow(graph, [item["field"] for item in mismatches])
        # Rank the whole trace first; promotion happens before anything is truncated.
        full_flow = trace_issue_flow(graph, entry_ids, (structured or {}).get("mismatch_analysis"), mismatches, None)
        promoted = select_promoted(full_flow, settings.flow_promote_score, settings.flow_promote_max)
        _annotate_primary(primary, full_flow)
        primary = _promote_upstream(primary, flow_dicts(graph, promoted))
        promoted_ids = {item["chunk_id"] for item in promoted}
        flow = [item for item in full_flow if item["chunk_id"] not in promoted_ids][: settings.flow_top_k]

    expansion = related_dicts(
        graph, expand_related(graph, primary, settings.relationship_max_depth, settings.related_top_k)
    )
    primary_ids = {chunk["chunk_id"] for chunk in primary}
    flow_related = flow_dicts(graph, [item for item in flow if item["chunk_id"] not in primary_ids])
    flow_ids = {chunk["chunk_id"] for chunk in flow_related}
    related = flow_related + [chunk for chunk in expansion if chunk["chunk_id"] not in flow_ids]
    return primary, related, graph, entry_ids


PROMOTION_ORDER = {"upstream_writer": 0, "computes_root": 1, "feeds_root": 2, "executed": 3, "downstream": 4}


def select_promoted(flow: list[dict], threshold: float, limit: int) -> list[dict]:
    """Strong upstream candidates for the primary evidence, most upstream first.

    Upstream writers of a root field's inputs and code computing a root field
    qualify when the reproduction calls them directly; anything else needs
    `threshold`. Downstream-only code and test/fixture code are never promoted.
    """
    chosen = [
        item for item in flow
        if item["kind"] != "downstream"
        and not item["test"]
        and ((item["kind"] in {"upstream_writer", "computes_root"} and item["executed"]) or item["score"] >= threshold)
    ]
    chosen.sort(key=lambda item: (PROMOTION_ORDER[item["kind"]], -item["score"]))
    return chosen[:limit]


def _promote_upstream(primary: list[dict], flow_items: list[dict]) -> list[dict]:
    """Rank strong upstream candidates above downstream symptoms in the primary evidence.

    Order: chunks covering a stack frame (direct runtime evidence), then the
    promoted flow candidates in the given order, then the remaining
    hybrid-retrieval results (semantic and keyword matches never outrank them).
    """
    frames = [chunk for chunk in primary if "stack_trace" in chunk["sources"]]
    frame_ids = {chunk["chunk_id"] for chunk in frames}
    by_id = {chunk["chunk_id"]: chunk for chunk in primary}
    promoted = []
    for item in flow_items:
        if item["chunk_id"] in frame_ids:
            continue
        chunk = by_id.get(item["chunk_id"]) or (item | {"role": "primary", "relation": None})
        chunk["role"] = "primary"
        if "data_flow" not in chunk["sources"]:
            chunk["sources"].append("data_flow")
        promoted.append(chunk)
    promoted_ids = {chunk["chunk_id"] for chunk in promoted}
    rest = [chunk for chunk in primary if chunk["chunk_id"] not in frame_ids | promoted_ids]
    return frames + promoted + rest


def _annotate_primary(primary: list[dict], flow: list[dict]) -> None:
    """Record the data-flow reasons on primary chunks the flow analysis also selected."""
    by_id = {item["chunk_id"]: (rank, item) for rank, item in enumerate(flow, start=1)}
    for chunk in primary:
        if chunk["chunk_id"] in by_id:
            rank, item = by_id[chunk["chunk_id"]]
            chunk["flow_rank"] = rank
            chunk["flow_kind"] = item["kind"]
            chunk["sources"].append("data_flow")
            chunk["reasons"] = chunk.get("reasons", []) + [
                {"signal": "data_flow", "score": item["score"], "weight": None, "contribution": 0.0, "detail": reason}
                for reason in item["reasons"]
            ]


def persist_code_evidence(db: Session, run: DebugRun, primary: list[dict], related: list[dict]) -> None:
    for rank, chunk in enumerate([*primary, *related], start=1):
        db.add(
            EvidenceRecord(
                debug_run_id=run.id,
                kind="code_chunk",
                data={key: value for key, value in chunk.items() if key != "content"}
                | {"rank": rank, "role": chunk.get("role", "primary")},
            )
        )
    db.commit()


def update_structured(db: Session, record: EvidenceRecord | None, structured: dict | None) -> None:
    """Store the dependency-refined mismatch analysis back on the runtime evidence record."""
    if record is None or structured is None:
        return
    # The analysis dict was refined in place, so mark the JSON column dirty explicitly.
    record.data = {**record.data, "structured": structured}
    flag_modified(record, "data")
    db.commit()


def recent_changes(root: Path, settings: Settings) -> dict[str, float]:
    try:
        return git_service.recent_changed_files(root, settings.recent_commits_for_retrieval)
    except GitCommandError:
        return {}


def hypothesis_row(run: DebugRun, model: str, prompt: str, raw: str, fields: dict, parse_ok: bool, duration_ms, rank: int = 1) -> Hypothesis:
    return Hypothesis(
        debug_run_id=run.id,
        model=model,
        prompt=prompt,
        raw_response=raw,
        summary=fields["summary"],
        root_cause=fields["root_cause"],
        file_path=fields["file_path"],
        line_number=fields["line"],
        symbol=fields["symbol"],
        introducing_commit=fields["introducing_commit"],
        confidence=fields["confidence"],
        execution_path=fields["execution_path"],
        missing_evidence=fields["missing_evidence"],
        affected_fields=fields.get("affected_fields", []),
        rank=rank,
        parse_ok=parse_ok,
        duration_ms=duration_ms,
    )


def execute_pipeline(db: Session, run: DebugRun, ollama: OllamaClient, settings: Settings) -> None:
    repository = db.get(Repository, run.repository_id)
    root = Path(repository.local_path)
    run.commit_sha = git_service.head_sha(root)

    _step(db, run, "index")
    _ensure_index(db, repository, ollama, settings)

    runtime, analysis_runtime, stack_trace, structured, analysis_record = run_commands(db, run, root, settings)

    if no_failure_observed(analysis_runtime, structured, stack_trace):
        # Nothing failed: there is no behavior to explain, so no hypothesis is generated.
        run.context_package = {"conclusion": no_bug_conclusion(analysis_runtime), "runtime_evidence": runtime}
        run.overall_result = "no_bug_detected"
        db.commit()
        return

    _step(db, run, "retrieve")
    text = query_text(run.issue_description, structured, stack_trace)
    query_embedding = embed_texts(ollama, [text], settings)[0]
    query = build_query(
        run.issue_description, structured, stack_trace, (analysis_runtime or {}).get("argv"), root, recent_changes(root, settings)
    )
    primary, related, graph, entry_ids = gather_code_evidence(
        db, repository, root, query, query_embedding, structured, settings, trace_flow=True
    )
    update_structured(db, analysis_record, structured)
    persist_code_evidence(db, run, primary, related)

    _step(db, run, "git_evidence")
    git_evidence = collect_git_evidence(root, run.commit_sha, stack_trace, primary)
    db.add(EvidenceRecord(debug_run_id=run.id, kind="git", data=git_evidence))
    db.commit()

    _step(db, run, "context")
    package = build_context_package(
        run.issue_description, runtime, stack_trace, git_evidence, primary, related, settings.context_max_chars,
        settings.max_issue_hypotheses,
    )
    run.context_package = package
    db.commit()
    if settings.export_debug_json:
        settings.exports_dir.mkdir(parents=True, exist_ok=True)
        (settings.exports_dir / f"debug-run-{run.id}-context.json").write_text(json.dumps(package, indent=2), encoding="utf-8")

    _step(db, run, "hypothesis")
    generated = generate_hypotheses(ollama, package["llm_prompt"], settings, settings.max_issue_hypotheses)

    _step(db, run, "validation")
    indexed_symbols = set(
        db.execute(select(CodeChunk.file_path, CodeChunk.symbol).where(CodeChunk.repository_id == repository.id)).all()
    )
    evidence = [*primary, *related]
    has_mismatches = bool((structured or {}).get("mismatches"))

    def blame(path: str, start: int, end: int) -> list[dict]:
        return git_service.blame_porcelain(root, path, start, end)

    def assess(fields: dict, index: int, model: str, raw: str, parse_ok: bool) -> dict:
        outcome = validate_hypothesis(
            fields, analysis_runtime, stack_trace, git_evidence, evidence, root, indexed_symbols,
            runtime_structured=structured, graph=graph, entry_chunk_ids=entry_ids, blame_lookup=blame,
        )
        grounded = any(
            check["name"] == "grounded_in_strong_evidence" and check["status"] == "passed" for check in outcome["checks"]
        ) or not has_mismatches and not stack_trace
        return {"fields": fields, "outcome": outcome, "grounded": grounded, "index": index,
                "model": model, "raw": raw, "parse_ok": parse_ok}

    candidates = [
        assess(fields, index, generated.model, generated.raw_response, generated.parse_ok)
        for index, fields in enumerate(generated.items)
    ]
    explains_root = any(
        item["outcome"]["overall_result"] == "validated" for item in candidates
    )
    if has_mismatches and not explains_root:
        derived = evidence_hypothesis(graph, primary, structured, entry_ids, blame)
        if derived is not None:
            candidates.append(assess(derived, len(candidates), EVIDENCE_MODEL, json.dumps(derived), True))

    ordered = order_candidates(candidates)
    for rank, item in enumerate(ordered, start=1):
        hypothesis = hypothesis_row(
            run, item["model"], package["llm_prompt"], item["raw"], item["fields"], item["parse_ok"],
            generated.duration_ms if item["model"] != EVIDENCE_MODEL else None, rank,
        )
        db.add(hypothesis)
        db.flush()
        db.add(
            ValidationResult(
                debug_run_id=run.id,
                hypothesis_id=hypothesis.id,
                overall_result=item["outcome"]["overall_result"],
                checks=item["outcome"]["checks"],
            )
        )
    run.overall_result = ordered[0]["outcome"]["overall_result"] if ordered else "needs_review"
    db.commit()


def run_debug_pipeline(run_id: int, ollama: OllamaClient) -> None:
    from app.services.commit_analysis import execute_commit_analysis

    settings = get_settings()
    with database.SessionLocal() as db:
        run = db.get(DebugRun, run_id)
        run.status = "running"
        run.started_at = _now()
        db.commit()
        try:
            if run.run_type == "commit_analysis":
                execute_commit_analysis(db, run, ollama, settings)
            else:
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
