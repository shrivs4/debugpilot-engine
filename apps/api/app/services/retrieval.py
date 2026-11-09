"""Hybrid retrieval: pgvector cosine similarity plus exact keyword matching.

Replaces the in-memory cosine loop in poc/search_chunks.py. Both scores are
returned alongside the combined score so the dashboard can show why a chunk
was retrieved.
"""

import re
from dataclasses import asdict, dataclass, field

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import CodeChunk


STOPWORDS = frozenset(
    """
    a an and are as at be because been but by can cannot could did does doing for from had has have
    how if in into is it its not of on or our should so such than that the their them then there these
    they this those to too was we were what when where which while who why will with would you your
    fail fails failed failing error errors issue bug broken works working when after before
    """.split()
)

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


@dataclass
class RetrievedChunk:
    chunk_id: int
    file_path: str
    symbol: str
    chunk_type: str
    language: str
    start_line: int
    end_line: int
    content: str
    semantic_score: float
    lexical_score: float
    combined_score: float
    matched_keywords: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def extract_keywords(text: str, limit: int = 20) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for match in IDENTIFIER.findall(text or ""):
        for token in [match, *match.split(".")] if "." in match else [match]:
            normalized = token.lower()
            if len(token) < 3 or normalized in STOPWORDS or normalized in seen:
                continue
            seen.add(normalized)
            keywords.append(token)
    return keywords[:limit]


def lexical_match(keywords: list[str], chunk: CodeChunk) -> tuple[float, list[str]]:
    if not keywords:
        return 0.0, []
    haystack = f"{chunk.file_path}\n{chunk.symbol}\n{chunk.content}".lower()
    matched = [keyword for keyword in keywords if keyword.lower() in haystack]
    return len(matched) / len(keywords), matched


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build(chunk: CodeChunk, distance: float, keywords: list[str], weights: tuple[float, float], source: str) -> RetrievedChunk:
    semantic = max(0.0, 1.0 - float(distance))
    lexical, matched = lexical_match(keywords, chunk)
    return RetrievedChunk(
        chunk_id=chunk.id,
        file_path=chunk.file_path,
        symbol=chunk.symbol,
        chunk_type=chunk.chunk_type,
        language=chunk.language,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        content=chunk.content,
        semantic_score=round(semantic, 4),
        lexical_score=round(lexical, 4),
        combined_score=round(weights[0] * semantic + weights[1] * lexical, 4),
        matched_keywords=matched,
        sources=[source],
    )


def retrieve_chunks(
    db: Session,
    repository_id: int,
    query_text: str,
    query_embedding: list[float],
    top_k: int,
    candidate_k: int,
    semantic_weight: float,
    lexical_weight: float,
) -> list[RetrievedChunk]:
    weights = (semantic_weight, lexical_weight)
    keywords = extract_keywords(query_text)
    distance = CodeChunk.embedding.cosine_distance(query_embedding).label("distance")

    candidates: dict[int, RetrievedChunk] = {}

    semantic_rows = db.execute(
        select(CodeChunk, distance)
        .where(CodeChunk.repository_id == repository_id)
        .order_by(distance)
        .limit(candidate_k)
    ).all()
    for chunk, chunk_distance in semantic_rows:
        candidates[chunk.id] = _build(chunk, chunk_distance, keywords, weights, "semantic")

    if keywords:
        keyword_filter = or_(
            *[CodeChunk.content.ilike(f"%{_escape_like(keyword)}%", escape="\\") for keyword in keywords],
            *[CodeChunk.symbol.ilike(f"%{_escape_like(keyword)}%", escape="\\") for keyword in keywords],
        )
        lexical_rows = db.execute(
            select(CodeChunk, distance)
            .where(and_(CodeChunk.repository_id == repository_id, keyword_filter))
            .limit(candidate_k)
        ).all()
        for chunk, chunk_distance in lexical_rows:
            if chunk.id in candidates:
                if "lexical" not in candidates[chunk.id].sources:
                    candidates[chunk.id].sources.append("lexical")
            else:
                candidates[chunk.id] = _build(chunk, chunk_distance, keywords, weights, "lexical")

    for candidate in candidates.values():
        if candidate.lexical_score > 0 and "lexical" not in candidate.sources:
            candidate.sources.append("lexical")

    ranked = sorted(candidates.values(), key=lambda item: item.combined_score, reverse=True)
    return ranked[:top_k]


def chunks_for_frames(
    db: Session,
    repository_id: int,
    frames: list[dict],
    query_text: str,
    query_embedding: list[float],
    semantic_weight: float,
    lexical_weight: float,
    max_frames: int = 5,
) -> list[RetrievedChunk]:
    """Chunks that contain in-repository stack frames, narrowest chunk per frame."""
    keywords = extract_keywords(query_text)
    distance = CodeChunk.embedding.cosine_distance(query_embedding).label("distance")
    results: list[RetrievedChunk] = []
    seen: set[int] = set()

    for frame in [frame for frame in frames if frame.get("in_repository")][:max_frames]:
        row = db.execute(
            select(CodeChunk, distance)
            .where(
                CodeChunk.repository_id == repository_id,
                CodeChunk.file_path == frame["relative_path"],
                CodeChunk.start_line <= frame["line"],
                CodeChunk.end_line >= frame["line"],
            )
            .order_by(CodeChunk.end_line - CodeChunk.start_line)
            .limit(1)
        ).first()
        if row is None or row[0].id in seen:
            continue
        seen.add(row[0].id)
        results.append(_build(row[0], row[1], keywords, (semantic_weight, lexical_weight), "stack_trace"))
    return results


def merge_retrieved(primary: list[RetrievedChunk], frame_chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Stack-trace chunks first (they are direct runtime evidence), then the ranked retrieval results."""
    by_id = {chunk.chunk_id: chunk for chunk in primary}
    merged: list[RetrievedChunk] = []
    for frame_chunk in frame_chunks:
        existing = by_id.pop(frame_chunk.chunk_id, None)
        if existing is not None:
            existing.sources.append("stack_trace")
            merged.append(existing)
        else:
            merged.append(frame_chunk)
    merged.extend(chunk for chunk in primary if chunk.chunk_id in by_id)
    return merged
