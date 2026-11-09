from pathlib import Path

from tests.conftest import commit_files


def test_add_clone_index_then_sync_records_commit_metadata(client, local_remote: Path, workspace: Path):
    first_sha = commit_files(
        local_remote,
        {
            "src/a.ts": "export function a() {\n  return 1;\n}\n",
            "README.md": "# Demo\n",
            "node_modules/dep/index.js": "module.exports = 1;\n",
        },
        "first",
    )

    created = client.post("/repositories", json={"url": "https://github.com/acme/demo"})
    assert created.status_code == 202, created.text
    repository_id = created.json()["id"]

    repository = client.get(f"/repositories/{repository_id}").json()
    assert repository["status"] == "ready", repository["status_message"]
    assert repository["branch"] == "main"
    assert repository["current_commit_sha"] == first_sha
    assert repository["indexed_commit_sha"] == first_sha
    assert repository["file_count"] == 2
    assert repository["chunk_count"] >= 2
    assert repository["latest_sync"]["kind"] == "clone"
    checkout = workspace / "repos" / f"{repository_id}-acme__demo"
    assert (checkout / ".git").is_dir()

    no_change = client.post(f"/repositories/{repository_id}/sync")
    assert no_change.status_code == 202
    sync = client.get(f"/repositories/{repository_id}").json()["latest_sync"]
    assert sync["kind"] == "sync" and sync["status"] == "completed"
    assert sync["commit_changed"] is False
    assert sync["previous_commit_sha"] == sync["new_commit_sha"] == first_sha

    second_sha = commit_files(local_remote, {"src/b.ts": "export const b = 2;\n"}, "second")
    client.post(f"/repositories/{repository_id}/sync")
    repository = client.get(f"/repositories/{repository_id}").json()
    sync = repository["latest_sync"]

    assert repository["current_commit_sha"] == second_sha
    assert repository["indexed_commit_sha"] == second_sha
    assert repository["file_count"] == 3
    assert sync["previous_commit_sha"] == first_sha
    assert sync["new_commit_sha"] == second_sha
    assert sync["commit_changed"] is True
    assert sync["changed_files"] == ["src/b.ts"]
    assert sync["index_strategy"] == "full_rebuild"
    assert sync["index_plan"] == {"added": 1, "modified": 0, "deleted": 0, "unchanged": 2}

    history = client.get(f"/repositories/{repository_id}/syncs").json()
    assert [item["kind"] for item in history] == ["sync", "sync", "clone"]


def test_duplicate_repository_is_rejected(client, local_remote: Path):
    commit_files(local_remote, {"a.ts": "export const a = 1;\n"}, "first")
    client.post("/repositories", json={"url": "https://github.com/acme/demo", "branch": "main"})

    response = client.post("/repositories", json={"url": "https://github.com/acme/demo.git", "branch": "main"})

    assert response.status_code == 409


def test_clone_failure_is_reported_on_the_repository(client, monkeypatch, tmp_path):
    from app.services import git_service

    monkeypatch.setattr(git_service, "clone_source", lambda _repository: str(tmp_path / "missing"))

    repository_id = client.post("/repositories", json={"url": "https://github.com/acme/missing"}).json()["id"]
    repository = client.get(f"/repositories/{repository_id}").json()

    assert repository["status"] == "error"
    assert "git clone failed" in repository["status_message"]
    assert repository["latest_sync"]["status"] == "failed"
