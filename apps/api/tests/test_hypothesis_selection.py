"""Hypothesis selection in issue runs: the leading hypothesis must explain the observed behavior."""

import shutil
from pathlib import Path

import pytest

from app.services.hypothesis_selection import EVIDENCE_MODEL
from tests import synthetic_checkout as shop
from tests.conftest import commit_files
from tests.test_fixture_shaped_flow import requires_node_ts
from tests.test_pipeline_end_to_end import DISCOUNT, REPRO


def _start(client, local_remote, fake_ollama, files, regression_files, command, response_for) -> dict:
    commit_files(local_remote, files, "Initial version")
    regression = commit_files(local_remote, regression_files, "Change under test") if regression_files else None
    repository_id = client.post("/repositories", json={"url": "https://github.com/acme/shop"}).json()["id"]
    run = client.post(
        "/debug-runs", json={"repository_id": repository_id, "issue_description": shop.ISSUE, "analysis_command": command}
    ).json()
    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": command})
    fake_ollama.response = response_for(regression)
    assert client.post(f"/debug-runs/{run['id']}/run").status_code == 202
    report = client.get(f"/debug-runs/{run['id']}/report").json()
    assert report["run"]["status"] == "completed", report["run"]["error"]
    return report


def _duplicate_quantity(client, local_remote, fake_ollama, response_for) -> dict:
    return _start(
        client, local_remote, fake_ollama, shop.FILES_CORRECT, {shop.BUG_FILE: shop.ORDER_SERVICE_REGRESSION},
        shop.JSON_COMMAND, response_for,
    )


def _by_symbol(report: dict) -> dict:
    return {item["hypothesis"]["symbol"]: item for item in report["hypotheses"]}


def _checks(item: dict) -> dict:
    return {check["name"]: check["status"] for check in item["validation"]["checks"]}


@requires_node_ts
def test_duplicate_quantity_selects_the_upstream_writer_over_the_models_first_answer(client, local_remote, fake_ollama):
    # The model lists the unrelated coupon idea first; selection must lead with the hypothesis that explains the mismatch.
    report = _duplicate_quantity(
        client, local_remote, fake_ollama,
        lambda sha: {"hypotheses": [shop.legacy_coupon_hypothesis(sha), shop.add_item_hypothesis(sha)]},
    )

    leading = report["hypotheses"][0]
    assert leading["hypothesis"]["symbol"] == "addItem"
    assert leading["hypothesis"]["model"] != EVIDENCE_MODEL  # the model's own hypothesis was selected
    assert leading["validation"]["overall_result"] == "validated"
    assert report["hypothesis"]["id"] == leading["hypothesis"]["id"]
    assert report["run"]["overall_result"] == "validated"

    coupon = _by_symbol(report)["findCoupon"]
    assert coupon["hypothesis"]["rank"] == 2
    assert coupon["validation"]["overall_result"] in {"rejected", "needs_review"}
    checks = _checks(coupon)
    assert checks["affects_mismatched_field"] == "failed"
    assert checks["grounded_in_strong_evidence"] == "failed"
    assert len(report["hypotheses"]) == 2  # a model hypothesis validated, so nothing was derived

    prompt = fake_ollama.prompts[0]
    observed = prompt.split("=== Observed behavior (authoritative) ===")[1].split("===")[0]
    assert "subtotal: expected 105, actual 65 [ROOT mismatch]" in observed
    assert "discount: expected 10.5, actual 6.5 [downstream (follows from the root)]" in observed
    assert "Do not introduce concepts" in prompt
    assert '{"hypotheses": [ ... ]}' in prompt


