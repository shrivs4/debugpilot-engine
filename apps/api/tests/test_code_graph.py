import re
import hashlib
from pathlib import Path

import pytest

from app.services.chunker import chunk_file
from app.services.code_graph import (
    build_graph,
    entry_files_from_command,
    expand_related,
    extract_facts,
    field_dependencies,
    field_influence,
    parse_imports,
    resolve_module,
)
from app.services.runtime_extraction import extract_runtime_evidence
from app.services.scanner import ScannedFile
from tests.synthetic_project import CHECK_OUTPUT, FILES_CORRECT


def chunk_dicts(files: dict[str, str]) -> list[dict]:
    chunks = []
    for path, content in files.items():
        language = {"ts": "typescript", "js": "javascript", "json": "json"}[path.rsplit(".", 1)[1]]
        scanned = ScannedFile(path, language, len(content), len(content.splitlines()), hashlib.sha256(content.encode()).hexdigest(), content)
        for chunk in chunk_file(scanned):
            chunks.append(
                {
                    "id": len(chunks) + 1,
                    "file_path": chunk.file_path,
                    "symbol": chunk.symbol,
                    "chunk_type": chunk.chunk_type,
                    "language": chunk.language,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "content": chunk.content,
                }
            )
    return chunks


@pytest.fixture
def project_graph():
    structured = extract_runtime_evidence(CHECK_OUTPUT, "", 1)
    graph = build_graph(chunk_dicts(FILES_CORRECT), None, set(structured["mismatched_fields"]), file_contents=FILES_CORRECT)
    return graph, structured


def node(graph, symbol):
    return next(item for item in graph.nodes.values() if item.symbol == symbol)


def edges(graph, symbol):
    return {(graph.nodes[edge.target].symbol, edge.type, edge.via) for edge in graph.edges[node(graph, symbol).id]}


def test_extract_facts_records_calls_reads_writes_and_data_flow():
    facts = extract_facts(
        "function f(order: Order) {\n  const fee = rate(order.weight);\n  order.total = fee + order.base;\n  log({ fee });\n}",
        "src/a.ts",
    )
    assert {"rate", "log"} <= facts.calls
    assert {"weight", "base"} <= facts.reads
    assert {"total", "fee"} <= facts.writes
    assert "Order" in facts.types
    assert facts.assigns["fee"] >= {"rate", "order", "weight"}
    assert facts.assigns["total"] >= {"fee", "base"}


def test_imports_resolve_to_repository_files():
    source = "import { a as b, c } from './lib/x.js';\nimport d from '../y';\nconst e = require('./z');\nimport ext from 'react';\n"
    imports = parse_imports(source, "src/main.ts")
    assert imports == {"b": "./lib/x.js", "c": "./lib/x.js", "d": "../y", "e": "./z", "ext": "react"}

    known = {"src/lib/x.ts", "y/index.js", "src/z.js"}
    assert resolve_module("src/main.ts", "./lib/x.js", known) == "src/lib/x.ts"
    assert resolve_module("src/main.ts", "../y", known) == "y/index.js"
    assert resolve_module("src/main.ts", "./z", known) == "src/z.js"
    assert resolve_module("src/main.ts", "react", known) is None


def test_call_type_and_shared_property_edges(project_graph):
    graph, _ = project_graph

    assert ("sumLines", "calls", "sumLines") in edges(graph, "settle")
    assert ("rebate", "calls", "rebate") in edges(graph, "settle")
    assert ("settle", "called_by", "sumLines") in edges(graph, "sumLines")
    # sumLines reads line.units; putLine writes found.units -> data dependency.
    assert ("putLine", "shared_property", "units") in edges(graph, "sumLines")
    script_calls = {
        graph.nodes[edge.target].symbol
        for script_node in graph.chunks_in_file("scripts/check.js")
        for edge in graph.edges[script_node.id]
        if edge.type == "calls"
    }
    assert {"createBasket", "putLine", "settle"} <= script_calls

    typed = build_graph(
        chunk_dicts({
            "src/types.ts": "export interface Parcel { grams: number }\n",
            "src/ship.ts": "import { Parcel } from './types';\nexport function cost(p: Parcel) { return p.grams * 2; }\n",
        }),
        None,
        file_contents={"src/ship.ts": "import { Parcel } from './types';\n"},
    )
    assert ("Parcel", "uses_type", "Parcel") in edges(typed, "cost")


def test_expand_related_follows_relationships_with_explanations(project_graph):
    graph, _ = project_graph
    primary = [{"chunk_id": node(graph, "settle").id, "combined_score": 0.8}]

    related = {graph.nodes[item["chunk_id"]].symbol: item for item in expand_related(graph, primary, 2, 10)}

    assert related["sumLines"]["relation"]["depth"] == 1
    assert related["sumLines"]["relation"]["type"] == "calls"
    assert related["putLine"]["relation"]["depth"] == 2
    # putLine writes the data sumLines reads (the lines it pushes and their units).
    assert re.fullmatch(
        r"settle -\[calls:sumLines\]-> sumLines -\[shared_property:(units|lines)\]-> putLine", related["putLine"]["relation"]["path"]
    )
    assert related["sumLines"]["score"] > related["putLine"]["score"]
    assert "settle" not in related


def test_field_dependencies_find_root_and_propagated_fields(project_graph):
    graph, structured = project_graph

    analysis = field_dependencies(graph, structured["mismatch_analysis"], structured["mismatches"])

    assert analysis["method"] == "code_dependencies"
    assert analysis["dependencies"] == {"gross": [], "reduction": ["gross"], "net": ["gross", "reduction"]}
    assert analysis["root_fields"] == ["gross"]
    assert analysis["propagated_fields"] == ["reduction", "net"]


def test_field_influence_separates_upstream_from_downstream(project_graph):
    graph, _ = project_graph

    gross = field_influence(graph, "gross")
    reduction = field_influence(graph, "reduction")

    assert node(graph, "putLine").id in gross
    assert node(graph, "rebate").id not in gross
    assert node(graph, "rebate").id in reduction


def test_entry_files_from_command(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check.js").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"scripts": {"check": "node scripts/check.js", "ci": "npm run check"}}', encoding="utf-8"
    )

    assert entry_files_from_command(["npm", "run", "check"], tmp_path) == ["scripts/check.js"]
    assert entry_files_from_command(["npm", "run", "ci"], tmp_path) == ["scripts/check.js"]
    assert entry_files_from_command(["node", "./scripts/check.js"], tmp_path) == ["scripts/check.js"]
    assert entry_files_from_command(["node", "../outside.js"], tmp_path) == []
    assert entry_files_from_command(["make", "test"], tmp_path) == []
