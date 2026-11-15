"""Hybrid retrieval: pgvector cosine similarity plus exact keyword matching.

Replaces the in-memory cosine loop in poc/search_chunks.py. Both scores are
returned alongside the combined score so the dashboard can show why a chunk
was retrieved.

`hybrid_retrieve` (Phase 2) adds explainable signals on top of semantic
search: exact keyword, symbol and file-name matches, runtime-reported
locations and Git changes. Every signal's score, weight and contribution is
kept in `reasons`, so each retrieved chunk says why it was selected.
"""

import re
from dataclasses import asdict, dataclass, field

from pathlib import PurePosixPath

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
    signals: dict[str, float] = field(default_factory=dict)
    reasons: list[dict] = field(default_factory=list)
    role: str = "primary"
    relation: dict | None = None

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


# ----- Phase 2: hybrid, explainable retrieval -------------------------------

SIGNALS = ("semantic", "keyword", "symbol", "file_name", "runtime_location", "git_change")
CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[_\-\s.]+")
MAX_FRAME_CHUNKS = 5
KEYWORD_SATURATION = 6.0


@dataclass
class RetrievalQuery:
    text: str
    # Terms recovered from runtime evidence (mismatched fields, error identifiers); weighted double.
    runtime_terms: list[str] = field(default_factory=list)
    frame_functions: list[str] = field(default_factory=list)
    # [{file_path, line, source}] from stack traces and file:line references in the output.
    locations: list[dict] = field(default_factory=list)
    entry_files: list[str] = field(default_factory=list)
    # path -> 0..1 recency weight (recent commits) or 1.0 (commit under analysis).
    changed_files: dict[str, float] = field(default_factory=dict)
    changed_lines: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def terms(self) -> dict[str, float]:
        weighted: dict[str, float] = {}
        for term in extract_keywords(self.text, limit=25):
            weighted.setdefault(term.lower(), 1.0)
        for term in self.runtime_terms:
            for token in {term, *split_identifier(term)}:
                if len(token) >= 3 and token.lower() not in STOPWORDS:
                    weighted[token.lower()] = 2.0
        return weighted


def split_identifier(name: str) -> list[str]:
    return [part.lower() for part in CAMEL_SPLIT.split(name or "") if part]


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for identifier in re.findall(r"[A-Za-z_$][\w$]*", text or ""):
        tokens.add(identifier.lower())
        tokens.update(split_identifier(identifier))
    return tokens


def hybrid_weights(settings) -> dict[str, float]:
    return {
        "semantic": settings.hybrid_semantic_weight,
        "keyword": settings.hybrid_keyword_weight,
        "symbol": settings.hybrid_symbol_weight,
        "file_name": settings.hybrid_file_name_weight,
        "runtime_location": settings.hybrid_runtime_location_weight,
        "git_change": settings.hybrid_git_change_weight,
    }


def score_chunk(chunk, distance: float, query: RetrievalQuery, terms: dict[str, float], weights: dict[str, float]) -> RetrievedChunk:
    """Score one chunk on every signal; `chunk` needs the CodeChunk attributes."""
    signals: dict[str, float] = {}
    details: dict[str, str] = {}
    sources: list[str] = []

    semantic = max(0.0, 1.0 - float(distance)) if distance is not None else 0.0
    signals["semantic"] = round(semantic, 4)
    details["semantic"] = f"cosine similarity {semantic:.3f}"

    tokens = _tokens(f"{chunk.symbol}\n{chunk.content}")
    matched = [term for term in terms if term in tokens]
    if terms:
        matched_weight = sum(terms[term] for term in matched)
        # Runtime-derived terms weigh double, so an exact runtime match outweighs prose overlap.
        signals["keyword"] = round(min(1.0, matched_weight / min(sum(terms.values()), KEYWORD_SATURATION)), 4)
    else:
        signals["keyword"] = 0.0
    details["keyword"] = f"exact keyword: {', '.join(matched)}" if matched else "no exact keyword"

    symbol_lower = (chunk.symbol or "").lower()
    symbol_parts = set(split_identifier(chunk.symbol))
    frame_functions = {name.split(".")[-1].lower() for name in query.frame_functions if name}
    if symbol_lower in frame_functions:
        signals["symbol"], details["symbol"] = 1.0, f"symbol {chunk.symbol} is on the failing stack"
    elif symbol_lower in terms:
        signals["symbol"], details["symbol"] = 1.0, f"symbol {chunk.symbol} named in the query"
    elif symbol_parts & set(terms):
        signals["symbol"] = 0.6
        details["symbol"] = f"symbol {chunk.symbol} contains {', '.join(sorted(symbol_parts & set(terms)))}"
    else:
        signals["symbol"], details["symbol"] = 0.0, "no symbol match"

    path = PurePosixPath(chunk.file_path)
    raw_stem = path.name.split(".")[0]
    stem = raw_stem.lower()
    path_parts = set(split_identifier(raw_stem)) | {part.lower() for part in path.parent.parts}
    if stem in terms:
        signals["file_name"], details["file_name"] = 1.0, f"file name {path.name} matches"
    elif path_parts & set(terms):
        signals["file_name"] = 0.5
        details["file_name"] = f"file path contains {', '.join(sorted(path_parts & set(terms)))}"
    else:
        signals["file_name"], details["file_name"] = 0.0, "no file-name match"

    runtime, runtime_detail = 0.0, "not in a runtime-reported location"
    for location in query.locations:
        if location["file_path"] != chunk.file_path:
            continue
        if chunk.start_line <= location["line"] <= chunk.end_line:
            runtime = 1.0
            runtime_detail = f"covers runtime location {location['file_path']}:{location['line']} ({location.get('source')})"
            if location.get("source") == "stack_trace" and "stack_trace" not in sources:
                sources.append("stack_trace")
            break
        if runtime < 0.5:
            runtime, runtime_detail = 0.5, f"same file as runtime location {location['file_path']}:{location['line']}"
    if runtime < 0.5 and chunk.file_path in query.entry_files:
        runtime, runtime_detail = 0.5, f"in {chunk.file_path}, which the analysis command executes"
    signals["runtime_location"], details["runtime_location"] = runtime, runtime_detail

    git, git_detail = 0.0, "not in recently changed files"
    for start, end in query.changed_lines.get(chunk.file_path, []):
        if start <= chunk.end_line and chunk.start_line <= end:
            git, git_detail = 1.0, f"changed lines {start}-{end} fall inside this chunk"
            break
    if git == 0.0 and chunk.file_path in query.changed_files:
        git = round(0.6 * query.changed_files[chunk.file_path], 4)
        git_detail = "file changed in recent commits" if query.changed_files[chunk.file_path] < 1 else "file changed in the analyzed commits"
    signals["git_change"], details["git_change"] = git, git_detail

    total_weight = sum(weights.values()) or 1.0
    reasons = []
    combined = 0.0
    for name in SIGNALS:
        contribution = weights[name] * signals[name] / total_weight
        combined += contribution
        if signals[name] > 0:
            reasons.append(
                {
                    "signal": name,
                    "score": signals[name],
                    "weight": weights[name],
                    "contribution": round(contribution, 4),
                    "detail": details[name],
                }
            )
    reasons.sort(key=lambda reason: reason["contribution"], reverse=True)

    for name in SIGNALS:
        label = "lexical" if name == "keyword" else name
        if signals[name] > 0 and label not in sources:
            sources.append(label)

    return RetrievedChunk(
        chunk_id=chunk.id,
        file_path=chunk.file_path,
        symbol=chunk.symbol,
        chunk_type=chunk.chunk_type,
        language=chunk.language,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        content=chunk.content,
        semantic_score=signals["semantic"],
        lexical_score=signals["keyword"],
        combined_score=round(combined, 4),
        matched_keywords=matched,
        sources=sources,
        signals=signals,
        reasons=reasons,
    )


