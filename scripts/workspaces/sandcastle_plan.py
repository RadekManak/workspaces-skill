"""Sandcastle planning: multi-repo classification and dependency-ordered execution graph."""
import datetime as dt
import hashlib
import json

from .common import now, repo_identity_snapshot, repo_projection, write_json
from .issues import (
    ISSUE_ACTIVE_STATUSES,
    ISSUE_DONE_STATUSES,
    ISSUE_TYPES,
    blockers_complete,
    issue_index,
    issue_payload,
    resolve_issue_repo,
    sandcastle_block,
    sandcastle_issue_meta,
    write_issue,
)
from .ledger import WorkspaceLedger


def repo_names(repos):
    return {str(repo.get("name") or "") for repo in repos}


def select_plan_repos(workspace, repo_name=None):
    repos = workspace.repos
    if not repos:
        raise SystemExit("Workspace has no repos. Add or adopt one before running Sandcastle.")
    if repo_name:
        for repo in repos:
            if repo.get("name") == repo_name:
                return [repo]
        raise SystemExit(f"Repo not found in workspace: {repo_name}")
    return list(repos)


def _blocker_reason(index, issue):
    blockers = []
    for blocker_id in issue["meta"].get("blockedBy") or []:
        blocker = index.get(str(blocker_id))
        if not blocker:
            blockers.append(f"missing blocker `{blocker_id}`")
        elif blocker["meta"].get("status") not in ISSUE_DONE_STATUSES:
            blockers.append(
                f"blocker `{blocker_id}` status `{blocker['meta'].get('status')}`"
            )
    return blockers


def classify_issue(index, issue, all_repos, scope_repo_set, *, sandcastle=True):
    """Classify an issue for planning display.

    `all_repos` is the FULL workspace repo list, used to resolve which repo
    an issue belongs to (including the single-repo-workspace default).
    `scope_repo_set` is the set of repo names actually in scope for this
    planning invocation (narrowed by `--repo`, if given). These are kept
    distinct so that narrowing scope via `--repo` never changes which repo
    an unset-`repo:` issue resolves to, and never gets misreported as
    "unknown repo" merely because it belongs to a real repo outside scope.
    """
    meta = issue["meta"]
    status = meta.get("status")
    issue_type = sandcastle_issue_meta(meta)["type"] if sandcastle else "AFK"
    repo = resolve_issue_repo(meta, all_repos)

    if repo is None:
        if meta.get("repo") is not None:
            reason = f"unknown repo `{meta.get('repo')}`"
        elif len(all_repos) > 1:
            reason = "repo not specified"
        else:
            reason = "repo could not be resolved"
        return "blocked", reason, None

    if repo not in scope_repo_set:
        return "out_of_scope", f"repo `{repo}` not in planning scope", repo

    if status not in ISSUE_ACTIVE_STATUSES:
        return "blocked", f"inactive status `{status}`", repo

    if not blockers_complete(index, issue):
        blockers = _blocker_reason(index, issue)
        return "blocked", "; ".join(blockers) if blockers else "blockers incomplete", repo

    if sandcastle:
        if issue_type not in ISSUE_TYPES:
            return "blocked", f"invalid type `{issue_type}`", repo
        if issue_type == "HITL":
            return "ready_not_targeted", "HITL issue", repo

    return "ready_afk", None, repo


def build_execution_waves(targeted_entries):
    """Return dependency-ordered waves spanning targeted issues across repos."""
    ids = {entry["id"] for entry in targeted_entries}
    blockers_in_plan = {}
    for entry in targeted_entries:
        blockers_in_plan[entry["id"]] = [
            str(blocker_id)
            for blocker_id in entry.get("blockedBy") or []
            if str(blocker_id) in ids
        ]

    waves = []
    remaining = set(ids)
    while remaining:
        ready = sorted(
            issue_id
            for issue_id in remaining
            if all(blocker not in remaining for blocker in blockers_in_plan[issue_id])
        )
        if not ready:
            raise SystemExit("Cycle detected in targeted issue dependency graph.")
        wave = [entry for entry in targeted_entries if entry["id"] in ready]
        wave.sort(key=lambda item: item["id"])
        waves.append(
            {
                "wave": len(waves),
                "issues": [
                    {
                        "id": entry["id"],
                        "repo": entry["repo"],
                        "dependsOn": list(blockers_in_plan[entry["id"]]),
                    }
                    for entry in wave
                ],
            }
        )
        remaining -= set(ready)
    return waves


