"""Git operations: URL validation, controlled clones, sync, and evidence commands.

The evidence helpers are derived from poc/collect_git_evidence.py. All git
invocations use argument lists (never a shell), disable interactive credential
prompts, and run inside paths the server derived itself.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


GITHUB_URL_PATTERN = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<name>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?$"
)

BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class RepositoryUrlError(ValueError):
    pass


class BranchNameError(ValueError):
    pass


class GitCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    name: str

    @property
    def canonical_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"

    @property
    def clone_url(self) -> str:
        return f"{self.canonical_url}.git"


def parse_github_url(url: str) -> GitHubRepository:
    """Accept only `https://github.com/<owner>/<repo>` (optionally `.git` / trailing slash)."""
    candidate = (url or "").strip()
    match = GITHUB_URL_PATTERN.match(candidate)
    if match is None:
        raise RepositoryUrlError(
            "Only public GitHub repository URLs of the form https://github.com/<owner>/<repo> are supported."
        )
    name = match.group("name")
    if name in {".", ".."} or name.startswith("."):
        raise RepositoryUrlError("Repository name is not valid.")
    return GitHubRepository(owner=match.group("owner"), name=name)


def validate_branch_name(branch: str | None) -> str | None:
    if branch is None or not branch.strip():
        return None
    branch = branch.strip()
    invalid = (
        not BRANCH_PATTERN.match(branch)
        or branch.startswith(("-", "/", "."))
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "//" in branch
        or "/." in branch
    )
    if invalid:
        raise BranchNameError(f"'{branch}' is not a valid branch name.")
    return branch


def clone_source(repository: GitHubRepository) -> str:
    """Where to clone from. Isolated so tests can point at a local fixture repository."""
    return repository.clone_url


def repository_checkout_path(repositories_dir: Path, repository_id: int, repository: GitHubRepository) -> Path:
    """Server-derived clone location; never influenced by user-supplied paths."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{repository.owner}__{repository.name}")
    root = repositories_dir.resolve()
    path = (root / f"{repository_id}-{safe_name}").resolve()
    if path.parent != root:
        raise RepositoryUrlError("Refusing to use a checkout path outside the workspace.")
    return path


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "TMPDIR"}}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GCM_INTERACTIVE": "never"})
    return env


def run_git(arguments: list[str], cwd: Path | None = None, timeout: int = 300) -> str:
    command = ["git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise GitCommandError(f"git {arguments[0]} timed out after {timeout}s") from error
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        if "could not read Username" in stderr or "Authentication failed" in stderr or "not found" in stderr:
            stderr += " (the repository may be private or may not exist; only public repositories are supported)"
        raise GitCommandError(f"git {arguments[0]} failed: {stderr}")
    return completed.stdout


def clone(source: str, destination: Path, branch: str | None, timeout: int = 300) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    arguments = ["clone", "--no-tags"]
    if branch:
        arguments += ["--branch", branch, "--single-branch"]
    run_git([*arguments, "--", source, str(destination)], timeout=timeout)


def current_branch(path: Path) -> str:
    return run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path).strip()


def head_sha(path: Path) -> str:
    return run_git(["rev-parse", "HEAD"], cwd=path).strip()


def fetch_and_reset(path: Path, branch: str, timeout: int = 300) -> None:
    """Move the managed clone to the remote branch tip. Untracked files (node_modules) are kept."""
    run_git(["fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd=path, timeout=timeout)
    run_git(["checkout", "-B", branch, f"refs/remotes/origin/{branch}"], cwd=path)
    run_git(["reset", "--hard", f"refs/remotes/origin/{branch}"], cwd=path)


def changed_files(path: Path, old_sha: str, new_sha: str) -> list[str]:
    output = run_git(["diff", "--name-only", old_sha, new_sha], cwd=path)
    return [line for line in output.splitlines() if line]


def show_stat(path: Path, revision: str) -> str:
    return run_git(["show", "--stat", "--format=fuller", revision], cwd=path)


def show_diff(path: Path, revision: str, files: list[str]) -> str:
    return run_git(["show", "--format=%H %s", revision, "--", *files], cwd=path)


def blame(path: Path, file_path: str, start_line: int, end_line: int) -> str:
    return run_git(["blame", "-L", f"{start_line},{end_line}", "--", file_path], cwd=path)


def blame_porcelain(path: Path, file_path: str, start_line: int, end_line: int) -> list[dict]:
    """Return one entry per line: {line, commit, author, summary}."""
    output = run_git(["blame", "--line-porcelain", "-L", f"{start_line},{end_line}", "--", file_path], cwd=path)
    entries: list[dict] = []
    current: dict = {}
    for line in output.splitlines():
        header = re.match(r"^([0-9a-f]{40}) \d+ (\d+)", line)
        if header:
            current = {"commit": header.group(1), "line": int(header.group(2))}
        elif line.startswith("author "):
            current["author"] = line[len("author "):]
        elif line.startswith("summary "):
            current["summary"] = line[len("summary "):]
        elif line.startswith("\t"):
            current["content"] = line[1:]
            entries.append(current)
    return entries


def file_history(path: Path, file_path: str, limit: int = 10) -> list[dict]:
    output = run_git(
        ["log", f"-n{limit}", "--date=iso-strict", "--format=%H%x09%an%x09%ad%x09%s", "--", file_path],
        cwd=path,
    )
    history = []
    for line in output.splitlines():
        sha, author, date, subject = (line.split("\t", 3) + ["", "", ""])[:4]
        history.append({"commit": sha, "author": author, "date": date, "subject": subject})
    return history


SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def resolve_commit(path: Path, sha: str) -> str:
    """Full hash of a commit given as a 7-40 character hex SHA. Refs and options are rejected."""
    sha = (sha or "").strip()
    if not SHA_PATTERN.match(sha):
        raise GitCommandError(f"'{sha}' is not a commit SHA (7-40 hexadecimal characters).")
    return run_git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], cwd=path).strip()


