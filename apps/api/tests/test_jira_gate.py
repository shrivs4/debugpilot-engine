"""The mock Jira draft is only created for an explicitly identified, validated hypothesis."""

import pytest
from sqlalchemy import func, select

from app.models import DebugRun, Hypothesis, Repository, TicketDraft, ValidationResult


@pytest.fixture
def make_run(db_session):
    def make(*results: str | None) -> tuple[int, list[int]]:
        name = f"gate{db_session.scalar(select(func.count(Repository.id)))}"
        repository = Repository(url=f"https://github.com/acme/{name}", owner="acme", name=name, status="ready")
        db_session.add(repository)
        db_session.flush()
        run = DebugRun(
            repository_id=repository.id, issue_description="Totals are wrong", analysis_command="node check.js",
            status="completed", overall_result=results[0] if results else None,
        )
        db_session.add(run)
        db_session.flush()
        ids = []
        for rank, result in enumerate(results, start=1):
            hypothesis = Hypothesis(
                debug_run_id=run.id, model="fake", prompt="p", raw_response="{}", summary=f"Hypothesis {rank}",
                root_cause="cause", file_path="src/a.js", line_number=rank, symbol=f"fn{rank}", rank=rank, parse_ok=True,
            )
            db_session.add(hypothesis)
            db_session.flush()
            if result is not None:
                db_session.add(ValidationResult(debug_run_id=run.id, hypothesis_id=hypothesis.id, overall_result=result, checks=[]))
            ids.append(hypothesis.id)
        db_session.commit()
        return run.id, ids

    return make


def draft_count(db_session, run_id: int) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count(TicketDraft.id)).where(TicketDraft.debug_run_id == run_id))


def test_validated_hypothesis_creates_a_draft(client, db_session, make_run):
    run_id, [hypothesis_id] = make_run("validated")

    response = client.post(f"/debug-runs/{run_id}/jira-draft")

    assert response.status_code == 201, response.text
    ticket = response.json()
    assert ticket["ticket_key"] == f"MOCK-{run_id}"
    assert ticket["validation_status"] == "validated"
    assert ticket["hypothesis_id"] == hypothesis_id
    assert draft_count(db_session, run_id) == 1


@pytest.mark.parametrize("result", ["needs_review", "rejected", None])
def test_unvalidated_hypothesis_does_not_create_a_draft(client, db_session, make_run, result):
    run_id, _ = make_run(result)

    response = client.post(f"/debug-runs/{run_id}/jira-draft")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail.startswith("Human review required")
    assert "only created for a validated hypothesis" in detail
    assert draft_count(db_session, run_id) == 0


def test_multiple_hypotheses_require_an_explicit_validated_selection(client, db_session, make_run):
    run_id, [first, second] = make_run("needs_review", "rejected")

    implicit = client.post(f"/debug-runs/{run_id}/jira-draft")
    assert implicit.status_code == 422
    assert "Select one explicitly" in implicit.json()["detail"]
    assert "(0 validated)" in implicit.json()["detail"]

    for hypothesis_id, label in ((first, "needs review"), (second, "rejected")):
        response = client.post(f"/debug-runs/{run_id}/jira-draft", json={"hypothesis_id": hypothesis_id})
        assert response.status_code == 409
        assert label in response.json()["detail"]
    assert draft_count(db_session, run_id) == 0


def test_multiple_hypotheses_never_pick_one_implicitly(client, db_session, make_run):
    run_id, [first, second] = make_run("needs_review", "validated")

    assert client.post(f"/debug-runs/{run_id}/jira-draft").status_code == 422
    assert draft_count(db_session, run_id) == 0

    response = client.post(f"/debug-runs/{run_id}/jira-draft", json={"hypothesis_id": second})
    assert response.status_code == 201
    assert response.json()["hypothesis_id"] == second
    assert response.json()["summary"] == "Hypothesis 2"


def test_hypothesis_from_another_run_is_not_found(client, make_run):
    run_id, _ = make_run("validated")
    _, [foreign] = make_run("validated")

    response = client.post(f"/debug-runs/{run_id}/jira-draft", json={"hypothesis_id": foreign})

    assert response.status_code == 404


def test_failed_gate_keeps_the_previous_draft(client, db_session, make_run):
    run_id, [hypothesis_id] = make_run("validated")
    assert client.post(f"/debug-runs/{run_id}/jira-draft").status_code == 201
    # A later re-validation downgrades the hypothesis; its latest result is what counts.
    db_session.add(ValidationResult(debug_run_id=run_id, hypothesis_id=hypothesis_id, overall_result="needs_review", checks=[]))
    db_session.commit()

    assert client.post(f"/debug-runs/{run_id}/jira-draft").status_code == 409
    assert draft_count(db_session, run_id) == 1


@pytest.mark.parametrize("result", ["needs_review", "rejected"])
def test_direct_request_naming_an_unvalidated_hypothesis_is_refused(client, db_session, make_run, result):
    # A validated hypothesis exists, but the request explicitly names the other one.
    run_id, [validated_id, other_id] = make_run("validated", result)

    response = client.post(f"/debug-runs/{run_id}/jira-draft", json={"hypothesis_id": other_id})

    assert response.status_code == 409
    assert "Human review required" in response.json()["detail"]
    assert result.replace("_", " ") in response.json()["detail"]
    assert draft_count(db_session, run_id) == 0
    assert client.get(f"/debug-runs/{run_id}/report").json()["ticket_draft"] is None

    # The validated hypothesis still works as before.
    created = client.post(f"/debug-runs/{run_id}/jira-draft", json={"hypothesis_id": validated_id})
    assert created.status_code == 201 and created.json()["hypothesis_id"] == validated_id
