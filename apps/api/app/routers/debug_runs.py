from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import DebugRun, EvidenceRecord, Hypothesis, Repository, TicketDraft, ValidationResult
from app.routers.repositories import repository_out
from app.schemas import (
    CommandApproval,
    DebugRunCreate,
    DebugRunOut,
    DebugRunReport,
    HypothesisOut,
    RuntimeEvidenceOut,
    TicketDraftOut,
    ValidationOut,
)
from app.services.command_runner import CommandValidationError, parse_command
from app.services.ollama_client import OllamaClient, OllamaError, get_ollama_client
from app.services.pipeline import run_debug_pipeline
from app.services.tickets import build_ticket_fields


router = APIRouter(prefix="/debug-runs", tags=["debug-runs"])


def _ready_to_run(run: DebugRun) -> bool:
    setup_ok = not run.setup_command or run.setup_command_approved_at is not None
    return run.status == "draft" and setup_ok and run.analysis_command_approved_at is not None


def run_out(run: DebugRun) -> DebugRunOut:
    output = DebugRunOut.model_validate(run)
    output.repository_name = run.repository.name if run.repository else None
    output.ready_to_run = _ready_to_run(run)
    return output


def _get(db: Session, run_id: int) -> DebugRun:
    run = db.get(DebugRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Debug run not found.")
    return run


def _validate_command(command: str, label: str) -> str:
    try:
        parse_command(command)
    except CommandValidationError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{label}: {error}") from error
    return command.strip()


@router.post("", response_model=DebugRunOut, status_code=status.HTTP_201_CREATED)
def create_debug_run(body: DebugRunCreate, db: Session = Depends(get_db)) -> DebugRunOut:
    repository = db.get(Repository, body.repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found.")

    setup_command = body.setup_command.strip() if body.setup_command and body.setup_command.strip() else None
    run = DebugRun(
        repository_id=repository.id,
        issue_description=body.issue_description.strip(),
        setup_command=_validate_command(setup_command, "Setup command") if setup_command else None,
        analysis_command=_validate_command(body.analysis_command, "Analysis command"),
        status="draft",
    )
    db.add(run)
    db.commit()
    return run_out(run)


@router.get("", response_model=list[DebugRunOut])
def list_debug_runs(repository_id: int | None = None, db: Session = Depends(get_db)) -> list[DebugRunOut]:
    query = select(DebugRun).order_by(DebugRun.created_at.desc(), DebugRun.id.desc())
    if repository_id is not None:
        query = query.where(DebugRun.repository_id == repository_id)
    return [run_out(run) for run in db.scalars(query).all()]


@router.get("/{run_id}", response_model=DebugRunOut)
def get_debug_run(run_id: int, db: Session = Depends(get_db)) -> DebugRunOut:
    return run_out(_get(db, run_id))


@router.post("/{run_id}/approve-command", response_model=DebugRunOut)
def approve_command(run_id: int, body: CommandApproval, db: Session = Depends(get_db)) -> DebugRunOut:
    run = _get(db, run_id)
    if run.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is {run.status}; commands can only be approved on a draft run.")

    expected = run.setup_command if body.kind == "setup" else run.analysis_command
    if expected is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This run has no setup command.")
    if body.command.strip() != expected:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The approved command text does not match the command stored on this run.",
        )

    now = datetime.now(timezone.utc)
    if body.kind == "setup":
        run.setup_command_approved_at = now
    else:
        run.analysis_command_approved_at = now
    db.commit()
    return run_out(run)


@router.post("/{run_id}/run", response_model=DebugRunOut, status_code=status.HTTP_202_ACCEPTED)
def start_debug_run(
    run_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    ollama: OllamaClient = Depends(get_ollama_client),
) -> DebugRunOut:
    run = _get(db, run_id)
    if run.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is already {run.status}. Create a new run to try again.")
    if run.setup_command and run.setup_command_approved_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The setup command has not been approved.")
    if run.analysis_command_approved_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The analysis command has not been approved.")
    if run.repository.status != "ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Repository is {run.repository.status}; wait until it is ready before running.",
        )

    settings = get_settings()
    try:
        ollama.ensure_models([settings.embedding_model, settings.generation_model])
    except OllamaError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error

    run.status = "queued"
    run.current_step = "queued"
    db.commit()
    background_tasks.add_task(run_debug_pipeline, run.id, ollama)
    return run_out(run)


def _latest(db: Session, model, run_id: int):
    return db.scalars(
        select(model).where(model.debug_run_id == run_id).order_by(model.created_at.desc(), model.id.desc()).limit(1)
    ).first()


@router.get("/{run_id}/report", response_model=DebugRunReport)
def get_report(run_id: int, db: Session = Depends(get_db)) -> DebugRunReport:
    run = _get(db, run_id)
    evidence = db.scalars(
        select(EvidenceRecord).where(EvidenceRecord.debug_run_id == run_id).order_by(EvidenceRecord.id)
    ).all()
    git = next((item for item in evidence if item.kind == "git"), None)
    hypothesis = _latest(db, Hypothesis, run_id)
    validation = _latest(db, ValidationResult, run_id)
    ticket = _latest(db, TicketDraft, run_id)

    return DebugRunReport(
        run=run_out(run),
        repository=repository_out(db, run.repository),
        runtime_evidence=[RuntimeEvidenceOut.model_validate(item) for item in evidence if item.kind == "runtime"],
        code_evidence=[item.data for item in evidence if item.kind == "code_chunk"],
        git_evidence=git.data if git else None,
        hypothesis=HypothesisOut.model_validate(hypothesis) if hypothesis else None,
        validation=ValidationOut.model_validate(validation) if validation else None,
        ticket_draft=TicketDraftOut.model_validate(ticket) if ticket else None,
    )


@router.post("/{run_id}/jira-draft", response_model=TicketDraftOut, status_code=status.HTTP_201_CREATED)
def create_jira_draft(run_id: int, db: Session = Depends(get_db)) -> TicketDraftOut:
    """Create (or regenerate) the mock Jira draft. Nothing is sent to Jira."""
    run = _get(db, run_id)
    hypothesis = _latest(db, Hypothesis, run_id)
    if run.status != "completed" or hypothesis is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A Jira draft needs a completed run with a hypothesis.")

    evidence = db.scalars(
        select(EvidenceRecord).where(EvidenceRecord.debug_run_id == run_id).order_by(EvidenceRecord.id)
    ).all()
    fields = build_ticket_fields(run, run.repository, hypothesis, _latest(db, ValidationResult, run_id), evidence)

    db.execute(delete(TicketDraft).where(TicketDraft.debug_run_id == run_id))
    ticket = TicketDraft(debug_run_id=run_id, ticket_key=f"MOCK-{run_id}", status="draft", **fields)
    db.add(ticket)
    db.commit()
    return TicketDraftOut.model_validate(ticket)
