from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RepositoryCreate(BaseModel):
    url: str = Field(examples=["https://github.com/shrivs4/debugpilot-checkout-fixture"])
    branch: str | None = None


class RepositorySyncOut(ORMModel):
    id: int
    kind: str
    status: str
    previous_commit_sha: str | None
    new_commit_sha: str | None
    commit_changed: bool | None
    changed_files: list[str]
    index_strategy: str | None
    index_plan: dict[str, Any]
    files_indexed: int | None
    chunks_indexed: int | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class RepositoryOut(ORMModel):
    id: int
    url: str
    owner: str
    name: str
    branch: str | None
    status: str
    status_message: str | None
    current_commit_sha: str | None
    indexed_commit_sha: str | None
    last_synced_at: datetime | None
    last_indexed_at: datetime | None
    created_at: datetime
    file_count: int = 0
    chunk_count: int = 0
    latest_sync: RepositorySyncOut | None = None


class DebugRunCreate(BaseModel):
    repository_id: int
    issue_description: str = Field(min_length=3)
    setup_command: str | None = None
    analysis_command: str = Field(min_length=1)


class CommandApproval(BaseModel):
    kind: Literal["setup", "analysis"]
    # The exact command text the user saw and confirmed.
    command: str


class DebugRunOut(ORMModel):
    id: int
    repository_id: int
    repository_name: str | None = None
    issue_description: str
    setup_command: str | None
    setup_command_approved_at: datetime | None
    analysis_command: str
    analysis_command_approved_at: datetime | None
    status: str
    current_step: str | None
    error: str | None
    commit_sha: str | None
    overall_result: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    ready_to_run: bool = False


class RuntimeEvidenceOut(ORMModel):
    id: int
    phase: str | None
    command: str | None
    exit_code: int | None
    success: bool | None
    stdout: str | None
    stderr: str | None
    stack_trace: dict[str, Any] | None
    data: dict[str, Any]
    captured_at: datetime


class HypothesisOut(ORMModel):
    id: int
    model: str
    summary: str | None
    root_cause: str | None
    file_path: str | None
    line_number: int | None
    symbol: str | None
    introducing_commit: str | None
    confidence: str | None
    execution_path: list[str]
    missing_evidence: list[str]
    parse_ok: bool
    raw_response: str
    duration_ms: int | None
    created_at: datetime


class ValidationOut(ORMModel):
    id: int
    overall_result: str
    checks: list[dict[str, Any]]
    created_at: datetime


class TicketDraftOut(ORMModel):
    id: int
    debug_run_id: int
    provider: str
    ticket_key: str
    title: str
    summary: str
    root_cause: str | None
    file_path: str | None
    line_number: int | None
    reproduction: dict[str, Any]
    evidence: list[dict[str, Any]]
    validation_status: str | None
    status: str
    created_at: datetime


class DebugRunReport(BaseModel):
    run: DebugRunOut
    repository: RepositoryOut
    runtime_evidence: list[RuntimeEvidenceOut]
    code_evidence: list[dict[str, Any]]
    git_evidence: dict[str, Any] | None
    hypothesis: HypothesisOut | None
    validation: ValidationOut | None
    ticket_draft: TicketDraftOut | None
