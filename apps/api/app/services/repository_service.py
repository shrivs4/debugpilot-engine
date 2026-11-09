"""Repository lifecycle: register, clone, sync, index.

Clone and sync run as FastAPI background tasks with their own DB session. The
repository row's `status` acts as a simple lock: a new sync is refused while a
clone, sync or index is in progress.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import db as database
from app.config import get_settings
from app.models import DebugRun, Repository, RepositorySync
from app.services import git_service
from app.services.indexer import rebuild_index
from app.services.ollama_client import OllamaClient


logger = logging.getLogger(__name__)

BUSY_STATUSES = {"pending", "cloning", "syncing", "indexing"}


class RepositoryConflictError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_repository(db: Session, url: str, branch: str | None) -> Repository:
    parsed = git_service.parse_github_url(url)
    branch = git_service.validate_branch_name(branch)

    repository = Repository(
        url=parsed.canonical_url,
        owner=parsed.owner,
        name=parsed.name,
        branch=branch,
        status="pending",
        status_message="Waiting to clone",
    )
    db.add(repository)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise RepositoryConflictError(
            f"{parsed.canonical_url} ({branch or 'default branch'}) is already registered."
        ) from error

    settings = get_settings()
    repository.local_path = str(git_service.repository_checkout_path(settings.repositories_dir, repository.id, parsed))
    db.commit()
    return repository


def begin_sync(db: Session, repository: Repository) -> None:
    if repository.status in BUSY_STATUSES:
        raise RepositoryConflictError(f"Repository is busy ({repository.status}); try again when it is ready.")
    if not repository.local_path or not Path(repository.local_path, ".git").is_dir():
        raise RepositoryConflictError("Repository has no local clone yet.")
    repository.status = "syncing"
    repository.status_message = "Fetching latest commit"
    db.commit()


def _set_status(db: Session, repository: Repository, status: str, message: str | None) -> None:
    repository.status = status
    repository.status_message = message
    db.commit()


def _index(db: Session, repository: Repository, sync: RepositorySync, ollama: OllamaClient) -> None:
    _set_status(db, repository, "indexing", "Chunking and embedding files")
    result = rebuild_index(db, repository, ollama, get_settings())
    sync.index_strategy = result.strategy
    sync.index_plan = result.plan.summary()
    sync.files_indexed = result.files_indexed
    sync.chunks_indexed = result.chunks_indexed


def _finish(db: Session, repository: Repository, sync: RepositorySync, error: Exception | None) -> None:
    sync.finished_at = _now()
    if error is None:
        sync.status = "completed"
        repository.status = "ready"
        repository.status_message = f"Indexed {sync.chunks_indexed} chunks from {sync.files_indexed} files"
    else:
        sync.status = "failed"
        sync.error = str(error)
        repository.status = "error"
        repository.status_message = str(error)
    db.commit()


def process_clone(repository_id: int, ollama: OllamaClient) -> None:
    settings = get_settings()
    with database.SessionLocal() as db:
        repository = db.get(Repository, repository_id)
        sync = RepositorySync(repository_id=repository.id, kind="clone", status="running")
        db.add(sync)
        _set_status(db, repository, "cloning", "Cloning repository")

        error: Exception | None = None
        try:
            parsed = git_service.parse_github_url(repository.url)
            path = Path(repository.local_path)
            git_service.clone(git_service.clone_source(parsed), path, repository.branch, settings.git_timeout_seconds)
            repository.branch = repository.branch or git_service.current_branch(path)
            repository.current_commit_sha = git_service.head_sha(path)
            repository.last_synced_at = _now()
            sync.new_commit_sha = repository.current_commit_sha
            sync.commit_changed = True
            db.commit()
            _index(db, repository, sync, ollama)
        except Exception as caught:  # noqa: BLE001 - recorded on the sync row and shown in the UI
            logger.exception("Clone failed for repository %s", repository_id)
            db.rollback()
            error = caught
        _finish(db, repository, sync, error)


def process_sync(repository_id: int, ollama: OllamaClient) -> None:
    settings = get_settings()
    with database.SessionLocal() as db:
        repository = db.get(Repository, repository_id)
        sync = RepositorySync(
            repository_id=repository.id,
            kind="sync",
            status="running",
            previous_commit_sha=repository.current_commit_sha,
        )
        db.add(sync)
        db.commit()

        error: Exception | None = None
        try:
            path = Path(repository.local_path)
            git_service.fetch_and_reset(path, repository.branch, settings.git_timeout_seconds)
            new_sha = git_service.head_sha(path)
            sync.new_commit_sha = new_sha
            sync.commit_changed = new_sha != sync.previous_commit_sha
            sync.changed_files = (
                git_service.changed_files(path, sync.previous_commit_sha, new_sha)
                if sync.commit_changed and sync.previous_commit_sha
                else []
            )
            repository.current_commit_sha = new_sha
            repository.last_synced_at = _now()
            db.commit()
            # Phase 1: always rebuild. sync.changed_files and the index plan are
            # recorded so Phase 2 can switch to incremental indexing here.
            _index(db, repository, sync, ollama)
        except Exception as caught:  # noqa: BLE001
            logger.exception("Sync failed for repository %s", repository_id)
            db.rollback()
            error = caught
        _finish(db, repository, sync, error)


def recover_interrupted_work() -> None:
    """Background work does not survive an API restart; mark it failed instead of leaving it stuck."""
    message = "Interrupted by an API restart; run it again."
    with database.SessionLocal() as db:
        db.execute(
            update(Repository)
            .where(Repository.status.in_(BUSY_STATUSES - {"pending"}))
            .values(status="error", status_message=message)
        )
        db.execute(
            update(Repository)
            .where(Repository.status == "pending", Repository.current_commit_sha.is_(None))
            .values(status="error", status_message=message)
        )
        db.execute(
            update(RepositorySync)
            .where(RepositorySync.status == "running")
            .values(status="failed", error=message, finished_at=_now())
        )
        db.execute(
            update(DebugRun)
            .where(DebugRun.status.in_(["queued", "running"]))
            .values(status="failed", error=message, finished_at=_now())
        )
        db.commit()


def latest_sync(db: Session, repository_id: int) -> RepositorySync | None:
    return db.scalars(
        select(RepositorySync)
        .where(RepositorySync.repository_id == repository_id)
        .order_by(RepositorySync.started_at.desc(), RepositorySync.id.desc())
        .limit(1)
    ).first()
