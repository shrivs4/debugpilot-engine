"""Regression: issue-flow ranking on a TypeScript project shaped like the real checkout fixture.

A reproduction module imports order mutation functions and checkout, adds the
same product twice and prints expected/actual subtotal, discount and total.
The upstream order mutation (not checkout or discount) must lead the evidence,
reached through direct import/call relationships from the reproduction module.
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.chunker import chunk_file
from app.services.code_graph import build_graph, parse_imports, resolve_module
from app.services.scanner import ScannedFile, detect_language
from tests import synthetic_checkout as shop
from tests.conftest import commit_files


def _node_supports_typescript() -> bool:
    if shutil.which("node") is None or shutil.which("npm") is None:
        return False
    version = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    major, minor = (int(part) for part in version.split(".")[:2])
    return (major, minor) >= (22, 6)


requires_node_ts = pytest.mark.skipif(not _node_supports_typescript(), reason="needs node >= 22.6 for --experimental-strip-types")


def _chunks(files: dict[str, str]) -> list[dict]:
    chunks = []
    for path, content in files.items():
        language = detect_language(Path(path))
        if language is None or language in {"json", "markdown"}:
            continue
        scanned = ScannedFile(path, language, len(content), len(content.splitlines()), hashlib.sha256(content.encode()).hexdigest(), content)
        for chunk in chunk_file(scanned):
            chunks.append({"id": len(chunks) + 1, "file_path": chunk.file_path, "symbol": chunk.symbol,
                           "chunk_type": chunk.chunk_type, "language": chunk.language, "start_line": chunk.start_line,
                           "end_line": chunk.end_line, "content": chunk.content})
    return chunks


def _calls_from(graph, file_path: str) -> dict[str, str]:
    """symbol called from any chunk of `file_path` -> file defining it."""
    return {
        graph.nodes[edge.target].symbol: graph.nodes[edge.target].file_path
        for node in graph.chunks_in_file(file_path)
        for edge in graph.edges[node.id]
        if edge.type == "calls"
    }


# ----- graph level --------------------------------------------------------------

def test_reproduction_module_imports_resolve_to_direct_calls():
    files = shop.FILES_CORRECT | {shop.BUG_FILE: shop.ORDER_SERVICE_REGRESSION}
    imports = parse_imports(shop.REPRO, "scripts/reproScenario.ts")
    assert imports == {
        "createOrder": "../src/checkout/orderService.ts",
        "addItem": "../src/checkout/orderService.ts",
        "applyCoupon": "../src/checkout/orderService.ts",
        "checkout": "../src/checkout/checkoutService.ts",
    }
    assert resolve_module("scripts/reproScenario.ts", imports["addItem"], set(files)) == shop.BUG_FILE

    graph = build_graph(_chunks(files), None, file_contents=files)
    calls = _calls_from(graph, "scripts/reproScenario.ts")
    assert calls["addItem"] == shop.BUG_FILE
    assert calls["checkout"] == "src/checkout/checkoutService.ts"
    assert _calls_from(graph, "src/checkout/checkoutService.ts")["calculateSubtotal"] == "src/checkout/pricing.ts"


def test_mutations_are_data_writes_but_literal_initializers_are_not():
    files = shop.FILES_CORRECT
    graph = build_graph(_chunks(files), None, file_contents=files)
    by_symbol = {node.symbol: node for node in graph.nodes.values()}

    assert {"quantity", "items"} <= by_symbol["addItem"].facts.data_writes  # `+=` and `items.push(...)`
    assert "items" in by_symbol["removeItem"].facts.data_writes              # `order.items = order.items.filter(...)`
    assert "items" not in by_symbol["createOrder"].facts.data_writes         # `items: []`
    assert "quantity" not in by_symbol["sampleItems"].facts.data_writes      # `quantity: 1`


def test_barrel_reexports_and_path_aliases_resolve_to_the_defining_file(tmp_path: Path):
    files = {
        "tsconfig.json": '{\n  // project config\n  "compilerOptions": {"baseUrl": ".", "paths": {"@app/*": ["src/*"]},},\n}\n',
        "src/orders/mutations.ts": "export function putUnit(order, n) { order.units += n; }\n",
        "src/orders/index.ts": "export * from './mutations';\nexport { putUnit as addUnit } from './mutations';\n",
        "src/cart.ts": "export function putUnit() { return 0; }\n",
        "src/other.ts": "export function putUnit() { return 1; }\n",
        "src/more.ts": "export function putUnit() { return 2; }\n",
        "scripts/run.ts": "import { putUnit } from '@app/orders';\nimport { addUnit } from '../src/orders/index';\nputUnit(o, 1);\naddUnit(o, 2);\n",
    }
    for path, content in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(content, encoding="utf-8")

    graph = build_graph(_chunks(files), tmp_path)
    script = graph.chunks_in_file("scripts/run.ts")[0]
    targets = {(edge.via, graph.nodes[edge.target].file_path) for edge in graph.edges[script.id] if edge.type == "calls"}

    # putUnit is defined in four files; the alias + barrel pin the right one.
    assert ("putUnit", "src/orders/mutations.ts") in targets
    assert not any(file != "src/orders/mutations.ts" for via, file in targets if via == "putUnit")


# ----- full pipeline -------------------------------------------------------------

def _run(client, local_remote, fake_ollama, hypothesis_for) -> dict:
    commit_files(local_remote, shop.FILES_CORRECT, "Order and checkout services")
    regression = commit_files(local_remote, {shop.BUG_FILE: shop.ORDER_SERVICE_REGRESSION}, "Simplify item merging")
    repository_id = client.post("/repositories", json={"url": "https://github.com/acme/shop"}).json()["id"]
    run = client.post(
        "/debug-runs", json={"repository_id": repository_id, "issue_description": shop.ISSUE, "analysis_command": shop.COMMAND}
    ).json()
    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": shop.COMMAND})
    fake_ollama.response = hypothesis_for(regression)
    assert client.post(f"/debug-runs/{run['id']}/run").status_code == 202
    report = client.get(f"/debug-runs/{run['id']}/report").json()
    assert report["run"]["status"] == "completed", report["run"]["error"]
    return report


def _flow_details(item: dict) -> list[str]:
    return [reason["detail"] for reason in item["reasons"] if reason["signal"] == "data_flow"]


@requires_node_ts
def test_upstream_order_mutation_leads_primary_evidence(client, local_remote, fake_ollama):
    report = _run(client, local_remote, fake_ollama, shop.add_item_hypothesis)

    structured = report["runtime_evidence"][0]["data"]["structured"]
    assert [(m["field"], m["expected"], m["actual"]) for m in structured["mismatches"]] == [
        ("subtotal", "105", "65"), ("discount", "10.5", "6.5"), ("total", "94.5", "58.5")
    ]
    assert structured["mismatch_analysis"]["root_fields"] == ["subtotal"]

    evidence = report["code_evidence"]
    primary = [item["symbol"] for item in evidence if item["role"] == "primary"]
    assert "addItem" in primary
    assert primary[0] == "addItem"
    assert primary.index("addItem") < primary.index("checkout")
    assert primary.index("addItem") < primary.index("calculateDiscount")
    assert primary.index("calculateSubtotal") < primary.index("calculateDiscount")

    by_symbol = {item["symbol"]: item for item in evidence}
    add_item = _flow_details(by_symbol["addItem"])
    assert any(detail.startswith("upstream writer: writes") and "quantity" in detail and "subtotal" in detail for detail in add_item)
    # Direct import/call relationships from the reproduction module, not shared properties.
    assert "direct call path from the reproduction entry module: <module> -[calls:addItem]-> addItem" in add_item
    checkout = _flow_details(by_symbol["checkout"])
    assert any(re.search(r"direct call path .*-\[calls:checkout\]-> checkout$", detail) for detail in checkout)
    subtotal = _flow_details(by_symbol["calculateSubtotal"])
    assert any(re.search(r"-\[calls:checkout\]-> checkout -\[calls:calculateSubtotal\]-> calculateSubtotal$", d) for d in subtotal)
    for detail in [*add_item, *checkout, *subtotal]:
        if detail.startswith("direct call path"):
            assert "shared_property" not in detail
    assert any("likely a symptom" in detail for detail in _flow_details(by_symbol["calculateDiscount"]))


@requires_node_ts
def test_add_item_hypothesis_is_validated(client, local_remote, fake_ollama):
    report = _run(client, local_remote, fake_ollama, shop.add_item_hypothesis)

    checks = {check["name"]: check for check in report["validation"]["checks"]}
    assert report["run"]["overall_result"] == "validated", report["validation"]["checks"]
    assert checks["execution_path_connects"]["status"] == "passed"
    assert "-[calls:addItem]-> addItem" in checks["execution_path_connects"]["details"]
    assert checks["affects_mismatched_field"]["status"] == "passed"
    assert checks["consistent_with_stronger_evidence"]["status"] == "passed"


@requires_node_ts
def test_discount_hypothesis_is_rejected(client, local_remote, fake_ollama):
    report = _run(client, local_remote, fake_ollama, shop.discount_hypothesis)

    model = next(item for item in report["hypotheses"] if item["hypothesis"]["symbol"] == "calculateDiscount")
    checks = {check["name"]: check for check in model["validation"]["checks"]}
    assert model["validation"]["overall_result"] == "rejected"
    assert model["hypothesis"]["rank"] > 1
    assert checks["consistent_with_stronger_evidence"]["status"] == "failed"
    assert "subtotal" in checks["consistent_with_stronger_evidence"]["details"]
    # The evidence still puts the upstream writer first, whatever the model claimed.
    primary = [item["symbol"] for item in report["code_evidence"] if item["role"] == "primary"]
    assert primary.index("addItem") < primary.index("calculateDiscount")
