"""Sandcastle execute: wave-based dependency-ordered multi-repo orchestration."""
import datetime as dt
import json
import os
import subprocess
from pathlib import Path

from .issues import issue_index, issue_payload, resolve_issue_repo
from .ledger import WorkspaceLedger
from .sandcastle_plan import planning_fingerprint
from .sandcastle_state import clear_current_plan_pointer, current_plan


FAILURE_STATUSES = {"failed", "blocked-hitl"}
SKIP_FOR_EXECUTION = {"merged", "done", "skipped", "failed", "blocked-hitl"}


def _plan_artifact_paths(runs_dir):
    paths = list(runs_dir.glob("sandcastle-plan-*.json"))
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return paths


def load_plan_payload(path, *, workspace_id):
    if not path.exists():
        raise SystemExit(f"Sandcastle plan not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid Sandcastle plan JSON: {path}") from error
    if payload.get("version") != 2:
        raise SystemExit(f"Unsupported Sandcastle plan version in {path}")
    plan_workspace_id = (payload.get("workspace") or {}).get("id")
    if plan_workspace_id and plan_workspace_id != workspace_id:
        raise SystemExit(
            f"Sandcastle plan `{path}` belongs to workspace `{plan_workspace_id}`, "
            f"not `{workspace_id}`."
        )
    return payload


def targeted_entries_for_plan(ledger, workspace_id, payload, all_repos):
    """Resolve the plan's targeted issues against `all_repos` (must be LIVE, current data).

    `all_repos` must be the live workspace repo list (e.g. `data["repos"]`), never the
    plan artifact's own frozen `repos` snapshot — otherwise a repo rename/removal after
    planning can never be detected as staleness, since the frozen list would always
    resolve issues the same way it did at plan time.
    """
    targeted_ids = [str(item) for item in payload.get("targetedIssueIds") or []]
    index = issue_index(ledger, workspace_id)
    entries = []
    for issue_id in targeted_ids:
        issue = index.get(issue_id)
        if not issue:
            raise SystemExit(
                f"Sandcastle plan is stale: targeted issue `{issue_id}` no longer exists. "
                "Run `sandcastle plan` again."
            )
        meta = issue["meta"]
        repo = resolve_issue_repo(meta, all_repos)
        if repo is None:
            explicit = meta.get("repo")
            repo = str(explicit) if explicit is not None else None
        if repo is None:
            raise SystemExit(
                f"Sandcastle plan is stale: targeted issue `{issue_id}` repo could not be resolved. "
                "Run `sandcastle plan` again."
            )
        entries.append(issue_payload(issue, repo=repo))
    return entries


def _declared_targeted_entries(payload):
    """Reconstruct the plan-time targeted entries recorded in the plan artifact.

    A targeted issue is not always listed under a repo's `targeted` bucket: an issue
    pulled in only via dependency closure (e.g. a backend issue blocked by a still-open
    frontend issue in the same plan) is independently not-yet-ready, so the display
    classification puts it under `blocked` instead, even though it's part of the locked
    execution scope (`targetedIssueIds`/`execution.waves`). So every bucket must be
    scanned and then filtered down to the actual `targetedIssueIds` set, rather than
    assuming the `targeted` bucket alone is a complete picture.
    """
    targeted_ids = {str(item) for item in payload.get("targetedIssueIds") or []}
    entries_by_id = {}
    for bucket in (payload.get("issuesByRepo") or {}).values():
        for entry in bucket.get("targeted") or []:
            if entry.get("id") is not None:
                entries_by_id.setdefault(str(entry["id"]), entry)
        for item in bucket.get("blocked") or []:
            entry = item.get("issue") or {}
            if entry.get("id") is not None:
                entries_by_id.setdefault(str(entry["id"]), entry)
        for entry in bucket.get("readyButNotTargeted") or []:
            if entry.get("id") is not None:
                entries_by_id.setdefault(str(entry["id"]), entry)
    return [entries_by_id[issue_id] for issue_id in targeted_ids if issue_id in entries_by_id]


def _status_blind(entries):
    """Zero out `status` so fingerprint comparisons ignore ordinary status progression.

    Status is intentionally excluded from what counts as "planning-relevant": the eager
    invalidation hook (`_sandcastle_meta_snapshot`) never invalidates on a status-only
    change, so the lazy execute-time staleness recheck must agree, or a completely
    ordinary `issue set-status` between planning and execution would falsely refuse
    execution as stale.
    """
    blinded = []
    for entry in entries:
        entry = dict(entry)
        entry["status"] = None
        blinded.append(entry)
    return blinded


