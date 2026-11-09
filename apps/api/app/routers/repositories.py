from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import CodeChunk, File, Repository
from app.schemas import RepositoryCreate, RepositoryOut, RepositorySyncOut
from app.services import repository_service
from app.services.git_service import BranchNameError, RepositoryUrlError
from app.services.ollama_client import OllamaClient, get_ollama_client
from app.services.repository_service import RepositoryConflictError


router = APIRouter(prefix="/repositories", tags=["repositories"])


def repository_out(db: Session, repository: Repository) -> RepositoryOut:
    output = RepositoryOut.model_validate(repository)
    output.file_count = db.scalar(select(func.count(File.id)).where(File.repository_id == repository.id)) or 0
    output.chunk_count = db.scalar(select(func.count(CodeChunk.id)).where(CodeChunk.repository_id == repository.id)) or 0
    latest = repository_service.latest_sync(db, repository.id)
    output.latest_sync = RepositorySyncOut.model_validate(latest) if latest else None
    return output


def _get(db: Session, repository_id: int) -> Repository:
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found.")
    return repository


@router.get("", response_model=list[RepositoryOut])
def list_repositories(db: Session = Depends(get_db)) -> list[RepositoryOut]:
    repositories = db.scalars(select(Repository).order_by(Repository.created_at.desc())).all()
    return [repository_out(db, repository) for repository in repositories]


@router.post("", response_model=RepositoryOut, status_code=status.HTTP_202_ACCEPTED)
def add_repository(
    body: RepositoryCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    ollama: OllamaClient = Depends(get_ollama_client),
) -> RepositoryOut:
    try:
        repository = repository_service.create_repository(db, body.url, body.branch)
    except (RepositoryUrlError, BranchNameError) as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    except RepositoryConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    background_tasks.add_task(repository_service.process_clone, repository.id, ollama)
    return repository_out(db, repository)


@router.get("/{repository_id}", response_model=RepositoryOut)
def get_repository(repository_id: int, db: Session = Depends(get_db)) -> RepositoryOut:
    return repository_out(db, _get(db, repository_id))


@router.get("/{repository_id}/syncs", response_model=list[RepositorySyncOut])
def list_syncs(repository_id: int, db: Session = Depends(get_db)) -> list[RepositorySyncOut]:
    return [RepositorySyncOut.model_validate(sync) for sync in _get(db, repository_id).syncs]


@router.post("/{repository_id}/sync", response_model=RepositoryOut, status_code=status.HTTP_202_ACCEPTED)
def sync_repository(
    repository_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    ollama: OllamaClient = Depends(get_ollama_client),
) -> RepositoryOut:
    repository = _get(db, repository_id)
    try:
        repository_service.begin_sync(db, repository)
    except RepositoryConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    background_tasks.add_task(repository_service.process_sync, repository.id, ollama)
    return repository_out(db, repository)