def hybrid_retrieve(
    db: Session,
    repository_id: int,
    query: RetrievalQuery,
    query_embedding: list[float],
    settings,
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    """Union of semantic, keyword, runtime-location and changed-file candidates, ranked by weighted signals.

    Chunks that contain an in-repository stack frame come first (they are
    direct runtime evidence), in frame order.
    """
    top_k = top_k or settings.retrieval_top_k
    candidate_k = settings.retrieval_candidate_k
    terms = query.terms()
    weights = hybrid_weights(settings)
    distance = CodeChunk.embedding.cosine_distance(query_embedding).label("distance")
    in_repository = CodeChunk.repository_id == repository_id

    candidate_ids: set[int] = set(
        db.scalars(select(CodeChunk.id).where(in_repository).order_by(distance).limit(candidate_k)).all()
    )
    if terms:
        keyword_filter = or_(
            *[CodeChunk.content.ilike(f"%{_escape_like(term)}%", escape="\\") for term in terms],
            *[CodeChunk.symbol.ilike(f"%{_escape_like(term)}%", escape="\\") for term in terms],
            *[CodeChunk.file_path.ilike(f"%{_escape_like(term)}%", escape="\\") for term in terms],
        )
        candidate_ids.update(
            db.scalars(select(CodeChunk.id).where(and_(in_repository, keyword_filter)).limit(candidate_k * 2)).all()
        )
    location_files = {location["file_path"] for location in query.locations} | set(query.entry_files)
    location_files |= set(query.changed_lines)
    if location_files:
        candidate_ids.update(
            db.scalars(select(CodeChunk.id).where(in_repository, CodeChunk.file_path.in_(location_files))).all()
        )
    if query.changed_files:
        candidate_ids.update(
            db.scalars(
                select(CodeChunk.id).where(in_repository, CodeChunk.file_path.in_(list(query.changed_files))).limit(candidate_k)
            ).all()
        )
    if not candidate_ids:
        return []

    rows = db.execute(select(CodeChunk, distance).where(CodeChunk.id.in_(candidate_ids))).all()
    scored = [score_chunk(chunk, chunk_distance, query, terms, weights) for chunk, chunk_distance in rows]

    frame_order = [
        (location["file_path"], location["line"]) for location in query.locations if location.get("source") == "stack_trace"
    ]
    frame_chunks: list[RetrievedChunk] = []
    for file_path, line in frame_order:
        covering = [
            chunk for chunk in scored
            if chunk.file_path == file_path and chunk.start_line <= line <= chunk.end_line and chunk not in frame_chunks
        ]
        if covering:
            frame_chunks.append(min(covering, key=lambda chunk: chunk.end_line - chunk.start_line))
        if len(frame_chunks) >= MAX_FRAME_CHUNKS:
            break
    rest = sorted((chunk for chunk in scored if chunk not in frame_chunks), key=lambda chunk: chunk.combined_score, reverse=True)
    return (frame_chunks + rest)[: max(top_k, len(frame_chunks))]
