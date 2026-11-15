"""Execution / data-flow expansion for vague issue reports (no stack trace).

The synthetic ledger project (tests/synthetic_ledger.py) hides an upstream
writer bug behind a service-object method, uses the affected property in many
places, and contains keyword-rich decoys for the downstream calculation.
"""

import hashlib
import shutil
from pathlib import Path

import pytest

from app.services.chunker import chunk_file
from app.services.code_graph import (
    build_graph,
    computing_nodes,
    entry_files_from_command,
    field_dependencies,
    field_influence,
    link_field_data_flow,
    trace_issue_flow,
)
from app.services.runtime_extraction import extract_runtime_evidence
from app.services.scanner import ScannedFile, detect_language
from tests import synthetic_ledger as ledger
from tests.conftest import commit_files


# ----- graph level ------------------------------------------------------------

def _chunks(files: dict[str, str]) -> list[dict]:
    chunks = []
    for path, content in files.items():
        language = detect_language(Path(path))
        if language is None:
            continue
        scanned = ScannedFile(path, language, len(content), len(content.splitlines()), hashlib.sha256(content.encode()).hexdigest(), content)
        for chunk in chunk_file(scanned):
            chunks.append({"id": len(chunks) + 1, "file_path": chunk.file_path, "symbol": chunk.symbol,
                           "chunk_type": chunk.chunk_type, "language": chunk.language, "start_line": chunk.start_line,
                           "end_line": chunk.end_line, "content": chunk.content})
    return chunks


@pytest.fixture
def ledger_graph():
    files = ledger.FILES_CORRECT | {ledger.BUG_FILE: ledger.ENTRY_STORE_REGRESSION}
    structured = extract_runtime_evidence(ledger.SCENARIO_OUTPUT, "", 1)
    graph = build_graph(_chunks(files), None, set(structured["mismatched_fields"]), file_contents=files)
    structured["mismatch_analysis"] = field_dependencies(graph, structured["mismatch_analysis"], structured["mismatches"])
    entry_ids = [node.id for node in graph.chunks_in_file("scripts/scenario.js")]
    return graph, structured, entry_ids


def _node(graph, symbol):
    return next(node for node in graph.nodes.values() if node.symbol == symbol)


def test_common_properties_feeding_a_mismatched_field_are_linked(ledger_graph):
    graph, structured, _ = ledger_graph
    hours_users = graph.readers["hours"] | graph.writers["hours"]
    assert len(hours_users) > 6  # common enough that generic linking skips it
    writer = _node(graph, "entryStore").id
    assert writer not in field_influence(graph, "base")

    linked = link_field_data_flow(graph, structured["mismatched_fields"])

    assert {"hours", "rate"} <= linked
    assert "computeBase -[shared_property:" in field_influence(graph, "base")[writer]
    # The downstream allowance calculation itself writes none of the base's inputs.
    assert _node(graph, "computeAllowance").id not in field_influence(graph, "base")


def test_trace_ranks_upstream_writer_above_downstream_calculation(ledger_graph):
    graph, structured, entry_ids = ledger_graph
    link_field_data_flow(graph, structured["mismatched_fields"])

    flow = trace_issue_flow(graph, entry_ids, structured["mismatch_analysis"], structured["mismatches"], limit=50)
    ranked = [graph.nodes[item["chunk_id"]].symbol for item in flow]
    reasons = {graph.nodes[item["chunk_id"]].symbol: item for item in flow}

    assert ranked[0] == "entryStore"
    assert any("upstream writer: writes" in reason and "hours" in reason for reason in reasons["entryStore"]["reasons"])
    assert any("direct call path from the reproduction entry module" in reason for reason in reasons["entryStore"]["reasons"])
    assert ranked.index("computeBase") < ranked.index("computeAllowance")
    assert reasons["entryStore"]["score"] > reasons["computeAllowance"]["score"]
    assert any("likely a symptom" in reason for reason in reasons["computeAllowance"]["reasons"])
    # Literal-only fixtures produce no data; test helpers that do are down-weighted.
    assert "sampleEntries" not in reasons
    assert reasons["makeEntry"]["score"] < reasons["clampHours"]["score"]


def test_literal_assignments_do_not_compute_a_field(ledger_graph):
    graph, _, _ = ledger_graph
    computing = {node.symbol for node in computing_nodes(graph, "base")}
    assert "buildInvoice" in computing
    assert "expected" not in computing  # `const expected = { base: 290, ... }`


def test_plain_text_failure_uses_the_execution_path(ledger_graph):
    graph, _, _ = ledger_graph
    entry_ids = [node.id for node in graph.chunks_in_file("scripts/plain.js")]

    flow = trace_issue_flow(graph, entry_ids, None, [], limit=20)
    symbols = [graph.nodes[item["chunk_id"]].symbol for item in flow]

    assert {"entryStore", "buildInvoice", "computeBase", "computeAllowance"} <= set(symbols)
    assert all(any("reproduction entry module" in reason for reason in item["reasons"]) for item in flow)


def test_entry_files_map_build_output_to_sources(tmp_path: Path):
    (tmp_path / "src" / "tools").mkdir(parents=True)
    (tmp_path / "src" / "tools" / "repro.ts").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts": {"repro": "node dist/tools/repro.js"}}', encoding="utf-8")

    assert entry_files_from_command(["npm", "run", "repro"], tmp_path) == ["src/tools/repro.ts"]


