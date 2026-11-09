from app.models import Repository


def make_ready_repository(db_session, workspace) -> Repository:
    repository = Repository(
        url="https://github.com/acme/demo",
        owner="acme",
        name="demo",
        branch="main",
        local_path=str(workspace / "repos" / "1-acme__demo"),
        status="ready",
        current_commit_sha="a" * 40,
    )
    db_session.add(repository)
    db_session.commit()
    return repository


def create_run(client, repository_id, setup="npm install", analysis="npm run repro:legacy"):
    response = client.post(
        "/debug-runs",
        json={
            "repository_id": repository_id,
            "issue_description": "Checkout fails for legacy coupons",
            "setup_command": setup,
            "analysis_command": analysis,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_new_run_is_a_draft_with_no_approvals(client, db_session, workspace):
    repository = make_ready_repository(db_session, workspace)

    run = create_run(client, repository.id)

    assert run["status"] == "draft"
    assert run["setup_command_approved_at"] is None
    assert run["analysis_command_approved_at"] is None
    assert run["ready_to_run"] is False


def test_run_refused_until_every_command_is_approved(client, db_session, workspace):
    repository = make_ready_repository(db_session, workspace)
    run = create_run(client, repository.id)

    response = client.post(f"/debug-runs/{run['id']}/run")
    assert response.status_code == 409
    assert "setup command has not been approved" in response.json()["detail"]

    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "setup", "command": "npm install"})
    response = client.post(f"/debug-runs/{run['id']}/run")
    assert response.status_code == 409
    assert "analysis command has not been approved" in response.json()["detail"]

    approved = client.post(
        f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": "npm run repro:legacy"}
    ).json()
    assert approved["ready_to_run"] is True
    assert approved["status"] == "draft"


def test_approval_must_match_the_stored_command_text(client, db_session, workspace):
    repository = make_ready_repository(db_session, workspace)
    run = create_run(client, repository.id)

    response = client.post(
        f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": "npm run something-else"}
    )

    assert response.status_code == 400
    assert client.get(f"/debug-runs/{run['id']}").json()["analysis_command_approved_at"] is None


def test_setup_command_is_optional(client, db_session, workspace):
    repository = make_ready_repository(db_session, workspace)
    run = create_run(client, repository.id, setup=None)

    missing = client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "setup", "command": "npm install"})
    approved = client.post(
        f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": "npm run repro:legacy"}
    )

    assert missing.status_code == 400
    assert approved.json()["ready_to_run"] is True


def test_commands_with_shell_operators_are_rejected_at_creation(client, db_session, workspace):
    repository = make_ready_repository(db_session, workspace)

    response = client.post(
        "/debug-runs",
        json={
            "repository_id": repository.id,
            "issue_description": "x fails",
            "analysis_command": "npm install && npm run repro:legacy",
        },
    )

    assert response.status_code == 422
    assert "Shell operators" in response.json()["detail"]


def test_run_refused_when_ollama_is_unavailable(client, db_session, workspace, fake_ollama):
    repository = make_ready_repository(db_session, workspace)
    run = create_run(client, repository.id, setup=None)
    client.post(f"/debug-runs/{run['id']}/approve-command", json={"kind": "analysis", "command": "npm run repro:legacy"})
    fake_ollama.available = False

    response = client.post(f"/debug-runs/{run['id']}/run")

    assert response.status_code == 503
    assert "Ollama is not reachable" in response.json()["detail"]
    assert client.get(f"/debug-runs/{run['id']}").json()["status"] == "draft"
