"""GitHub PR sync: gathering `gh pr list` snapshots and PR lifecycle events.

Covers the two ways a workspace's recorded GitHub PR state changes: bulk
snapshot gathering by shelling out to `gh pr list` per repo (`sync_prs`), and
recording a single lifecycle event such as opened/feedback/merged along with
its associated PR state transition (`apply_pr_event`).

`refresh_recorded_prs` re-checks already-recorded snapshots via `gh pr view`
so cleanup decisions are not stuck on stale OPEN entries.

A failing or unparseable `gh` invocation is never treated as "this repo has no
PRs": it is reported to the caller (and to stderr) so an auth/network problem
cannot silently look like a clean, PR-free workspace.
"""
import json
import re
import shutil
from pathlib import Path

from workspaces.common import now, project, run, warn
from workspaces.git import git_info, github_repo_from_remote


_GH_TIMEOUT_SECONDS = 60
_PR_VIEW_FIELDS = "number,url,state,mergedAt,title,headRefName"
_PR_SNAPSHOT_FIELDS = ("number", "url", "branch", "state", "merged", "title")
_PR_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>\d+)",
    re.IGNORECASE,
)


def _gh_json(cmd):
    """Run a `gh ... --json` command, returning its decoded payload.

    Raises `SystemExit` with the command's stderr when `gh` fails, when it does
    not finish within `_GH_TIMEOUT_SECONDS`, and with the decode error when it
    succeeds but returns something that is not JSON.
    """
    raw = run(cmd, timeout=_GH_TIMEOUT_SECONDS)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise SystemExit(f"Could not parse `{' '.join(cmd)}` output as JSON: {error}") from error


def sync_prs(config, workspace):
    """Gather `gh pr list` PR snapshots for each of `workspace`'s repos.

    Requires `gh` on PATH (raises `SystemExit` if missing, since that's a
    precondition of gathering rather than of CLI wiring). Records each seen
    PR onto `workspace` via `workspace.record_github_pr(...)`, mutating
    `workspace` in place.

    Returns `(seen, failures)`: the list of seen PR dict items, and one
    `{"repo", "error"}` entry per repo whose `gh` query failed. Failures are
    per-repo so one broken repo still lets the others sync, but they are never
    swallowed -- callers are expected to surface them.
    """
    if shutil.which("gh") is None:
        raise SystemExit("gh is not installed or not on PATH")
    seen = []
    failures = []
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
        try:
            prs = _gh_json(
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
                ]
            ) or []
        except SystemExit as error:
            warn(f"Could not sync PRs for repo `{repo.get('name')}`: {error}")
            failures.append({"repo": repo.get("name"), "error": str(error)})
            continue
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
    return seen, failures


def _pr_view_command(pr):
    """Build `gh pr view` argv for a recorded snapshot, or None if underspecified."""
    url = (pr.get("url") or "").strip()
    if url:
        return ["gh", "pr", "view", url, "--json", _PR_VIEW_FIELDS]
    number = pr.get("number")
    repo = (pr.get("repo") or "").strip()
    if number is None:
        return None
    match = _PR_URL_RE.search(repo) if "://" in repo else None
    if match:
        repo = match.group("repo")
    if "/" not in repo:
        return None
    return ["gh", "pr", "view", str(number), "--repo", repo, "--json", _PR_VIEW_FIELDS]


def refresh_recorded_prs(workspace):
    """Refresh recorded `githubPrs` snapshots via `gh pr view`.

    Soft-fails when `gh` is missing or a view fails: leaves that snapshot
    unchanged, but warns on stderr so a stale OPEN snapshot is never mistaken
    for a freshly confirmed one. Returns True if any snapshot was updated.
    """
    if shutil.which("gh") is None:
        warn("gh is not installed or not on PATH; recorded PR snapshots were not refreshed")
        return False
    changed = False
    for index, pr in enumerate(workspace.github_prs):
        cmd = _pr_view_command(pr)
        if not cmd:
            warn(f"Skipping PR snapshot without a usable url/repo+number: {pr}")
            continue
        try:
            live = _gh_json(cmd)
        except SystemExit as error:
            warn(f"Could not refresh PR snapshot {pr.get('url') or pr.get('number')}: {error}")
            continue
        if not isinstance(live, dict) or live.get("number") is None:
            warn(f"Unexpected `gh pr view` payload for {pr.get('url') or pr.get('number')}: {live!r}")
            continue
        state = live.get("state")
        merged = bool(live.get("mergedAt")) or str(state or "").upper() == "MERGED"
        item = {
            "number": live.get("number"),
            "url": live.get("url") or pr.get("url"),
            "branch": live.get("headRefName") or pr.get("branch"),
            "state": state,
            "merged": merged,
            "lastSeenAt": now(),
            "title": live.get("title"),
        }
        before = {**project(pr, *_PR_SNAPSHOT_FIELDS), "merged": bool(pr.get("merged"))}
        after = project(item, *_PR_SNAPSHOT_FIELDS)
        if before == after:
            continue
        workspace.update_github_pr_snapshot(index, item)
        changed = True
    return changed


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
