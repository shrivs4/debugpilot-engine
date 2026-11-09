"""Full debug run against a tiny local repository with a real Node reproduction.

Exercises clone -> index -> retrieval -> approved command execution -> stack
trace -> git blame -> context -> (fake) LLM -> validation -> mock Jira draft.
"""

import shutil
from pathlib import Path

import pytest

from tests.conftest import commit_files


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required for this test")

DISCOUNT = (
    "export function calculateDiscount(coupon, subtotal) {\n"
    "  const kind = coupon.discountType.toUpperCase();\n"
    "  return kind === 'PERCENTAGE' ? (subtotal * coupon.value) / 100 : coupon.value;\n"
    "}\n"
)
REPRO = "import { calculateDiscount } from './discount.js';\ncalculateDiscount({ code: 'LEGACY20', value: 20 }, 100);\n"


def test_debug_run_produces_validated_report_and_jira_draft(client, local_remote: Path, fake_ollama):
    commit_files(local_remote, {"package.json": '{"type": "module"}\n', "README.md": "# Shop\n"}, "init")
    bug_sha = commit_files(local_remote, {"src/discount.js": DISCOUNT}, "Read discountType for coupons")
    commit_files(local_remote, {"src/repro.js": REPRO}, "Add legacy coupon reproduction")

    repository_id = client.post("/repositories", json={"url": "https://github.com/acme/shop"}).json()["id"]
    assert client.get(f"/repositories/{repository_id}").json()["status"] == "ready"

    run = client.post(
        "/debug-runs",
        json={
            "repository_id": repository_id,
            "issue_description": "Checkout fails for legacy coupons because discountType is missing",
            "analysis_command": "node src/repro.js",
        },
    ).json()
    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": "node src/repro.js"})

    fake_ollama.response = {
        "summary": "Legacy coupons without discountType crash calculateDiscount.",
        "root_cause": "calculateDiscount calls coupon.discountType.toUpperCase() but legacy coupons have no discountType.",
        "file_path": "src/discount.js",
        "line": 2,
        "symbol": "calculateDiscount",
        "execution_path": ["src/repro.js:2 <module>", "src/discount.js:2 calculateDiscount"],
        "introducing_commit": bug_sha,
        "confidence": "high",
        "missing_evidence": [],
    }

    started = client.post(f"/debug-runs/{run['id']}/run")
    assert started.status_code == 202, started.text

    report = client.get(f"/debug-runs/{run['id']}/report").json()
    assert report["run"]["status"] == "completed", report["run"]["error"]
    assert report["run"]["overall_result"] == "validated"

    analysis = report["runtime_evidence"][0]
    assert analysis["phase"] == "analysis"
    assert analysis["exit_code"] == 1
    assert analysis["stack_trace"]["error_type"] == "TypeError"
    assert analysis["stack_trace"]["top_repository_frame"]["relative_path"] == "src/discount.js"

    cited = {(item["file_path"], item["symbol"]) for item in report["code_evidence"]}
    assert ("src/discount.js", "calculateDiscount") in cited
    first = report["code_evidence"][0]
    assert {"semantic_score", "lexical_score", "combined_score", "start_line", "end_line"} <= first.keys()
    assert "stack_trace" in first["sources"]

    assert report["git_evidence"]["suspect_commit"]["commit"] == bug_sha
    assert report["hypothesis"]["line_number"] == 2
    assert bug_sha in fake_ollama.prompts[0]
    assert {check["status"] for check in report["validation"]["checks"]} == {"passed"}

    draft = client.post(f"/debug-runs/{run['id']}/jira-draft")
    assert draft.status_code == 201
    ticket = draft.json()
    assert ticket["ticket_key"] == f"MOCK-{run['id']}"
    assert ticket["provider"] == "mock-jira"
    assert ticket["file_path"] == "src/discount.js" and ticket["line_number"] == 2
    assert ticket["validation_status"] == "validated"
    assert ticket["reproduction"]["analysis_command"] == "node src/repro.js"
    assert any(item["type"] == "git_commit" for item in ticket["evidence"])
    assert client.get(f"/debug-runs/{run['id']}/report").json()["ticket_draft"]["id"] == ticket["id"]


def test_failing_setup_command_stops_the_run(client, local_remote: Path, fake_ollama):
    commit_files(local_remote, {"src/a.js": "export const a = 1;\n"}, "init")
    repository_id = client.post("/repositories", json={"url": "https://github.com/acme/setup"}).json()["id"]
    run = client.post(
        "/debug-runs",
        json={
            "repository_id": repository_id,
            "issue_description": "anything",
            "setup_command": "node -e process.exit(4)",
            "analysis_command": "node src/a.js",
        },
    ).json()
    for kind, command in (("setup", "node -e process.exit(4)"), ("analysis", "node src/a.js")):
        client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": kind, "command": command})

    client.post(f"/debug-runs/{run['id']}/run")
    report = client.get(f"/debug-runs/{run['id']}/report").json()

    assert report["run"]["status"] == "failed"
    assert "Setup command failed (exit code 4)" in report["run"]["error"]
    assert [item["phase"] for item in report["runtime_evidence"]] == ["setup"]
    assert client.post(f"/debug-runs/{run['id']}/jira-draft").status_code == 409
