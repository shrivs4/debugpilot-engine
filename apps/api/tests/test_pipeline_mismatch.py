"""Debug runs on a wrong-value regression (no stack trace) through the whole pipeline.

The reproduction only prints expected / actual values. The pipeline must
extract the mismatches, retrieve the upstream data writer through code
relationships, validate the upstream hypothesis and reject one that blames a
downstream calculation.
"""

import shutil
from pathlib import Path

import pytest

from tests.conftest import commit_files
from tests.synthetic_project import (
    BASKET_REGRESSION,
    FILES_CORRECT,
    downstream_hypothesis,
    upstream_hypothesis,
)


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required for this test")


def _run(client, local_remote: Path, fake_ollama, hypothesis_for) -> tuple[dict, str]:
    commit_files(local_remote, FILES_CORRECT, "Basket and pricing")
    regression = commit_files(local_remote, {"src/basket.js": BASKET_REGRESSION}, "Simplify line merging")

    repository_id = client.post("/repositories", json={"url": "https://github.com/acme/till"}).json()["id"]
    run = client.post(
        "/debug-runs",
        json={
            "repository_id": repository_id,
            "issue_description": "Settlement totals are wrong",
            "analysis_command": "node scripts/check.js",
        },
    ).json()
    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": "node scripts/check.js"})
    fake_ollama.response = hypothesis_for(regression)

    assert client.post(f"/debug-runs/{run['id']}/run").status_code == 202
    report = client.get(f"/debug-runs/{run['id']}/report").json()
    assert report["run"]["status"] == "completed", report["run"]["error"]
    return report, regression


def _statuses(report: dict) -> dict:
    return {check["name"]: check["status"] for check in report["validation"]["checks"]}


def test_structured_runtime_evidence_and_explainable_retrieval(client, local_remote, fake_ollama):
    report, _ = _run(client, local_remote, fake_ollama, upstream_hypothesis)

    analysis = report["runtime_evidence"][0]
    structured = analysis["data"]["structured"]
    assert analysis["stdout"].startswith("expected gross: 55")  # raw output is kept
    assert structured["mismatched_fields"] == ["gross", "reduction", "net"]
    assert structured["mismatch_analysis"]["method"] == "code_dependencies"
    assert structured["mismatch_analysis"]["root_fields"] == ["gross"]
    assert set(structured["mismatch_analysis"]["propagated_fields"]) == {"reduction", "net"}

    evidence = {(item["file_path"], item["symbol"]): item for item in report["code_evidence"]}
    assert ("src/basket.js", "putLine") in evidence
    for item in report["code_evidence"]:
        assert item["role"] in {"primary", "related"}
        assert item["reasons"], item
    settle = evidence[("src/pricing.js", "settle")]
    assert any(reason["signal"] == "keyword" and "gross" in reason["detail"] for reason in settle["reasons"])
    entry = evidence[("scripts/check.js", "<module>")]
    assert any(reason["signal"] == "runtime_location" for reason in entry["reasons"])

    prompt = fake_ollama.prompts[0]
    assert "Mismatched values (expected -> actual)" in prompt
    assert "Root mismatched field(s): gross" in prompt
    assert "why selected:" in prompt


def test_upstream_hypothesis_is_validated(client, local_remote, fake_ollama):
    report, regression = _run(client, local_remote, fake_ollama, upstream_hypothesis)

    statuses = _statuses(report)
    assert report["run"]["overall_result"] == "validated", report["validation"]["checks"]
    assert statuses["explains_observed_behavior"] == "passed"
    assert statuses["execution_path_connects"] == "passed"
    assert statuses["affects_mismatched_field"] == "passed"
    assert statuses["consistent_with_stronger_evidence"] == "passed"
    assert statuses["introducing_commit_matches_blame"] == "passed"
    # No stack frame runs inside putLine, so this is honestly reported as not confirmed.
    assert statuses["focused_reproduction"] == "skipped"
    assert report["hypothesis"]["affected_fields"] == ["gross", "reduction", "net"]


def test_downstream_hypothesis_is_rejected(client, local_remote, fake_ollama):
    report, _ = _run(client, local_remote, fake_ollama, downstream_hypothesis)

    # The model's (only) hypothesis is rejected and ranked below the evidence-derived upstream one.
    model = next(item for item in report["hypotheses"] if item["hypothesis"]["symbol"] == "rebate")
    statuses = {check["name"]: check["status"] for check in model["validation"]["checks"]}
    assert model["validation"]["overall_result"] == "rejected"
    assert model["hypothesis"]["rank"] > 1
    assert statuses["consistent_with_stronger_evidence"] == "failed"
    assert statuses["affects_mismatched_field"] == "failed"
    conflict = next(check for check in model["validation"]["checks"] if check["name"] == "consistent_with_stronger_evidence")
    assert "gross" in conflict["details"]
    assert report["hypothesis"]["symbol"] == "putLine"
