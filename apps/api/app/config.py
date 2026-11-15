"""Application settings.

Every tunable value lives here and is read from the environment (or a local
`.env` file). Services receive values from `get_settings()` instead of
hardcoding URLs, model names or limits.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


API_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://debugpilot:debugpilot@localhost:5433/debugpilot"
    schema_sql_path: Path = API_ROOT / "db" / "init" / "001_schema.sql"

    ollama_base_url: str = "http://localhost:11434"
    embedding_model: str = "nomic-embed-text"
    generation_model: str = "qwen2.5-coder:7b"
    embedding_batch_size: int = 16
    embedding_max_chars: int = 6000
    generation_temperature: float = 0.1
    generation_num_ctx: int = 8192
    ollama_timeout_seconds: float = 600.0

    workspace_dir: Path = Path("/workspace")
    max_file_size_bytes: int = 200_000
    git_timeout_seconds: int = 300

    setup_command_timeout_seconds: int = 900
    analysis_command_timeout_seconds: int = 180
    max_output_chars: int = 200_000

    retrieval_top_k: int = 8
    retrieval_candidate_k: int = 40
    semantic_weight: float = 0.7
    lexical_weight: float = 0.3

    # Hybrid retrieval signal weights (normalized by their sum).
    hybrid_semantic_weight: float = 0.35
    hybrid_keyword_weight: float = 0.2
    hybrid_symbol_weight: float = 0.15
    hybrid_file_name_weight: float = 0.05
    hybrid_runtime_location_weight: float = 0.15
    hybrid_git_change_weight: float = 0.1
    recent_commits_for_retrieval: int = 10

    # Relationship expansion and context size.
    related_top_k: int = 6
    flow_top_k: int = 8
    flow_promote_score: float = 0.8
    flow_promote_max: int = 5
    relationship_max_depth: int = 2
    context_max_chars: int = 24_000

    max_commit_hypotheses: int = 3
    max_issue_hypotheses: int = 3

    cors_origins: str = "http://localhost:5173"
    export_debug_json: bool = False

    @property
    def repositories_dir(self) -> Path:
        return self.workspace_dir / "repos"

    @property
    def worktrees_dir(self) -> Path:
        return self.workspace_dir / "worktrees"

    @property
    def exports_dir(self) -> Path:
        return self.workspace_dir / "exports"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
