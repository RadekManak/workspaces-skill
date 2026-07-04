"""GitHub PR sync: gathering `gh pr list` snapshots and PR lifecycle events.

Covers the two ways a workspace's recorded GitHub PR state changes: bulk
snapshot gathering by shelling out to `gh pr list` per repo (`sync_prs`), and
recording a single lifecycle event such as opened/feedback/merged along with
its associated PR state transition (`apply_pr_event`).
"""
import json
import shutil
from pathlib import Path

from workspaces.common import now, run
from workspaces.git import git_info, github_repo_from_remote


def sync_prs(config, workspace):
    """Gather `gh pr list` PR snapshots for each of `workspace`'s repos.

    Requires `gh` on PATH (raises `SystemExit` if missing, since that's a
    precondition of gathering rather than of CLI wiring). Records each seen
    PR onto `workspace` via `workspace.record_github_pr(...)`, mutating
    `workspace` in place, and returns the list of seen PR dict items.
    """
    if shutil.which("gh") is None:
        raise SystemExit("gh is not installed or not on PATH")
    seen = []
    for repo in workspace.repos:
        path = repo.get("worktreePath")
        if not path or not Path(path).exists():
            continue
        info = git_info(path, config)
        branch = info["branch"]
        remote = info["remote"]
        full_name = github_repo_from_remote(remote or "")
        if not branch or not full_name:
            continue
        raw = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                full_name,
                "--head",
                branch,
                "--json",
                "number,url,state,isDraft,headRefName,baseRefName,mergedAt,title",
                "--limit",
                "10",
            ],
            check=False,
        )
        try:
            prs = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            prs = []
        for pr in prs:
            item = {
                "repo": repo.get("name"),
                "number": pr.get("number"),
                "url": pr.get("url"),
                "branch": pr.get("headRefName") or branch,
                "state": pr.get("state"),
                "merged": bool(pr.get("mergedAt")),
                "lastSeenAt": now(),
                "title": pr.get("title"),
            }
            seen.append(item)
    for item in seen:
        workspace.record_github_pr(item)
    return seen


def apply_pr_event(workspace, event, url=None, number=None):
    """Apply a PR lifecycle event to `workspace`, mutating it in place.

    Maps `event` (opened/feedback/merged) to a workspace state and sets it.
    If `url` or `number` is given, also appends a PR entry reflecting the
    event onto `workspace`.
    """
    state_by_event = {
        "opened": "pr-review",
        "feedback": "review-feedback",
        "merged": "dev-complete",
    }
    workspace.set_state(state_by_event[event])
    if url or number:
        item = {
            "url": url,
            "number": number,
            "state": "MERGED" if event == "merged" else "OPEN",
            "merged": event == "merged",
            "lastSeenAt": now(),
        }
        workspace.append_github_pr({key: value for key, value in item.items() if value is not None})