def planning_fingerprint(repos, targeted_entries):
    """Hash the Sandcastle planning inputs that invalidate a locked plan."""
    repo_part = repo_identity_snapshot(repos)
    issue_part = sorted(
        [
            {
                "id": entry["id"],
                "repo": entry["repo"],
                "blockedBy": sorted(str(item) for item in entry.get("blockedBy") or []),
                "branch": entry.get("branch"),
                "status": entry.get("status"),
                "type": entry.get("type"),
                "reviewStatus": entry.get("reviewStatus"),
            }
            for entry in targeted_entries
        ],
        key=lambda item: item["id"],
    )
    canonical = {"repos": repo_part, "targetedIssues": issue_part}
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def targeted_entry_from_issue(issue, repo):
    payload = issue_payload(issue)
    payload["repo"] = repo
    return payload


def expand_execution_targeted(index, all_repos, scope_repo_set, seed_issues):
    """Include AFK issues blocked only by other in-scope not-yet-done issues.

    `all_repos` (the full workspace repo list, never a `--repo`-scoped
    subset) resolves each issue's repo; `scope_repo_set` filters which
    resolved repos are actually part of this plan invocation.

    An issue is only pulled in via closure if its own readiness genuinely
    *depends on* another in-scope targeted issue finishing first — i.e. it
    has at least one blocker that is not yet done. An issue whose blockers
    are already all done is independently ready and must come from the seed
    (`--limit`-selected) set, not be re-admitted here: otherwise `--limit`
    would fail to bound execution scope, since any independently-ready
    issue excluded by `--limit` would get silently pulled back in.
    """
    included = {issue["id"] for issue in seed_issues}
    changed = True
    while changed:
        changed = False
        for issue_id, issue in index.items():
            if issue_id in included:
                continue
            meta = issue["meta"]
            repo = resolve_issue_repo(meta, all_repos)
            if repo not in scope_repo_set:
                continue
            if sandcastle_issue_meta(meta)["type"] != "AFK":
                continue
            if meta.get("status") not in ISSUE_ACTIVE_STATUSES:
                continue
            blockers = meta.get("blockedBy") or []
            if not blockers:
                continue
            if blockers_complete(index, issue):
                continue
            ok = True
            for blocker_id in blockers:
                blocker = index.get(str(blocker_id))
                if not blocker:
                    ok = False
                    break
                if blocker["meta"].get("status") in ISSUE_DONE_STATUSES:
                    continue
                if str(blocker_id) not in included:
                    ok = False
                    break
            if ok:
                included.add(issue_id)
                changed = True

    entries = []
    for issue_id in sorted(included):
        issue = index[issue_id]
        repo = resolve_issue_repo(issue["meta"], all_repos)
        entries.append(targeted_entry_from_issue(issue, repo))
    return entries


UNSPECIFIED_REPO_BUCKET = "(unspecified)"


def _empty_bucket():
    return {"targeted": [], "readyButNotTargeted": [], "blocked": []}


def build_plan_view(ledger, workspace_id, all_repos, scope_repos, *, limit=None):
    """Build the per-repo display buckets and cross-repo execution graph.

    `all_repos` is the full workspace repo list (used to resolve which repo
    an issue belongs to); `scope_repos` is the possibly-`--repo`-narrowed
    set of repos this particular plan invocation covers.
    """
    index = issue_index(ledger, workspace_id)
    scope_repo_set = repo_names(scope_repos)
    by_repo = {
        repo.get("name"): _empty_bucket() for repo in scope_repos if repo.get("name")
    }

    ready_afk = []
    for issue in index.values():
        bucket, reason, repo = classify_issue(
            index, issue, all_repos, scope_repo_set, sandcastle=True
        )
        meta = issue["meta"]
        if repo is None:
            explicit = meta.get("repo")
            bucket_repo = str(explicit) if explicit is not None else UNSPECIFIED_REPO_BUCKET
            by_repo.setdefault(bucket_repo, _empty_bucket())
            by_repo[bucket_repo]["blocked"].append(
                {
                    "issue": targeted_entry_from_issue(issue, bucket_repo),
                    "reason": reason,
                }
            )
            continue
        if bucket == "out_of_scope":
            continue
        if bucket == "ready_afk":
            ready_afk.append((repo, issue))
        elif bucket == "ready_not_targeted":
            by_repo[repo]["readyButNotTargeted"].append(
                targeted_entry_from_issue(issue, repo)
            )
        else:
            by_repo[repo]["blocked"].append(
                {
                    "issue": targeted_entry_from_issue(issue, repo),
                    "reason": reason,
                }
            )

    ready_afk.sort(key=lambda item: (item[0], str(item[1]["meta"].get("id", ""))))
    selected = ready_afk[:limit] if limit else ready_afk
    selected_ids = {issue["id"] for _, issue in selected}
    for repo, issue in selected:
        by_repo[repo]["targeted"].append(targeted_entry_from_issue(issue, repo))
    for repo, issue in ready_afk:
        if issue["id"] not in selected_ids:
            entry = targeted_entry_from_issue(issue, repo)
            entry["reason"] = "excluded by --limit"
            by_repo[repo]["readyButNotTargeted"].append(entry)

    seed_issues = [issue for _, issue in selected]
    targeted_entries = expand_execution_targeted(
        index, all_repos, scope_repo_set, seed_issues
    )
    waves = build_execution_waves(targeted_entries)
    fingerprint = planning_fingerprint(scope_repos, targeted_entries)
    return {
        "byRepo": by_repo,
        "targetedEntries": targeted_entries,
        "waves": waves,
        "fingerprint": fingerprint,
    }