def plan_is_stale(ledger, workspace_id, data, payload):
    """Compare LIVE repo/issue state against the plan's DECLARED (frozen-at-plan-time)
    state, ignoring status-only drift.

    `live_repos` comes from live `data` (e.g. `data["repos"]`), while `declared_repos` is
    the plan artifact's own frozen `repos` snapshot -- comparing live-vs-live (or
    declared-vs-declared) on both sides would never detect a repo rename/removal, since
    it would trivially always match itself. Both sides are hashed with the same
    `planning_fingerprint()` used at plan time (reused, not reimplemented), but the
    comparison is between two freshly computed, status-blinded fingerprints rather than
    against the plan artifact's stored `fingerprint` field verbatim -- that stored value
    was computed with status included, so comparing directly against it would falsely
    flag ordinary status progression as staleness (see `_status_blind`).
    """
    live_repos = data.get("repos") or []
    declared_repos = payload.get("repos") or []
    live_entries = targeted_entries_for_plan(ledger, workspace_id, payload, live_repos)
    declared_entries = _declared_targeted_entries(payload)
    if {entry["id"] for entry in live_entries} != {entry["id"] for entry in declared_entries}:
        return True
    live_fingerprint = planning_fingerprint(live_repos, _status_blind(live_entries))
    declared_fingerprint = planning_fingerprint(declared_repos, _status_blind(declared_entries))
    return live_fingerprint != declared_fingerprint


def assert_plan_fresh(ledger, workspace_id, data, payload):
    if plan_is_stale(ledger, workspace_id, data, payload):
        raise SystemExit(
            "Sandcastle plan is stale: issue blockers, branches, Sandcastle metadata, "
            "repo scope, or targeted issue set no longer match the locked plan fingerprint. "
            "Run `sandcastle plan` again."
        )


def resolve_execution_plan(ledger, workspace_id, data, *, explicit_plan=None):
    runs_dir = ledger.runs_dir(workspace_id)
    if explicit_plan:
        plan_path = Path(explicit_plan).expanduser()
    else:
        pointer = current_plan(data)
        if pointer and pointer.get("path"):
            plan_path = Path(pointer["path"]).expanduser()
        else:
            plan_path = None
            for candidate in _plan_artifact_paths(runs_dir):
                payload = load_plan_payload(candidate, workspace_id=workspace_id)
                if not plan_is_stale(ledger, workspace_id, data, payload):
                    plan_path = candidate
                    break
            if plan_path is None:
                raise SystemExit(
                    "No valid Sandcastle plan found. Run `sandcastle plan` first or pass `--plan <path>`."
                )
    payload = load_plan_payload(plan_path, workspace_id=workspace_id)
    assert_plan_fresh(ledger, workspace_id, data, payload)
    return plan_path, payload


def dependency_map(payload):
    deps = {}
    for wave in (payload.get("execution") or {}).get("waves") or []:
        for item in wave.get("issues") or []:
            issue_id = str(item.get("id"))
            deps[issue_id] = [str(blocker) for blocker in item.get("dependsOn") or []]
    return deps


def runner_repos(payload):
    return [
        {
            "name": repo.get("name"),
            "worktreePath": repo.get("worktreePath"),
        }
        for repo in payload.get("repos") or []
        if repo.get("name")
    ]


def resolve_issue_depends_on(ledger, workspace_id, issue_id, own_repo_name, all_repos):
    """Resolve an issue's full blockedBy list to cross-repo mount dependencies.

    Uses live issue metadata (not the plan wave graph) so blockers already merged
    in a prior execute wave are still included. Deduplication is by repo name
    (first blocker wins); same-repo blockers and self-dependencies are excluded.
    Unresolvable blockers are skipped.
    """
    index = issue_index(ledger, workspace_id)
    issue = index.get(issue_id)
    if not issue:
        return []
    own_repo = str(own_repo_name or "")
    seen_repos = set()
    depends_on = []
    for blocker_id in issue["meta"].get("blockedBy") or []:
        blocker_id = str(blocker_id)
        if blocker_id == str(issue_id):
            continue
        blocker = index.get(blocker_id)
        if not blocker:
            continue
        blocker_repo = resolve_issue_repo(blocker["meta"], all_repos)
        if blocker_repo is None:
            explicit = blocker["meta"].get("repo")
            blocker_repo = str(explicit) if explicit is not None else None
        if blocker_repo is None:
            continue
        blocker_repo = str(blocker_repo)
        if blocker_repo == own_repo:
            continue
        if blocker_repo in seen_repos:
            continue
        seen_repos.add(blocker_repo)
        depends_on.append({"id": blocker_id, "repo": blocker_repo})
    return depends_on