def list_commits(path: Path, limit: int = 50) -> list[dict]:
    output = run_git(["log", f"-n{limit}", "--date=iso-strict", "--format=%H%x09%P%x09%an%x09%ad%x09%s"], cwd=path)
    commits = []
    for line in output.splitlines():
        sha, parents, author, date, subject = (line.split("\t", 4) + ["", "", "", ""])[:5]
        commits.append(
            {"sha": sha, "short": sha[:8], "parents": parents.split(), "author": author, "date": date, "subject": subject}
        )
    return commits


def parent_of(path: Path, sha: str) -> str | None:
    parents = run_git(["rev-list", "--parents", "-n1", sha], cwd=path).split()[1:]
    return parents[0] if parents else None


def is_ancestor(path: Path, ancestor: str, descendant: str) -> bool:
    try:
        run_git(["merge-base", "--is-ancestor", ancestor, descendant], cwd=path)
        return True
    except GitCommandError:
        return False


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def range_commits(path: Path, base: str | None, head: str) -> list[dict]:
    revision = f"{base}..{head}" if base else head
    arguments = ["log", "--date=iso-strict", "--format=%H%x09%an%x09%ad%x09%s", revision]
    if not base:
        arguments.insert(1, "-n1")
    commits = []
    for line in run_git(arguments, cwd=path).splitlines():
        sha, author, date, subject = (line.split("\t", 3) + ["", "", ""])[:4]
        commits.append({"sha": sha, "short": sha[:8], "author": author, "date": date, "subject": subject})
    return commits


def range_diff(path: Path, base: str | None, head: str, context_lines: int = 3) -> str:
    return run_git(["diff", f"-U{context_lines}", "--no-color", base or EMPTY_TREE, head, "--"], cwd=path)


def range_changed_files(path: Path, base: str | None, head: str) -> list[dict]:
    output = run_git(["diff", "--name-status", "--no-renames", base or EMPTY_TREE, head, "--"], cwd=path)
    files = []
    for line in output.splitlines():
        status, _, file_path = line.partition("\t")
        if file_path:
            files.append({"status": status[:1], "file_path": file_path})
    return files


def changed_line_ranges(path: Path, base: str | None, head: str) -> dict[str, list[tuple[int, int]]]:
    """New-side line ranges touched by the diff, per file (pure deletions map to the following line)."""
    output = run_git(["diff", "-U0", "--no-color", "--no-renames", base or EMPTY_TREE, head, "--"], cwd=path)
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
            continue
        match = HUNK_HEADER.match(line)
        if match and current:
            start = int(match.group("start"))
            count = int(match.group("count")) if match.group("count") is not None else 1
            ranges.setdefault(current, []).append((max(1, start), max(1, start + max(count, 1) - 1)))
    return ranges


def file_at(path: Path, revision: str, file_path: str) -> str | None:
    try:
        return run_git(["show", f"{revision}:{file_path}"], cwd=path)
    except GitCommandError:
        return None


def recent_changed_files(path: Path, limit_commits: int = 10) -> dict[str, float]:
    """Files touched by the last N commits, weighted by recency (1.0 for HEAD, decaying)."""
    output = run_git(["log", f"-n{limit_commits}", "--name-only", "--format=%x00%H"], cwd=path)
    weights: dict[str, float] = {}
    for index, block in enumerate(part for part in output.split("\x00") if part.strip()):
        weight = round(1.0 / (1 + index * 0.5), 4)
        for file_path in block.splitlines()[1:]:
            if file_path.strip():
                weights.setdefault(file_path.strip(), weight)
    return weights


def add_worktree(path: Path, destination: Path, revision: str) -> None:
    if destination.exists():
        remove_worktree(path, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "add", "--detach", "--force", str(destination), revision], cwd=path)


def remove_worktree(path: Path, destination: Path) -> None:
    try:
        run_git(["worktree", "remove", "--force", str(destination)], cwd=path)
    except GitCommandError:
        shutil.rmtree(destination, ignore_errors=True)
        run_git(["worktree", "prune"], cwd=path)