def repo_record(repo):
    return repo_projection(repo, "name", "worktreePath", "branch", "remote")


def write_plan_artifact(workspace, repos, view, *, ledger_dir, runs_dir):
    workspace_id = workspace.id
    created = now()
    targeted_by_repo = {}
    for entry in view["targetedEntries"]:
        targeted_by_repo.setdefault(entry["repo"], []).append(entry["id"])
    for repo_name in view["byRepo"]:
        targeted_by_repo.setdefault(repo_name, [])
    targeted_issue_ids = [entry["id"] for entry in view["targetedEntries"]]
    payload = {
        "version": 2,
        "createdAt": created,
        "workspace": {
            "id": workspace_id,
            "title": workspace.to_dict().get("title"),
            "state": workspace.state,
            "ledgerDir": str(ledger_dir),
            "specPath": str(ledger_dir / "spec.md"),
        },
        "repos": [repo_record(repo) for repo in repos],
        "targetedRepos": sorted(view["byRepo"].keys()),
        "targetedIssueIds": targeted_issue_ids,
        "targetedIssuesByRepo": targeted_by_repo,
        "issuesByRepo": view["byRepo"],
        "execution": {
            "waves": view["waves"],
        },
        "fingerprint": view["fingerprint"],
    }
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    path = runs_dir / f"sandcastle-plan-{stamp}.json"
    write_json(path, payload)
    return path, payload


def set_current_plan_pointer(workspace, *, plan_path, repos, targeted_issue_ids, fingerprint):
    workspace.set_current_sandcastle_plan(
        {
            "path": str(plan_path),
            "repos": sorted(repo.get("name") for repo in repos if repo.get("name")),
            "targetedIssueIds": list(targeted_issue_ids),
            "fingerprint": fingerprint,
            "createdAt": now(),
        }
    )


def enrich_targeted_issues(ledger, workspace_id, targeted_entries):
    index = issue_index(ledger, workspace_id)
    for entry in targeted_entries:
        issue = index.get(entry["id"])
        if not issue:
            continue
        meta = dict(issue["meta"])
        if sandcastle_block(meta).get("type"):
            continue
        write_issue(
            ledger,
            workspace_id,
            entry["id"],
            meta,
            issue["body"],
            sandcastle=True,
        )


def plan_workspace(config, workspace, workspace_id, repos, *, limit=None):
    """Plan Sandcastle execution for `repos` (the possibly `--repo`-narrowed scope).

    `workspace.repos` (the full workspace repo list) is threaded separately
    into `build_plan_view` so that repo resolution for unset-`repo:` issues
    is always evaluated against the whole workspace, never against a
    `--repo`-narrowed scope.
    """
    ledger = WorkspaceLedger(config)
    all_repos = workspace.repos
    view = build_plan_view(ledger, workspace_id, all_repos, repos, limit=limit)
    enrich_targeted_issues(ledger, workspace_id, view["targetedEntries"])
    ledger_dir = ledger.ledger_dir(workspace_id)
    runs = ledger.runs_dir(workspace_id)
    plan_path, payload = write_plan_artifact(
        workspace,
        repos,
        view,
        ledger_dir=ledger_dir,
        runs_dir=runs,
    )
    targeted_issue_ids = [entry["id"] for entry in view["targetedEntries"]]
    set_current_plan_pointer(
        workspace,
        plan_path=plan_path,
        repos=repos,
        targeted_issue_ids=targeted_issue_ids,
        fingerprint=view["fingerprint"],
    )
    return plan_path, payload, view
