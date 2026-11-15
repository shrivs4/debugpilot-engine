"""Hybrid retrieval scoring against Postgres + pgvector, with deterministic fake embeddings."""

import hashlib
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.models import CodeChunk, File, Repository
from app.services.retrieval import RetrievalQuery, hybrid_retrieve, hybrid_weights, score_chunk


CHUNKS = [
    # (path, symbol, start, end, content)
    ("src/notes/overview.md", "Overview", 1, 10,
     "Freight surcharge totals for heavy parcels on overweight shipments and freight totals."),
    ("src/freight/surcharge.ts", "applySurcharge", 1, 12,
     "export function applySurcharge(cost, rule) {\n  return cost + rule.flat;\n}"),
    ("src/freight/dimWeight.ts", "dimensionalWeight", 1, 8,
     "export function dimensionalWeight(box) {\n  return (box.length * box.width * box.height) / 5000;\n}"),
    ("src/freight/zones.ts", "zoneFor", 1, 20,
     "export function zoneFor(postcode) {\n  const prefix = postcode.slice(0, 2);\n  return ZONES[prefix];\n}"),
    ("src/util/strings.ts", "pad", 1, 5, "export function pad(text, size) {\n  return text.padStart(size);\n}"),
]


@pytest.fixture
def indexed(db_session, fake_ollama):
    repository = Repository(url="https://github.com/acme/freight", owner="acme", name="freight", status="ready")
    db_session.add(repository)
    db_session.flush()
    ids = {}
    for path, symbol, start, end, content in CHUNKS:
        file_row = File(
            repository_id=repository.id, path=path, language="typescript", size_bytes=len(content),
            line_count=end, content_hash=hashlib.sha256(content.encode()).hexdigest(),
        )
        db_session.add(file_row)
        db_session.flush()
        chunk = CodeChunk(
            repository_id=repository.id, file_id=file_row.id, file_path=path, chunk_type="function", symbol=symbol,
            language="typescript", start_line=start, end_line=end, content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            embedding=fake_ollama.embed([content], "fake")[0], embedding_model="fake",
        )
        db_session.add(chunk)
        db_session.flush()
        ids[symbol] = chunk.id
    db_session.commit()
    return repository, ids


def retrieve(db_session, fake_ollama, repository, query: RetrievalQuery):
    embedding = fake_ollama.embed([query.text], "fake")[0]
    return {chunk.symbol: chunk for chunk in hybrid_retrieve(db_session, repository.id, query, embedding, get_settings(), top_k=10)}


def test_every_chunk_records_scored_reasons(db_session, fake_ollama, indexed):
    repository, _ = indexed
    results = retrieve(db_session, fake_ollama, repository, RetrievalQuery(text="Freight surcharge totals are wrong"))

    weights = hybrid_weights(get_settings())
    for chunk in results.values():
        assert set(chunk.signals) == set(weights)
        assert chunk.combined_score == pytest.approx(sum(reason["contribution"] for reason in chunk.reasons), abs=1e-3)
        assert all(reason["detail"] for reason in chunk.reasons)


def test_exact_keyword_symbol_and_file_matches(db_session, fake_ollama, indexed):
    repository, _ = indexed
    query = RetrievalQuery(text="Totals are wrong for heavy parcels", runtime_terms=["dimensionalWeight"])

    results = retrieve(db_session, fake_ollama, repository, query)

    dim = results["dimensionalWeight"]
    assert dim.signals["symbol"] == 1.0
    assert "dimensionalweight" in dim.matched_keywords
    reasons = {reason["signal"]: reason for reason in dim.reasons}
    assert "exact keyword" in reasons["keyword"]["detail"]
    assert reasons["file_name"]["score"] > 0  # dimWeight.ts shares the "dim"/"weight" parts
    assert "symbol" in dim.sources and "lexical" in dim.sources
    # The prose chunk is semantically closest to the question, but the exact runtime term wins.
    overview = results["Overview"]
    assert overview.signals["semantic"] > dim.signals["semantic"]
    assert dim.combined_score > overview.combined_score
    assert next(iter(results)) == "dimensionalWeight"


def test_runtime_location_chunks_come_first(db_session, fake_ollama, indexed):
    repository, _ = indexed
    query = RetrievalQuery(
        text="Freight surcharge totals are wrong",
        frame_functions=["zoneFor"],
        locations=[{"file_path": "src/freight/zones.ts", "line": 3, "function": "zoneFor", "source": "stack_trace"}],
    )

    ranked = list(retrieve(db_session, fake_ollama, repository, query).values())

    assert ranked[0].symbol == "zoneFor"
    assert ranked[0].signals["runtime_location"] == 1.0
    assert "stack_trace" in ranked[0].sources
    assert any("covers runtime location src/freight/zones.ts:3" in reason["detail"] for reason in ranked[0].reasons)


def test_changed_files_and_lines_are_boosted(db_session, fake_ollama, indexed):
    repository, _ = indexed
    base = RetrievalQuery(text="Totals are wrong")
    changed = RetrievalQuery(
        text="Totals are wrong",
        changed_files={"src/util/strings.ts": 1.0, "src/freight/zones.ts": 0.5},
        changed_lines={"src/util/strings.ts": [(2, 2)]},
    )

    before = retrieve(db_session, fake_ollama, repository, base)
    after = retrieve(db_session, fake_ollama, repository, changed)

    assert after["pad"].signals["git_change"] == 1.0
    assert after["pad"].combined_score > before.get("pad", SimpleNamespace(combined_score=0)).combined_score
    assert after["zoneFor"].signals["git_change"] == pytest.approx(0.3)
    assert "changed lines 2-2" in next(r["detail"] for r in after["pad"].reasons if r["signal"] == "git_change")


def test_score_chunk_without_embedding_distance():
    chunk = SimpleNamespace(
        id=-1, file_path="src/a.ts", symbol="computeLevy", chunk_type="function", language="typescript",
        start_line=1, end_line=3, content="function computeLevy(base) { return base * LEVY; }",
    )
    query = RetrievalQuery(text="levy is wrong", runtime_terms=["levy"])

    result = score_chunk(chunk, None, query, query.terms(), hybrid_weights(get_settings()))

    assert result.signals["semantic"] == 0.0
    assert result.signals["symbol"] == 0.6
    assert result.matched_keywords == ["levy"]
