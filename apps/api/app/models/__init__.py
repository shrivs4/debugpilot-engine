"""ORM models. These mirror db/init/001_schema.sql, which owns the DDL."""

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


# Must match the vector(...) column in db/init/001_schema.sql.
EMBEDDING_DIMENSIONS = 768


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    status_message: Mapped[str | None] = mapped_column(Text)
    current_commit_sha: Mapped[str | None] = mapped_column(Text)
    indexed_commit_sha: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    syncs: Mapped[list["RepositorySync"]] = relationship(
        back_populates="repository", order_by="RepositorySync.started_at.desc()"
    )


class RepositorySync(Base):
    __tablename__ = "repository_syncs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="running")
    previous_commit_sha: Mapped[str | None] = mapped_column(Text)
    new_commit_sha: Mapped[str | None] = mapped_column(Text)
    commit_changed: Mapped[bool | None] = mapped_column(Boolean)
    changed_files: Mapped[list[str]] = mapped_column(JSONB, default=list)
    index_strategy: Mapped[str | None] = mapped_column(Text)
    index_plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    files_indexed: Mapped[int | None] = mapped_column(Integer)
    chunks_indexed: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repository: Mapped[Repository] = relationship(back_populates="syncs")


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    line_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chunks: Mapped[list["CodeChunk"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", passive_deletes=True
    )


class CodeChunk(Base):
    __tablename__ = "code_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    file_path: Mapped[str] = mapped_column(Text)
    chunk_type: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    embedding_model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    file: Mapped[File] = relationship(back_populates="chunks")


class DebugRun(Base):
    __tablename__ = "debug_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    issue_description: Mapped[str] = mapped_column(Text)
    setup_command: Mapped[str | None] = mapped_column(Text)
    setup_command_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis_command: Mapped[str] = mapped_column(Text)
    analysis_command_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="draft")
    current_step: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    overall_result: Mapped[str | None] = mapped_column(Text)
    context_package: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repository: Mapped[Repository] = relationship()


class EvidenceRecord(Base):
    __tablename__ = "evidence_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    debug_run_id: Mapped[int] = mapped_column(ForeignKey("debug_runs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    phase: Mapped[str | None] = mapped_column(Text)
    command: Mapped[str | None] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool | None] = mapped_column(Boolean)
    stdout: Mapped[str | None] = mapped_column(Text)
    stderr: Mapped[str | None] = mapped_column(Text)
    stack_trace: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Hypothesis(Base):
    __tablename__ = "hypotheses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    debug_run_id: Mapped[int] = mapped_column(ForeignKey("debug_runs.id", ondelete="CASCADE"))
    model: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    raw_response: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    root_cause: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)
    line_number: Mapped[int | None] = mapped_column(Integer)
    symbol: Mapped[str | None] = mapped_column(Text)
    introducing_commit: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    execution_path: Mapped[list[str]] = mapped_column(JSONB, default=list)
    missing_evidence: Mapped[list[str]] = mapped_column(JSONB, default=list)
    parse_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ValidationResult(Base):
    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    debug_run_id: Mapped[int] = mapped_column(ForeignKey("debug_runs.id", ondelete="CASCADE"))
    hypothesis_id: Mapped[int] = mapped_column(ForeignKey("hypotheses.id", ondelete="CASCADE"))
    overall_result: Mapped[str] = mapped_column(Text)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TicketDraft(Base):
    __tablename__ = "ticket_drafts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    debug_run_id: Mapped[int] = mapped_column(ForeignKey("debug_runs.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(Text, default="mock-jira")
    ticket_key: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    root_cause: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)
    line_number: Mapped[int | None] = mapped_column(Integer)
    reproduction: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    validation_status: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
