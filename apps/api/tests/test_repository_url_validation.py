from pathlib import Path

import pytest

from app.services.git_service import (
    BranchNameError,
    RepositoryUrlError,
    parse_github_url,
    repository_checkout_path,
    validate_branch_name,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/shrivs4/debugpilot-checkout-fixture",
        "https://github.com/shrivs4/debugpilot-checkout-fixture.git",
        "https://github.com/shrivs4/debugpilot-checkout-fixture/",
        "  https://github.com/shrivs4/debugpilot-checkout-fixture  ",
    ],
)
def test_accepts_public_github_urls(url):
    parsed = parse_github_url(url)

    assert parsed.owner == "shrivs4"
    assert parsed.name == "debugpilot-checkout-fixture"
    assert parsed.canonical_url == "https://github.com/shrivs4/debugpilot-checkout-fixture"
    assert parsed.clone_url == "https://github.com/shrivs4/debugpilot-checkout-fixture.git"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://github.com/owner/repo",
        "https://gitlab.com/owner/repo",
        "https://user:token@github.com/owner/repo",
        "git@github.com:owner/repo.git",
        "file:///etc/passwd",
        "/Users/me/projects/repo",
        "https://github.com/owner",
        "https://github.com/owner/repo/tree/main",
        "https://github.com/owner/repo?x=1",
        "https://github.com/owner/..",
        "https://github.com/-owner/repo",
        "https://github.com.evil.com/owner/repo",
    ],
)
def test_rejects_other_urls(url):
    with pytest.raises(RepositoryUrlError):
        parse_github_url(url)


@pytest.mark.parametrize("branch", ["main", "feature/legacy-coupons", "release-1.2", "v2_fix"])
def test_accepts_valid_branches(branch):
    assert validate_branch_name(branch) == branch


@pytest.mark.parametrize("branch", [None, "", "   "])
def test_empty_branch_means_default(branch):
    assert validate_branch_name(branch) is None


@pytest.mark.parametrize("branch", ["-x", "a..b", "a b", "a;rm", "a/", ".hidden", "x.lock", "a//b", "$(id)"])
def test_rejects_invalid_branches(branch):
    with pytest.raises(BranchNameError):
        validate_branch_name(branch)


def test_checkout_path_is_server_derived_inside_workspace(tmp_path: Path):
    parsed = parse_github_url("https://github.com/shrivs4/debugpilot-checkout-fixture")

    path = repository_checkout_path(tmp_path, 7, parsed)

    assert path.parent == tmp_path.resolve()
    assert path.name == "7-shrivs4__debugpilot-checkout-fixture"


def test_add_repository_endpoint_rejects_invalid_url(client):
    response = client.post("/repositories", json={"url": "https://example.com/a/b"})

    assert response.status_code == 422
    assert "github.com" in response.json()["detail"]
