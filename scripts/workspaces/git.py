import subprocess
from pathlib import Path
from urllib.parse import urlparse

from .common import run


def path_is_under(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def repo_name_from_remote(remote_url):
    remote_url = remote_url.strip()
    if remote_url.startswith("git@"):
        _, rest = remote_url.split(":", 1)
        return rest.removesuffix(".git")
    if remote_url.startswith("https://") or remote_url.startswith("ssh://"):
        parsed = urlparse(remote_url)
        return parsed.path.strip("/").removesuffix(".git")
    return remote_url.removesuffix(".git")


def github_repo_from_remote(remote_url):
    name = repo_name_from_remote(remote_url)
    if "/" in name:
        return name
    return None


def git_root(path):
    try:
        root = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
        return Path(root)
    except SystemExit:
        return None


def git_info(path, config):
    root = git_root(path)
    if root is None:
        raise SystemExit(f"Not inside a git repo: {path}")
    branch = run(["git", "-C", str(root), "branch", "--show-current"], check=False)
    origin = run(["git", "-C", str(root), "remote", "get-url", "origin"], check=False)
    fork = run(["git", "-C", str(root), "remote", "get-url", config["push_remote"]], check=False)
    status = run(["git", "-C", str(root), "status", "--short"], check=False)
    ahead = run(
        [
            "git",
            "-C",
            str(root),
            "rev-list",
            "--count",
            f"{config['base_remote']}/{config['base_branch']}..HEAD",
        ],
        check=False,
    )
    return {
        "root": str(root),
        "name": root.name,
        "branch": branch or None,
        "remote": origin or None,
        "forkRemote": fork or None,
        "dirty": bool(status),
        "aheadOfBase": int(ahead) if ahead.isdigit() else None,
    }


def git_success(path, args):
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 0


def git_resolved_path(path, arg):
    output = run(["git", "-C", str(path), "rev-parse", arg], check=False)
    if not output:
        return None
    candidate = Path(output)
    if not candidate.is_absolute():
        candidate = Path(path) / candidate
    return candidate.resolve()


def is_linked_worktree(path):
    git_dir = git_resolved_path(path, "--git-dir")
    common_dir = git_resolved_path(path, "--git-common-dir")
    if not git_dir or not common_dir:
        return False
    return git_dir != common_dir


def git_worktrees(path):
    output = run(["git", "-C", str(path), "worktree", "list", "--porcelain"], check=False)
    entries = []
    current = {}
    for line in output.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"path": line.removeprefix("worktree ").strip(), "branch": None}
        elif line.startswith("branch ") and current:
            ref = line.removeprefix("branch ").strip()
            current["branch"] = ref.removeprefix("refs/heads/")
    if current:
        entries.append(current)
    return entries


def worktree_for_branch(path, branch):
    for entry in git_worktrees(path):
        if entry.get("branch") == branch:
            return entry.get("path")
    return None


def branch_exists(path, branch):
    return git_success(path, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])


def branch_merged(path, branch, target_branch):
    if not branch_exists(path, branch):
        return None
    if not target_branch:
        return False
    if not branch_exists(path, target_branch):
        return False
    return git_success(path, ["merge-base", "--is-ancestor", branch, target_branch])


def worktree_dirty(path):
    return bool(run(["git", "-C", str(path), "status", "--porcelain"], check=False))


def remove_linked_worktree(path, workspace_root, *, source_path=None, force=False):
    if not path or not Path(path).exists():
        return
    if not is_linked_worktree(path):
        raise SystemExit(f"Refusing to delete non-linked repo checkout: {path}")
    if not path_is_under(path, workspace_root):
        raise SystemExit(f"Refusing to delete repo outside workspace root: {path}")
    if not force and worktree_dirty(path):
        raise SystemExit(f"Refusing to delete dirty repo worktree: {path}")
    git_cwd = source_path if source_path and Path(source_path).exists() else path
    remove_args = ["git", "-C", str(git_cwd), "worktree", "remove", str(path)]
    if force:
        remove_args.append("--force")
    run(remove_args)
