"""Shared fixtures.

Database tests use a separate `<name>_test` database on the same server
(created on demand) so they never touch application data. They are skipped
when Postgres is unreachable; run them with `make test-api`, which executes
inside the API container.
"""

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


_base_url = make_url(
    os.environ.get("DATABASE_URL", "postgresql+psycopg://debugpilot:debugpilot@localhost:5433/debugpilot")
)
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _base_url.set(
    database=f"{_base_url.database}_test"
).render_as_string(hide_password=False)
# Must happen before app modules import and build the engine.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def _create_test_database() -> str | None:
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
            ).scalar()
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{url.database}"'))
        return None
    except Exception as error:  # noqa: BLE001
        return str(error)
    finally:
        admin.dispose()


DATABASE_ERROR = _create_test_database()

TABLES = (
    "ticket_drafts, validation_results, hypotheses, evidence_records, debug_runs, "
    "code_chunks, files, repository_syncs, repositories"
)


@pytest.fixture(scope="session")
def database():
    if DATABASE_ERROR:
        pytest.skip(f"Postgres not available for tests: {DATABASE_ERROR}")
    from app.db import ensure_schema

    ensure_schema(retries=3)


@pytest.fixture
def db_session(database):
    from app.db import SessionLocal, engine

    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from app.config import get_settings

    path = tmp_path / "workspace"
    path.mkdir()
    monkeypatch.setattr(get_settings(), "workspace_dir", path)
    return path


class FakeOllama:
    """Deterministic stand-in: bag-of-words embeddings and a canned generation response."""

    base_url = "http://fake-ollama"

    def __init__(self) -> None:
        self.response: dict | str = {}
        self.prompts: list[str] = []
        self.available = True

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        vectors = []
        for value in texts:
            vector = [0.0] * 768
            for token in re.findall(r"[a-z0-9]+", value.lower()):
                vector[int(hashlib.md5(token.encode()).hexdigest(), 16) % 768] += 1.0
            norm = math.sqrt(sum(item * item for item in vector)) or 1.0
            vectors.append([item / norm for item in vector])
        return vectors

    def generate(self, prompt: str, model: str, response_format=None, options=None) -> dict:
        self.prompts.append(prompt)
        response = self.response if isinstance(self.response, str) else json.dumps(self.response)
        return {"response": response, "duration_ms": 1}

    def list_models(self) -> list[str]:
        return ["nomic-embed-text:latest", "qwen2.5-coder:7b"]

    def ensure_models(self, models: list[str]) -> None:
        from app.services.ollama_client import OllamaUnavailableError

        if not self.available:
            raise OllamaUnavailableError("Ollama is not reachable at http://fake-ollama.")

    def status(self, models: list[str]) -> dict:
        if not self.available:
            return {"ok": False, "base_url": self.base_url, "version": None, "models": {}, "error": "unreachable"}
        return {"ok": True, "base_url": self.base_url, "version": "test", "models": {m: True for m in models}, "error": None}


@pytest.fixture
def fake_ollama() -> FakeOllama:
    return FakeOllama()


@pytest.fixture
def client(db_session, workspace, fake_ollama):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.ollama_client import get_ollama_client

    app.dependency_overrides[get_ollama_client] = lambda: fake_ollama
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_files(path: Path, files: dict[str, str], message: str) -> str:
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def local_remote(tmp_path, monkeypatch):
    """A local git repository that stands in for GitHub; clone_source is redirected to it."""
    from app.services import git_service

    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-q", "-b", "main")
    monkeypatch.setattr(git_service, "clone_source", lambda _repository: str(remote))
    return remote
