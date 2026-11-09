"""Repository indexing into Postgres + pgvector.

Derived from poc/build_chunks.py and poc/embed_chunks.py (the embedding text
format is unchanged). Phase 1 always rebuilds the full index, but the change
plan (added / modified / deleted / unchanged by content hash) is computed and
recorded so Phase 2 can re-embed only what changed.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import EMBEDDING_DIMENSIONS, CodeChunk, File, Repository
from app.services.chunker import Chunk, chunk_file
from app.services.ollama_client import OllamaClient, OllamaError
from app.services.scanner import ScannedFile, scan_repository


@dataclass
class IndexPlan:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "added": len(self.added),
            "modified": len(self.modified),
            "deleted": len(self.deleted),
            "unchanged": len(self.unchanged),
        }


@dataclass
class IndexResult:
    strategy: str
    plan: IndexPlan
    files_indexed: int
    chunks_indexed: int


def build_embedding_text(chunk: Chunk, max_chars: int) -> str:
    text = (
        f"File: {chunk.file_path}\n"
        f"Symbol: {chunk.symbol}\n"
        f"Chunk type: {chunk.chunk_type}\n"
        f"Language: {chunk.language}\n\n"
        f"Code or documentation:\n"
        f"{chunk.content}"
    )
    return text[:max_chars]


def plan_index_changes(existing_hashes: dict[str, str], scanned: list[ScannedFile]) -> IndexPlan:
    plan = IndexPlan()
    scanned_paths = set()
    for scanned_file in scanned:
        scanned_paths.add(scanned_file.path)
        previous = existing_hashes.get(scanned_file.path)
        if previous is None:
            plan.added.append(scanned_file.path)
        elif previous != scanned_file.content_hash:
            plan.modified.append(scanned_file.path)
        else:
            plan.unchanged.append(scanned_file.path)
    plan.deleted = sorted(set(existing_hashes) - scanned_paths)
    return plan


def embed_texts(ollama: OllamaClient, texts: list[str], settings: Settings) -> list[list[float]]:
    embeddings: list[list[float]] = []
    for start in range(0, len(texts), settings.embedding_batch_size):
        batch = texts[start:start + settings.embedding_batch_size]
        embeddings.extend(ollama.embed(batch, settings.embedding_model))
    for embedding in embeddings:
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise OllamaError(
                f"Embedding model '{settings.embedding_model}' returned {len(embedding)} dimensions, "
                f"but the database column stores {EMBEDDING_DIMENSIONS}. Use nomic-embed-text or "
                "update the vector column in db/init/001_schema.sql."
            )
    return embeddings


def rebuild_index(
    db: Session,
    repository: Repository,
    ollama: OllamaClient,
    settings: Settings,
) -> IndexResult:
    """Replace the repository's files and chunks. Embeds first so a failure keeps the old index."""
    root = Path(repository.local_path)
    scanned = scan_repository(root, settings.max_file_size_bytes)

    existing_hashes = dict(
        db.execute(select(File.path, File.content_hash).where(File.repository_id == repository.id)).all()
    )
    plan = plan_index_changes(existing_hashes, scanned)

    chunks_by_file = [(scanned_file, chunk_file(scanned_file)) for scanned_file in scanned]
    all_chunks = [chunk for _, chunks in chunks_by_file for chunk in chunks]
    embeddings = embed_texts(
        ollama, [build_embedding_text(chunk, settings.embedding_max_chars) for chunk in all_chunks], settings
    )

    db.execute(delete(File).where(File.repository_id == repository.id))

    embedding_iter = iter(embeddings)
    now = datetime.now(timezone.utc)
    for scanned_file, chunks in chunks_by_file:
        file_row = File(
            repository_id=repository.id,
            path=scanned_file.path,
            language=scanned_file.language,
            size_bytes=scanned_file.size_bytes,
            line_count=scanned_file.line_count,
            content_hash=scanned_file.content_hash,
            commit_sha=repository.current_commit_sha,
            indexed_at=now,
        )
        db.add(file_row)
        db.flush()
        for chunk in chunks:
            db.add(
                CodeChunk(
                    repository_id=repository.id,
                    file_id=file_row.id,
                    file_path=chunk.file_path,
                    chunk_type=chunk.chunk_type,
                    symbol=chunk.symbol,
                    language=chunk.language,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    content=chunk.content,
                    content_hash=hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                    embedding=next(embedding_iter),
                    embedding_model=settings.embedding_model,
                )
            )

    repository.indexed_commit_sha = repository.current_commit_sha
    repository.last_indexed_at = now
    db.flush()

    return IndexResult(strategy="full_rebuild", plan=plan, files_indexed=len(scanned), chunks_indexed=len(all_chunks))
