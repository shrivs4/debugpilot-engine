"""Commit and commit-range analysis through the API, against a local repository."""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import get_settings
from tests.conftest import commit_files
from tests.synthetic_project import (
    BASKET_CORRECT,
    BASKET_REGRESSION,
    FILES_CORRECT,
    downstream_hypothesis,
    upstream_hypothesis,
)


@pytest.fixture
def history(client, local_remote: Path):
    """init (correct) -> regression -> unrelated docs -> fix. HEAD behaves correctly."""
    shas = {
        "init": commit_files(local_remote, FILES_CORRECT, "Basket and pricing"),
        "regression": commit_files(local_remote, {"src/basket.js": BASKET_REGRESSION}, "Simplify line merging"),
        "docs": commit_files(local_remote, {"README.md": "# Till\n"}, "Add readme"),
        "fix": commit_files(local_remote, {"src/basket.js": BASKET_CORRECT}, "Restore unit accumulation"),
    }
    repository_id = client.post("/repositories", json={"url": "https://github.com/acme/till"}).json()["id"]
    return repository_id, shas


def create(client, repository_id: int, **body) -> dict:
    response = client.post("/commit-analyses", json={"repository_id": repository_id, **body})
    assert response.status_code == 201, response.text
    return response.json()


def test_list_commits(client, history):
    repository_id, shas = history

    commits = client.get(f"/repositories/{repository_id}/commits?limit=10").json()

    assert [commit["sha"] for commit in commits] == [shas["fix"], shas["docs"], shas["regression"], shas["init"]]
    assert commits[0]["subject"] == "Restore unit accumulation"
    assert commits[0]["short"] == shas["fix"][:8]
    assert commits[1]["parents"] == [shas["regression"]]


@pytest.mark.parametrize(
    "body, message",
    [
        ({"commit_sha": "HEAD"}, "is not a commit SHA"),
        ({"commit_sha": "--output=/tmp/x"}, "is not a commit SHA"),
        ({"commit_sha": "deadbeefdeadbeef"}, "Commit not found"),
        ({"base_sha": "abc1234"}, "Provide either commit_sha or both"),
    ],
)
def test_invalid_commit_selection_is_rejected(client, history, body, message):
    repository_id, _ = history

    response = client.post("/commit-analyses", json={"repository_id": repository_id, **body})

    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_range_must_be_ordered_and_commands_validated(client, history):
    repository_id, shas = history

    reversed_range = client.post(
        "/commit-analyses", json={"repository_id": repository_id, "base_sha": shas["fix"], "head_sha": shas["init"]}
    )
    assert reversed_range.status_code == 422
    assert "ancestor" in reversed_range.json()["detail"]

    unsafe = client.post(
        "/commit-analyses",
        json={"repository_id": repository_id, "commit_sha": shas["regression"], "analysis_command": "node a.js && rm -rf /"},
    )
    assert unsafe.status_code == 422
    assert "Shell operators" in unsafe.json()["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for this test")
