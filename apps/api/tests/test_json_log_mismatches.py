"""Structured JSON logs with a top-level `mismatches` array (the shape the real checkout fixture emits)."""

import json

import pytest

from app.services.runtime_extraction import extract_runtime_evidence
from tests import synthetic_checkout as shop
from tests.test_fixture_shaped_flow import requires_node_ts
from tests.conftest import commit_files


ONE_LINE = json.dumps(json.loads(shop.FIXTURE_JSON_LOG))


def fields(result: dict) -> list[tuple[str, str, str]]:
    return [(item["field"], item["expected"], item["actual"]) for item in result["mismatches"]]


@pytest.mark.parametrize(
    "stdout, stderr",
    [
        (f"> shop@ repro\n{ONE_LINE}\n", ""),                       # one JSON log line on stdout
        ("> shop@ repro\n", f"{ONE_LINE}\n"),                       # ... on stderr
        (f"starting\n{shop.FIXTURE_JSON_LOG}\ndone\n", ""),        # the pretty-printed record as given
    ],
)
def test_mismatches_array_is_extracted(stdout, stderr):
    result = extract_runtime_evidence(stdout, stderr, 1)

    assert result["extracted"] is True
    assert fields(result) == [("subtotal", "105", "65"), ("discount", "10.5", "6.5"), ("total", "94.5", "58.5")]
    assert result["mismatched_fields"] == ["subtotal", "discount", "total"]
    assert all(item["source"] == "structured_log" for item in result["mismatches"])
    # The raw log record is preserved, both on each mismatch and in the structured logs.
    assert json.loads(result["mismatches"][0]["raw"]) == json.loads(shop.FIXTURE_JSON_LOG)
    log = next(entry for entry in result["structured_logs"] if entry["format"] == "json")
    assert json.loads(log["raw"])["message"] == "Scenario checkout produced unexpected totals"
    assert log["data"]["level"] == "error"
    # All three deviate by the same factor: one upstream cause; subtotal is the root candidate.
    analysis = result["mismatch_analysis"]
    assert analysis["proportional_groups"] == [{"ratio": 0.619048, "fields": ["subtotal", "discount", "total"]}]
    assert analysis["root_fields"] == ["subtotal"]


def test_value_types_and_unchanged_values():
    log = json.dumps({"mismatches": [
        {"field": "isPaid", "expected": True, "actual": False},
        {"field": "currency", "expected": "EUR", "actual": "USD"},
        {"field": "coupon", "expected": "SAVE10", "actual": None},
        {"field": "notes", "expected": None, "actual": None},
        {"field": "count", "expected": 2, "actual": 2.0},
        {"name": "items.length", "expected": 3, "received": 2},
        {"expected": 1, "actual": 2},
        "not an object",
    ]})

    result = extract_runtime_evidence(log, "", 1)

    assert fields(result) == [
        ("ispaid", "true", "false"),
        ("currency", "EUR", "USD"),
        ("coupon", "SAVE10", "null"),
        ("items_length", "3", "2"),
    ]
    assert result["mismatches"][0]["ratio"] is None


def test_multiple_mismatch_logs_merge_deterministically():
    first = json.dumps({"mismatches": [{"field": "a", "expected": 1, "actual": 2}, {"field": "b", "expected": 5, "actual": 4}]})
    second = json.dumps({"mismatches": [{"field": "a", "expected": 1, "actual": 3}, {"field": "c", "expected": 0, "actual": 9}]})

    result = extract_runtime_evidence(f"{first}\n", f"{second}\n", 1)
    again = extract_runtime_evidence(f"{first}\n", f"{second}\n", 1)

    assert fields(result) == [("a", "1", "2"), ("b", "5", "4"), ("c", "0", "9")]  # first report wins, output order
    assert result["mismatches"][0]["later_values"] == {"expected": [], "actual": ["3"]}
    assert result["mismatches"] == again["mismatches"]


def test_malformed_json_and_plain_text_do_not_fail():
    stdout = '{"level": "error", "mismatches": [ {"field": "x"\nnot json at all {\n{"level":"info","message":"ok"}\nplain text line\n'

    result = extract_runtime_evidence(stdout, "Error: something broke\n", 1)

    assert result["mismatches"] == []
    assert [entry["data"]["message"] for entry in result["structured_logs"]] == ["ok"]
    assert result["errors"] == ["Error: something broke"]


@requires_node_ts
def test_json_log_only_reproduction_promotes_the_upstream_writer(client, local_remote, fake_ollama):
    commit_files(local_remote, shop.FILES_CORRECT, "Order and checkout services")
    regression = commit_files(local_remote, {shop.BUG_FILE: shop.ORDER_SERVICE_REGRESSION}, "Simplify item merging")
    repository_id = client.post("/repositories", json={"url": "https://github.com/acme/shop"}).json()["id"]
    run = client.post(
        "/debug-runs",
        json={"repository_id": repository_id, "issue_description": shop.ISSUE, "analysis_command": shop.JSON_COMMAND},
    ).json()
    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": shop.JSON_COMMAND})
    fake_ollama.response = shop.add_item_hypothesis(regression)
    assert client.post(f"/debug-runs/{run['id']}/run").status_code == 202
    report = client.get(f"/debug-runs/{run['id']}/report").json()
    assert report["run"]["status"] == "completed", report["run"]["error"]

    analysis_evidence = report["runtime_evidence"][0]
    assert "expected subtotal" not in analysis_evidence["stdout"]  # only JSON logs were printed
    assert '"mismatches"' in analysis_evidence["stdout"]           # raw output is kept
    structured = analysis_evidence["data"]["structured"]           # what the UI's mismatch table reads
    assert [(m["field"], m["expected"], m["actual"]) for m in structured["mismatches"]] == [
        ("subtotal", "105", "65"), ("discount", "10.5", "6.5"), ("total", "94.5", "58.5")
    ]
    mismatch_analysis = structured["mismatch_analysis"]
    assert mismatch_analysis["method"] == "code_dependencies"
    assert mismatch_analysis["root_fields"] == ["subtotal"]
    assert set(mismatch_analysis["propagated_fields"]) == {"discount", "total"}

    primary = [item["symbol"] for item in report["code_evidence"] if item["role"] == "primary"]
    assert primary[0] == "addItem"
    assert primary.index("addItem") < primary.index("checkout")
    add_item = next(item for item in report["code_evidence"] if item["symbol"] == "addItem")
    assert any(
        reason["detail"].startswith("upstream writer: writes") and "subtotal" in reason["detail"]
        for reason in add_item["reasons"]
    )
    assert "Root mismatched field(s): subtotal" in fake_ollama.prompts[0]
    assert report["run"]["overall_result"] == "validated"
