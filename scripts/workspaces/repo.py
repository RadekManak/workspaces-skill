"""Repo management: selecting, adding, removing, and refreshing workspace repos.

Covers the workspace-scoped repo lifecycle that sits on top of git worktrees:
picking a repo out of a workspace's repo list, creating a new worktree from a
source repo and recording it on the workspace, tearing a linked worktree back
down, and refreshing a repo's recorded git metadata (branch, dirty, remotes)
from disk.
"""
from pathlib import Path

from workspaces.common import run
from workspaces.git import git_info, remove_linked_worktree
from workspaces.ledger import WorkspaceLedger


def select_repo(workspace, repo_name=None):
    repos = workspace.repos
    if repo_name:
        return workspace.find_repo(repo_name)
    if not repos:
        raise SystemExit("Workspace has no repos. Add or adopt one before running Sandcastle.")
    if len(repos) > 1:
        names = ", ".join(repo.get("name") or "?" for repo in repos)
        raise SystemExit(f"Workspace has multiple repos ({names}). Pass --repo.")
    return repos[0]


def add_source_repo_to_workspace(config, workspace, repo_name, branch=None):
    workspace_id = workspace.id
    workspace_root = WorkspaceLedger(config).workspace_root_path(workspace_id)
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / ".workspace-id").write_text(workspace_id + "\n", encoding="utf-8")
    source = Path(config["source_root"]) / repo_name
    dest = workspace_root / repo_name
    if not source.exists():
        raise SystemExit(f"Source repo not found: {source}")
    if not dest.exists():
        base_ref = f"{config['base_remote']}/{config['base_branch']}"
        # Refresh the local remote-tracking ref so new worktrees are not based on a stale tip.
        run(
            [
                "git",
                "-C",
                str(source),
                "fetch",
                config["base_remote"],
                config["base_branch"],
            ],
            capture=True,
        )
        run(
            [
                "git",
                "-C",
                str(source),
                "worktree",
                "add",
                "-b",
                branch or workspace_id,
                str(dest),
                base_ref,
            ],
            capture=True,
        )
    info = git_info(dest, config)
    workspace.upsert_repo(
        {
            "name": repo_name,
            "sourcePath": str(source),
            "worktreePath": str(dest),
            "branch": info["branch"],
            "baseRemote": config["base_remote"],
            "baseBranch": config["base_branch"],
            "pushRemote": config["push_remote"],
            "remote": info["remote"],
            "forkRemote": info["forkRemote"],
        },
    )
    return dest


def remove_repo_worktree(config, workspace, repo):
    remove_linked_worktree(
        repo.get("worktreePath"),
        WorkspaceLedger(config).workspace_root_path(workspace.id),
        source_path=repo.get("sourcePath"),
    )


def refresh_repo(config, repo):
    path = repo.get("worktreePath")
    if not path or not Path(path).exists():
        return repo
    info = git_info(path, config)
    return {
        **repo,
        "name": repo.get("name") or info["name"],
        "worktreePath": info["root"],
        "branch": info["branch"],
        "baseRemote": repo.get("baseRemote") or config["base_remote"],
        "baseBranch": repo.get("baseBranch") or config["base_branch"],
        "pushRemote": repo.get("pushRemote") or config["push_remote"],
        "remote": info["remote"],
        "forkRemote": info["forkRemote"],
    }