def test_single_commit_analysis_runs_in_a_worktree_and_validates_hypotheses(client, history, fake_ollama):
    repository_id, shas = history
    run = create(client, repository_id, commit_sha=shas["regression"][:10], analysis_command="node scripts/check.js")

    assert run["run_type"] == "commit_analysis"
    assert run["target_commit"] == shas["regression"]
    assert run["base_commit"] == shas["init"]
    assert run["ready_to_run"] is False
    assert client.post(f"/debug-runs/{run['id']}/run").status_code == 409  # approval is still required

    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": "node scripts/check.js"})
    fake_ollama.response = {
        "hypotheses": [upstream_hypothesis(shas["regression"]), downstream_hypothesis(shas["regression"])]
    }
    assert client.post(f"/debug-runs/{run['id']}/run").status_code == 202

    report = client.get(f"/debug-runs/{run['id']}/report").json()
    assert report["run"]["status"] == "completed", report["run"]["error"]

    # Commands ran at the analyzed commit (the fix at HEAD would have exited 0).
    analysis = report["runtime_evidence"][0]
    assert analysis["exit_code"] == 1
    assert analysis["data"]["structured"]["mismatched_fields"] == ["gross", "reduction", "net"]

    commit = report["commit_analysis"]
    assert [item["sha"] for item in commit["commits"]] == [shas["regression"]]
    assert commit["changed_files"] == [
        {"status": "M", "file_path": "src/basket.js", "symbols": ["putLine"], "line_ranges": [[8, 8]]}
    ]

    changed = next(item for item in report["code_evidence"] if item["symbol"] == "putLine")
    assert changed["role"] == "primary"
    assert "changed_in_commit" in changed["sources"]
    assert any(reason["signal"] == "git_change" and reason["score"] == 1.0 for reason in changed["reasons"])

    results = [(item["hypothesis"]["rank"], item["hypothesis"]["symbol"], item["validation"]["overall_result"]) for item in report["hypotheses"]]
    assert results == [(1, "putLine", "validated"), (2, "rebate", "rejected")]
    assert report["run"]["overall_result"] == "validated"
    assert report["hypothesis"]["symbol"] == "putLine"
    checks = {check["name"]: check["status"] for check in report["validation"]["checks"]}
    assert checks["introducing_commit_in_range"] == "passed"
    assert "Commits under analysis" in fake_ollama.prompts[0]

    # The worktree is removed and the shared clone is untouched.
    assert not (get_settings().worktrees_dir / f"run-{run['id']}").exists()
    clone = next(path.parent for path in get_settings().repositories_dir.rglob(".git"))
    worktrees = subprocess.run(
        ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip().splitlines()
    assert len(worktrees) == 1 and shas["fix"][:7] in worktrees[0]

    # Two hypotheses: the ticket's hypothesis must be named explicitly, and only a validated one is accepted.
    assert client.post(f"/debug-runs/{run['id']}/jira-draft").status_code == 422
    rejected_id = report["hypotheses"][1]["hypothesis"]["id"]
    assert client.post(f"/debug-runs/{run['id']}/jira-draft", json={"hypothesis_id": rejected_id}).status_code == 409
    validated_id = report["hypotheses"][0]["hypothesis"]["id"]
    draft = client.post(f"/debug-runs/{run['id']}/jira-draft", json={"hypothesis_id": validated_id})
    assert draft.status_code == 201
    assert draft.json()["file_path"] == "src/basket.js"
    assert draft.json()["hypothesis_id"] == validated_id


def test_range_analysis_without_commands_stays_needs_review(client, history, fake_ollama):
    repository_id, shas = history
    run = create(client, repository_id, base_sha=shas["init"], head_sha=shas["docs"], notes="Totals changed this week")

    assert run["ready_to_run"] is True  # no commands -> nothing to approve
    fake_ollama.response = {"hypotheses": [upstream_hypothesis(shas["regression"])]}
    assert client.post(f"/debug-runs/{run['id']}/run").status_code == 202

    report = client.get(f"/debug-runs/{run['id']}/report").json()
    assert report["run"]["status"] == "completed", report["run"]["error"]
    assert report["runtime_evidence"] == []
    assert [item["sha"] for item in report["commit_analysis"]["commits"]] == [shas["docs"], shas["regression"]]
    assert {item["file_path"] for item in report["commit_analysis"]["changed_files"]} == {"README.md", "src/basket.js"}
    assert report["commit_analysis"]["notes"] == "Totals changed this week"
    assert "Totals changed this week" in fake_ollama.prompts[0]

    checks = {check["name"]: check["status"] for check in report["validation"]["checks"]}
    assert checks["reproduction_failed"] == "skipped"
    assert checks["introducing_commit_in_range"] == "passed"
    assert report["run"]["overall_result"] == "needs_review"
    response = client.post(f"/debug-runs/{run['id']}/jira-draft")
    assert response.status_code == 409 and "Human review required" in response.json()["detail"]


def test_commit_analyses_are_listed_separately(client, history):
    repository_id, shas = history
    run = create(client, repository_id, commit_sha=shas["docs"])

    listed = client.get("/commit-analyses").json()
    assert [item["id"] for item in listed] == [run["id"]]
    assert any(item["id"] == run["id"] for item in client.get("/debug-runs").json())


def test_root_commit_is_compared_with_the_empty_tree(client, history, fake_ollama):
    repository_id, shas = history
    run = create(client, repository_id, commit_sha=shas["init"])
    assert run["base_commit"] is None

    fake_ollama.response = {"hypotheses": []}
    client.post(f"/debug-runs/{run['id']}/run")
    report = client.get(f"/debug-runs/{run['id']}/report").json()

    assert report["run"]["status"] == "completed", report["run"]["error"]
    assert {item["status"] for item in report["commit_analysis"]["changed_files"]} == {"A"}
    assert report["hypotheses"] == [] and report["hypothesis"] is None
    assert report["run"]["overall_result"] == "needs_review"


# ----- no regression detected ---------------------------------------------------

NO_BUG = "No bug detected: the analysis command succeeded and reported no mismatches, errors, or stack traces."


def _run_to_completion(client, run_id: int) -> dict:
    assert client.post(f"/debug-runs/{run_id}/run").status_code == 202
    report = client.get(f"/debug-runs/{run_id}/report").json()
    assert report["run"]["status"] == "completed", report["run"]["error"]
    return report


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for this test")
def test_fixed_commit_with_passing_command_reports_no_bug(client, history, fake_ollama):
    repository_id, shas = history
    run = create(client, repository_id, commit_sha=shas["fix"], analysis_command="node scripts/check.js")
    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": "node scripts/check.js"})
    fake_ollama.response = {"hypotheses": [upstream_hypothesis(shas["fix"])]}

    report = _run_to_completion(client, run["id"])

    analysis = report["runtime_evidence"][0]
    assert analysis["exit_code"] == 0
    assert analysis["data"]["structured"]["mismatches"] == []
    assert report["run"]["overall_result"] == "no_bug_detected"
    assert report["conclusion"] == {"result": "no_bug_detected", "message": NO_BUG, "command": "node scripts/check.js"}
    assert fake_ollama.prompts == []                       # the model is not asked to speculate
    assert report["hypotheses"] == [] and report["hypothesis"] is None and report["validation"] is None
    assert report["code_evidence"] == []
    assert report["commit_analysis"]["changed_files"][0]["file_path"] == "src/basket.js"  # commit context still shown
    assert not (get_settings().worktrees_dir / f"run-{run['id']}").exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for this test")
def test_no_jira_draft_is_persisted_for_no_bug_detected(client, db_session, history, fake_ollama):
    from sqlalchemy import func, select

    from app.models import Hypothesis, TicketDraft

    repository_id, shas = history
    run = create(client, repository_id, commit_sha=shas["fix"], analysis_command="node scripts/check.js")
    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": "node scripts/check.js"})
    _run_to_completion(client, run["id"])

    response = client.post(f"/debug-runs/{run['id']}/jira-draft")

    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.scalar(select(func.count(TicketDraft.id)).where(TicketDraft.debug_run_id == run["id"])) == 0
    assert db_session.scalar(select(func.count(Hypothesis.id)).where(Hypothesis.debug_run_id == run["id"])) == 0


def test_fixed_commit_without_command_keeps_static_analysis(client, history, fake_ollama):
    repository_id, shas = history
    run = create(client, repository_id, commit_sha=shas["fix"])
    fake_ollama.response = {"hypotheses": [upstream_hypothesis(shas["fix"])]}

    report = _run_to_completion(client, run["id"])

    assert report["conclusion"] is None
    assert len(fake_ollama.prompts) == 1 and "Commits under analysis" in fake_ollama.prompts[0]
    assert [item["hypothesis"]["symbol"] for item in report["hypotheses"]] == ["putLine"]
    assert report["run"]["overall_result"] == "needs_review"  # no runtime confirmation without a command


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for this test")
def test_failing_commit_with_mismatches_still_generates_hypotheses(client, history, fake_ollama):
    repository_id, shas = history
    run = create(client, repository_id, commit_sha=shas["regression"], analysis_command="node scripts/check.js")
    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": "node scripts/check.js"})
    fake_ollama.response = {"hypotheses": [upstream_hypothesis(shas["regression"])]}

    report = _run_to_completion(client, run["id"])

    assert report["runtime_evidence"][0]["exit_code"] == 1
    assert report["runtime_evidence"][0]["data"]["structured"]["mismatched_fields"] == ["gross", "reduction", "net"]
    assert report["conclusion"] is None
    assert len(fake_ollama.prompts) == 1
    assert report["hypotheses"][0]["hypothesis"]["symbol"] == "putLine"
    assert report["run"]["overall_result"] == "validated"