def dependency_mounts_for_issue(runner_plan, issue_record):
    """Derive sandcastle docker() mounts from a runner plan issue (self-check mirror).

    Mirrors templates/sandcastle/main.mts `buildDependencyMounts`. dependsOn is
    deduped by repo when written; this function defensively dedupes again.
    """
    own_repo = str((runner_plan.get("repo") or {}).get("name") or "")
    repos_by_name = {
        str(repo.get("name") or ""): repo for repo in runner_plan.get("repos") or []
    }
    seen = set()
    mounts = []
    for dep in issue_record.get("dependsOn") or []:
        repo_name = str(dep.get("repo") or "")
        if not repo_name or repo_name == own_repo or repo_name in seen:
            continue
        seen.add(repo_name)
        repo = repos_by_name.get(repo_name)
        if not repo:
            continue
        worktree = repo.get("worktreePath")
        if not worktree:
            continue
        mounts.append(
            {
                "hostPath": worktree,
                "sandboxPath": f"related-repos/{repo_name}",
                "readonly": True,
            }
        )
    return mounts


def runner_issue_record(ledger, workspace_id, issue_id, *, own_repo_name, all_repos):
    index = issue_index(ledger, workspace_id)
    issue = index.get(issue_id)
    if not issue:
        raise SystemExit(f"Issue not found while building runner plan: {issue_id}")
    meta = issue["meta"]
    return {
        "id": meta.get("id"),
        "title": meta.get("title"),
        "branch": meta.get("branch"),
        "body": issue["body"].strip(),
        "path": str(issue["path"]),
        "dependsOn": resolve_issue_depends_on(
            ledger, workspace_id, issue_id, own_repo_name, all_repos
        ),
    }


def write_runner_plan(
    runs_dir,
    payload,
    *,
    wave_number,
    repo_name,
    issue_ids,
    ledger,
    workspace_id,
):
    repos = repo_by_name(payload)
    repo = repos.get(repo_name)
    if not repo:
        raise SystemExit(f"Sandcastle plan repo not found: {repo_name}")
    all_repos = payload.get("repos") or []
    workspace = payload.get("workspace") or {}
    runner_plan = {
        "workspace": {
            "id": workspace.get("id"),
            "title": workspace.get("title"),
            "specPath": workspace.get("specPath"),
        },
        "repos": runner_repos(payload),
        "repo": {
            "name": repo.get("name"),
            "worktreePath": repo.get("worktreePath"),
            "branch": repo.get("branch"),
        },
        "issues": [
            runner_issue_record(
                ledger,
                workspace_id,
                issue_id,
                own_repo_name=repo_name,
                all_repos=all_repos,
            )
            for issue_id in issue_ids
        ],
    }
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    plan_path = runs_dir / f"sandcastle-run-plan-w{wave_number}-{repo_name}-{stamp}.json"
    result_path = runs_dir / f"sandcastle-run-result-w{wave_number}-{repo_name}-{stamp}.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(runner_plan, indent=2) + "\n", encoding="utf-8")
    return plan_path, result_path


def propagate_blocked_hitl(ledger, workspace_id, targeted_ids, depends_on, *, set_status):
    """Mark dependents blocked-hitl when a targeted blocker failed or is blocked-hitl."""
    index = issue_index(ledger, workspace_id)
    targeted = set(targeted_ids)
    blocked_sources = {
        issue_id
        for issue_id in targeted
        if index.get(issue_id)
        and index[issue_id]["meta"].get("status") in FAILURE_STATUSES
    }
    newly_blocked = set()
    changed = True
    while changed:
        changed = False
        for issue_id in sorted(targeted):
            if issue_id in newly_blocked:
                continue
            issue = index.get(issue_id)
            if not issue:
                continue
            status = issue["meta"].get("status")
            if status in SKIP_FOR_EXECUTION:
                continue
            blockers = [blocker for blocker in depends_on.get(issue_id, []) if blocker in targeted]
            if any(
                blocker in blocked_sources or blocker in newly_blocked for blocker in blockers
            ):
                set_status(
                    workspace_id,
                    issue_id,
                    "blocked-hitl",
                    extra={"blockedByFailure": True},
                )
                newly_blocked.add(issue_id)
                changed = True
                index = issue_index(ledger, workspace_id)
    return newly_blocked


def repo_by_name(payload):
    return {str(repo.get("name") or ""): repo for repo in payload.get("repos") or []}


