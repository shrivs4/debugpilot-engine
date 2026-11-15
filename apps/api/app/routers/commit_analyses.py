from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import DebugRun, Repository
from app.routers.debug_runs import _validate_command, run_out
from app.schemas import CommitAnalysisCreate, DebugRunOut
from app.services.commit_analysis import CommitAnalysisError, resolve_range


router = APIRouter(prefix="/commit-analyses", tags=["commit-analyses"])


@router.post("", response_model=DebugRunOut, status_code=status.HTTP_201_CREATED)
def create_commit_analysis(body: CommitAnalysisCreate, db: Session = Depends(get_db)) -> DebugRunOut:
    """Create a draft commit-analysis run. Commands still need approval via /debug-runs/{id}/approve-command."""
    repository = db.get(Repository, body.repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found.")
    if repository.status != "ready" or not repository.local_path:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Repository is {repository.status}; wait until it is ready.")
    if body.commit_sha and (body.base_sha or body.head_sha):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide commit_sha or base_sha/head_sha, not both.")

    try:
        base, head = resolve_range(Path(repository.local_path), body.commit_sha, body.base_sha, body.head_sha)
    except CommitAnalysisError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error

    setup_command = (body.setup_command or "").strip() or None
    analysis_command = (body.analysis_command or "").strip() or None
    notes = (body.notes or "").strip() or None
    label = f"{base[:8]}..{head[:8]}" if body.base_sha else head[:8]
    run = DebugRun(
        repository_id=repository.id,
        run_type="commit_analysis",
        base_commit=base,
        target_commit=head,
        issue_description=notes or f"Regression analysis of commit {label}",
        setup_command=_validate_command(setup_command, "Setup command") if setup_command else None,
        analysis_command=_validate_command(analysis_command, "Analysis command") if analysis_command else None,
        commit_analysis={"notes": notes, "mode": "range" if body.base_sha else "commit"},
        status="draft",
    )
    db.add(run)
    db.commit()
    return run_out(run)


@router.get("", response_model=list[DebugRunOut])
def list_commit_analyses(repository_id: int | None = None, db: Session = Depends(get_db)) -> list[DebugRunOut]:
    query = (
        select(DebugRun)
        .where(DebugRun.run_type == "commit_analysis")
        .order_by(DebugRun.created_at.desc(), DebugRun.id.desc())
    )
    if repository_id is not None:
        query = query.where(DebugRun.repository_id == repository_id)
    return [run_out(run) for run in db.scalars(query).all()]