def test_object_methods_written_as_arrow_functions_are_definitions():
    files = {
        "src/store.js": "export const store = {\n  put: (bag, n) => { bag.count = n; },\n};\n",
        "src/main.js": "import { store } from './store.js';\nstore.put(bag, 2);\n",
    }
    graph = build_graph(_chunks(files), None, file_contents=files)
    main = next(node for node in graph.nodes.values() if node.file_path == "src/main.js")
    assert any(graph.nodes[edge.target].symbol == "store" and edge.type == "calls" for edge in graph.edges[main.id])


# ----- full pipeline ------------------------------------------------------------

pipeline = pytest.mark.skipif(shutil.which("node") is None or shutil.which("npm") is None, reason="node and npm are required")


def _run(client, local_remote, fake_ollama, hypothesis_for, command=ledger.COMMAND) -> tuple[dict, str]:
    commit_files(local_remote, ledger.FILES_CORRECT, "Ledger and billing")
    regression = commit_files(local_remote, {ledger.BUG_FILE: ledger.ENTRY_STORE_REGRESSION}, "Simplify recording")
    repository_id = client.post("/repositories", json={"url": "https://github.com/acme/ledger"}).json()["id"]
    run = client.post(
        "/debug-runs", json={"repository_id": repository_id, "issue_description": ledger.ISSUE, "analysis_command": command}
    ).json()
    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": command})
    fake_ollama.response = hypothesis_for(regression)
    assert client.post(f"/debug-runs/{run['id']}/run").status_code == 202
    report = client.get(f"/debug-runs/{run['id']}/report").json()
    assert report["run"]["status"] == "completed", report["run"]["error"]
    return report, regression


def _checks(report: dict) -> dict:
    return {check["name"]: check["status"] for check in report["validation"]["checks"]}


@pipeline
def test_vague_incorrect_total_report_reaches_the_upstream_writer(client, local_remote, fake_ollama):
    report, _ = _run(client, local_remote, fake_ollama, ledger.upstream_hypothesis)

    evidence = report["code_evidence"]
    symbols = [item["symbol"] for item in evidence]
    writer = evidence[symbols.index("entryStore")]
    assert writer["role"] == "primary"
    assert symbols[0] == "entryStore"  # no stack trace, so the upstream writer leads the evidence
    assert "data_flow" in writer["sources"]
    details = [reason["detail"] for reason in writer["reasons"] if reason["signal"] == "data_flow"]
    assert any("feeds root field base" in detail for detail in details)
    assert any("upstream writer: writes" in detail for detail in details)
    # Every related chunk records why it was included.
    assert all(item["reasons"] for item in evidence if item["role"] == "related")
    # The existing hybrid signals are still recorded on retrieved chunks.
    signals = {reason["signal"] for item in evidence for reason in item["reasons"]}
    assert {"semantic", "keyword", "runtime_location", "git_change", "data_flow"} <= signals

    prompt = fake_ollama.prompts[0]
    assert "Execution and data-flow analysis (most upstream first)" in prompt
    flow_section = prompt.split("Execution and data-flow analysis")[1]
    assert flow_section.index("entryStore") < flow_section.index("buildInvoice")


@pipeline
def test_downstream_allowance_is_not_ranked_above_the_upstream_cause(client, local_remote, fake_ollama):
    report, _ = _run(client, local_remote, fake_ollama, ledger.downstream_hypothesis)

    symbols = [item["symbol"] for item in report["code_evidence"]]
    assert symbols.index("entryStore") < symbols.index("computeAllowance")
    assert symbols.index("computeBase") < symbols.index("computeAllowance")
    model = next(item for item in report["hypotheses"] if item["hypothesis"]["symbol"] == "computeAllowance")
    assert model["validation"]["overall_result"] == "rejected"
    assert model["hypothesis"]["rank"] > 1
    assert {c["name"]: c["status"] for c in model["validation"]["checks"]}["consistent_with_stronger_evidence"] == "failed"
    assert report["hypothesis"]["symbol"] == "entryStore"


@pipeline
def test_upstream_hypothesis_is_validated_with_runtime_support(client, local_remote, fake_ollama):
    report, _ = _run(client, local_remote, fake_ollama, ledger.upstream_hypothesis)

    checks = _checks(report)
    assert report["run"]["overall_result"] == "validated", report["validation"]["checks"]
    assert checks["reproduction_failed"] == "passed"
    assert checks["hypothesis_location_cited"] == "passed"
    assert checks["location_in_evidence"] == "passed"
    assert checks["affects_mismatched_field"] == "passed"
    assert checks["execution_path_connects"] == "passed"
    assert checks["consistent_with_stronger_evidence"] == "passed"


@pipeline
def test_plain_text_failure_still_completes_with_execution_path_evidence(client, local_remote, fake_ollama):
    def vague(commit):
        return ledger.upstream_hypothesis(commit) | {"affected_fields": []}

    report, _ = _run(client, local_remote, fake_ollama, vague, command="npm run repro:plain")

    structured = report["runtime_evidence"][0]["data"]["structured"]
    assert structured["mismatches"] == []
    assert report["runtime_evidence"][0]["exit_code"] == 1
    flow = [item for item in report["code_evidence"] if "data_flow" in item["sources"]]
    assert {"entryStore", "buildInvoice"} <= {item["symbol"] for item in flow}
    assert all(any("reproduction entry module" in r["detail"] for r in item["reasons"]) for item in flow)
    # Without mismatched fields or a stack trace nothing confirms the claim at runtime.
    assert report["run"]["overall_result"] == "needs_review"