def wave_issues_by_repo(wave, *, skip_ids):
    grouped = {}
    for item in wave.get("issues") or []:
        issue_id = str(item.get("id"))
        if issue_id in skip_ids:
            continue
        repo_name = str(item.get("repo") or "")
        grouped.setdefault(repo_name, []).append(issue_id)
    for repo_name in grouped:
        grouped[repo_name].sort()
    return grouped


def skipped_issue_ids(ledger, workspace_id, targeted_ids):
    index = issue_index(ledger, workspace_id)
    return {
        issue_id
        for issue_id in targeted_ids
        if index.get(issue_id)
        and index[issue_id]["meta"].get("status") in SKIP_FOR_EXECUTION
    }


def summarize_execution(targeted_ids, index):
    counts = {}
    for issue_id in targeted_ids:
        issue = index.get(issue_id)
        status = issue["meta"].get("status") if issue else "missing"
        counts[status] = counts.get(status, 0) + 1
    return counts


def execute_plan(
    config,
    data,
    workspace_id,
    plan_path,
    payload,
    *,
    command,
    set_status,
    apply_result,
    append_note,
    save_workspace,
    load_workspace,
):
    ledger = WorkspaceLedger(config)
    if ledger.sandcastle_run_active(workspace_id):
        lock_path = ledger.sandcastle_lock_path(workspace_id)
        raise SystemExit(f"Sandcastle run already active: {lock_path}")

    targeted_ids = [str(item) for item in payload.get("targetedIssueIds") or []]
    depends_on = dependency_map(payload)
    runs_dir = ledger.runs_dir(workspace_id)
    lock_path = ledger.acquire_sandcastle_lock(
        workspace_id,
        {"planPath": str(plan_path), "targetedIssueIds": targeted_ids},
    )
    result = None
    try:
        append_note(
            workspace_id,
            f"Started Sandcastle execute for plan `{plan_path}` targeting "
            f"{', '.join(targeted_ids) or '(none)'}.",
        )
        data = load_workspace(workspace_id)
        data["state"] = "in-progress"
        save_workspace(data)

        waves = (payload.get("execution") or {}).get("waves") or []
        for wave in waves:
            propagate_blocked_hitl(
                ledger,
                workspace_id,
                targeted_ids,
                depends_on,
                set_status=set_status,
            )
            skip_ids = skipped_issue_ids(ledger, workspace_id, targeted_ids)
            grouped = wave_issues_by_repo(wave, skip_ids=skip_ids)
            if not grouped:
                continue

            invocation = []
            for repo_name, issue_ids in sorted(grouped.items()):
                # A launch-time failure for THIS repo (e.g. its worktree vanished from
                # disk between planning and execute) must not abort the whole wave: any
                # sibling repo already `Popen`'d earlier in this same loop would then
                # never be `.wait()`-ed/reaped, and this repo's own issues would never
                # reach the reconciliation loop's backstop (since they'd have no
                # `invocation` item at all), leaving them stuck "running" forever --
                # exactly the bugs 4/5 symptom via an uncovered launch-time path. So
                # each repo's launch is isolated, failed repos are marked `failed`
                # immediately (they never got an invocation item for the backstop to
                # find), and the loop keeps going so unaffected repos still launch.
                try:
                    runner_plan_path, result_path = write_runner_plan(
                        runs_dir,
                        payload,
                        wave_number=wave.get("wave", 0),
                        repo_name=repo_name,
                        issue_ids=issue_ids,
                        ledger=ledger,
                        workspace_id=workspace_id,
                    )
                    repo = repo_by_name(payload)[repo_name]
                    worktree = repo.get("worktreePath")
                    if not worktree or not Path(worktree).exists():
                        raise SystemExit(f"Repo worktree missing on disk: {worktree}")
                    for issue_id in issue_ids:
                        set_status(workspace_id, issue_id, "running")
                    env = os.environ.copy()
                    env.update(
                        {
                            "WORKSPACE_ID": workspace_id,
                            "WORKSPACE_LEDGER_DIR": str(ledger.ledger_dir(workspace_id)),
                            "WORKSPACE_SANDCASTLE_PLAN": str(runner_plan_path),
                            "WORKSPACE_SANDCASTLE_RESULT": str(result_path),
                            "WORKSPACE_REPO_NAME": str(repo.get("name") or ""),
                            "WORKSPACE_REPO_PATH": str(worktree),
                        }
                    )
                    proc = subprocess.Popen(
                        command,
                        cwd=worktree,
                        shell=True,
                        env=env,
                    )
                except (Exception, SystemExit) as error:  # noqa: BLE001 - isolate per repo
                    append_note(
                        workspace_id,
                        f"Failed to launch Sandcastle command for repo `{repo_name}` "
                        f"wave {wave.get('wave', 0)}: {error}",
                    )
                    for issue_id in issue_ids:
                        set_status(
                            workspace_id,
                            issue_id,
                            "failed",
                            extra={"lastError": f"Failed to launch Sandcastle command: {error}"},
                        )
                    save_workspace(load_workspace(workspace_id))
                    continue
                invocation.append(
                    {
                        "repo": repo_name,
                        "issueIds": issue_ids,
                        "planPath": runner_plan_path,
                        "resultPath": result_path,
                        "proc": proc,
                    }
                )

            for item in invocation:
                returncode = item["proc"].wait()
                append_note(
                    workspace_id,
                    f"Sandcastle command exited with code {returncode} for repo `{item['repo']}` "
                    f"wave {wave.get('wave', 0)} plan `{item['planPath']}`.",
                )
                if item["resultPath"].exists():
                    try:
                        apply_result(workspace_id, item["resultPath"])
                    except (Exception, SystemExit) as error:  # noqa: BLE001 - must not abort sibling waits
                        append_note(
                            workspace_id,
                            f"Failed to reconcile Sandcastle result `{item['resultPath']}` "
                            f"for repo `{item['repo']}`: {error}",
                        )
                elif returncode != 0:
                    for issue_id in item["issueIds"]:
                        set_status(
                            workspace_id,
                            issue_id,
                            "failed",
                            extra={
                                "lastError": (
                                    f"Sandcastle command exited with code {returncode} "
                                    "with no result file"
                                )
                            },
                        )
                    save_workspace(load_workspace(workspace_id))
                    append_note(
                        workspace_id,
                        f"Marked {len(item['issueIds'])} issue(s) failed in repo `{item['repo']}` "
                        "because no result file was written.",
                    )

                # Backstop: anything still "running" after the above -- whether from a
                # zero-exit with no result file, a result file that omitted this issue,
                # or a result file that failed to parse/apply above -- must not linger
                # forever (stuck "running" is neither active nor done, so it can never
                # be replanned and blocks dependents indefinitely).
                index_after = issue_index(ledger, workspace_id)
                stuck_ids = sorted(
                    issue_id
                    for issue_id in item["issueIds"]
                    if index_after.get(issue_id, {}).get("meta", {}).get("status") == "running"
                )
                if stuck_ids:
                    for issue_id in stuck_ids:
                        set_status(
                            workspace_id,
                            issue_id,
                            "failed",
                            extra={
                                "lastError": (
                                    "Sandcastle runner exited without reporting a result "
                                    "for this issue"
                                )
                            },
                        )
                    save_workspace(load_workspace(workspace_id))
                    append_note(
                        workspace_id,
                        f"Marked {len(stuck_ids)} issue(s) failed in repo `{item['repo']}` "
                        "because the runner did not report a result for them.",
                    )

        index = issue_index(ledger, workspace_id)
        counts = summarize_execution(targeted_ids, index)
        if counts.get("failed") or counts.get("blocked-hitl"):
            outcome = "completed-with-failures"
        elif all(
            index.get(issue_id, {}).get("meta", {}).get("status") in {"merged", "done", "skipped"}
            for issue_id in targeted_ids
        ):
            outcome = "completed"
        else:
            outcome = "completed-with-open-issues"
        result = {
            "planPath": str(plan_path),
            "targetedIssueIds": targeted_ids,
            "targetedRepos": payload.get("targetedRepos") or [],
            "outcome": outcome,
            "statusCounts": counts,
        }
        return result
    finally:
        ledger.release_sandcastle_lock(lock_path)
        data = load_workspace(workspace_id)
        clear_current_plan_pointer(data)
        save_workspace(data)
        if result is not None:
            append_note(workspace_id, format_execute_summary(result))


def format_execute_summary(result):
    repos = ", ".join(result.get("targetedRepos") or []) or "(none)"
    issues = ", ".join(result.get("targetedIssueIds") or []) or "(none)"
    counts = result.get("statusCounts") or {}
    count_text = ", ".join(f"{status}={count}" for status, count in sorted(counts.items())) or "none"
    return (
        f"Sandcastle execute finished ({result.get('outcome')}): plan `{result.get('planPath')}`, "
        f"repo(s) {repos}, issue(s) {issues}, statuses {count_text}."
    )