@requires_node_ts
def test_unrelated_coupon_hypothesis_is_rejected_and_an_evidence_derived_writer_leads(client, local_remote, fake_ollama):
    report = _duplicate_quantity(client, local_remote, fake_ollama, shop.legacy_coupon_hypothesis)

    leading = report["hypotheses"][0]
    hypothesis = leading["hypothesis"]
    assert hypothesis["model"] == EVIDENCE_MODEL
    assert (hypothesis["symbol"], hypothesis["file_path"], hypothesis["line_number"]) == ("addItem", shop.BUG_FILE, shop.BUG_LINE)
    assert "subtotal is 65 instead of 105" in hypothesis["root_cause"]
    assert "quantity" in hypothesis["root_cause"]
    assert hypothesis["execution_path"][0].startswith("scripts/reproJsonLog.ts")
    assert any(step.endswith(" addItem") for step in hypothesis["execution_path"])
    assert any(step.endswith(" calculateSubtotal") for step in hypothesis["execution_path"])
    assert hypothesis["confidence"] == "low"
    assert leading["validation"]["overall_result"] == "validated"
    assert report["run"]["overall_result"] == "validated"

    coupon = _by_symbol(report)["findCoupon"]
    assert coupon["hypothesis"]["rank"] == 2
    assert coupon["validation"]["overall_result"] in {"rejected", "needs_review"}
    checks = _checks(coupon)
    assert checks["affects_mismatched_field"] == "failed"     # cannot explain the root subtotal mismatch
    assert checks["grounded_in_strong_evidence"] == "failed"  # coupon lookup is executed, but not in the subtotal's data flow


def _phase1_files() -> dict[str, str]:
    return {"package.json": '{"type": "module"}\n', "README.md": "# Shop\n"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for this test")
def test_legacy_coupon_exception_diagnosis_remains_validated(client, local_remote, fake_ollama):
    def legacy(sha):
        return {
            "summary": "Legacy coupons without discountType crash calculateDiscount.",
            "root_cause": "calculateDiscount calls coupon.discountType.toUpperCase() but legacy coupons have no discountType.",
            "file_path": "src/discount.js",
            "line": 2,
            "symbol": "calculateDiscount",
            "execution_path": ["src/repro.js:2 <module>", "src/discount.js:2 calculateDiscount"],
            "introducing_commit": sha,
            "confidence": "high",
            "missing_evidence": [],
        }

    report = _start(
        client, local_remote, fake_ollama, _phase1_files(), {"src/discount.js": DISCOUNT, "src/repro.js": REPRO},
        "node src/repro.js", legacy,
    )

    assert report["runtime_evidence"][0]["stack_trace"]["error_type"] == "TypeError"
    assert [item["hypothesis"]["symbol"] for item in report["hypotheses"]] == ["calculateDiscount"]
    assert report["run"]["overall_result"] == "validated"
    checks = _checks(report["hypotheses"][0])
    assert checks["grounded_in_strong_evidence"] == "passed"
    assert set(checks.values()) == {"passed"}
    observed = fake_ollama.prompts[0].split("=== Observed behavior (authoritative) ===")[1].split("===")[0]
    assert "Error: TypeError: Cannot read properties of undefined (reading 'toUpperCase')" in observed
    assert "Failing location: src/discount.js:2 in calculateDiscount" in observed


@requires_node_ts
def test_successful_reproduction_reports_no_bug(client, local_remote, fake_ollama):
    report = _start(
        client, local_remote, fake_ollama, shop.FILES_CORRECT, None, shop.JSON_COMMAND, shop.add_item_hypothesis
    )

    analysis = report["runtime_evidence"][0]
    assert analysis["exit_code"] == 0
    assert analysis["data"]["structured"]["mismatches"] == []
    assert report["run"]["overall_result"] == "no_bug_detected"
    assert report["conclusion"]["result"] == "no_bug_detected"
    assert report["conclusion"]["message"].startswith("No bug detected")
    assert report["hypotheses"] == [] and report["hypothesis"] is None and report["validation"] is None
    assert fake_ollama.prompts == []  # the model is not asked to invent a cause

    draft = client.post(f"/debug-runs/{report['run']['id']}/jira-draft")
    assert draft.status_code == 409
    assert client.get(f"/debug-runs/{report['run']['id']}/report").json()["ticket_draft"] is None
