"""Shared CLI command implementations for workspace entrypoints."""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from workspaces import issues as issue_model
from workspaces.cleanup import CleanupSafety
from workspaces.common import (
    DEFAULT_CONFIG,
    DEFAULT_CONFIG_PATH,
    DONE_STATES,
    SKILL_ROOT,
    STATE_ORDER,
    expand,
    now,
    read_yaml,
    run,
    slug,
    template,
)
from workspaces.git import (
    github_repo_from_remote,
    git_info,
    remove_linked_worktree,
)
from workspaces.issues import (
    ISSUE_DONE_STATUSES,
    default_issue_repo,
    sandcastle_issue_meta,
)
from workspaces.sandcastle_execute import execute_plan, format_execute_summary, resolve_execution_plan
from workspaces.sandcastle_plan import plan_workspace, select_plan_repos
from workspaces.ledger import WorkspaceLedger


CONFIG_PATH = Path(os.environ.get("WORKSPACES_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()

BASE_DESCRIPTION = (
    "Manage local workspace ledger.\n\n"
    "For Sandcastle/AFK/runner-based execution, use workspace_with_sandcastle.py instead."
)
SANDCASTLE_DESCRIPTION = (
    "WARNING: This entrypoint is only for explicit Sandcastle/AFK/runner-based execution.\n\n"
    "For default manual development, use workspace.py instead.\n\n"
    "Manage local workspace ledger with Sandcastle execution extensions."
)


def load_config():
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        config.update(loaded)
    for key in ("source_root", "workspace_root", "ledger_root"):
        config[key] = expand(config[key])
    return config

def ledger_dir(config, workspace_id):
    return Path(config["ledger_root"]) / "workspaces" / workspace_id


def workspace_root_path(config, workspace_id):
    return Path(config["workspace_root"]) / workspace_id


def vscode_workspace_path(config, workspace_id):
    return workspace_root_path(config, workspace_id) / f"{workspace_id}.code-workspace"


def issues_dir(config, workspace_id):
    return ledger_dir(config, workspace_id) / "issues"


def issue_path(config, workspace_id, issue_id):
    return issues_dir(config, workspace_id) / f"{slug(issue_id)}.md"


def runs_dir(config, workspace_id):
    return ledger_dir(config, workspace_id) / "runs"


def load_workspace(config, workspace_id):
    return WorkspaceLedger(config).load(workspace_id)


def iter_workspaces(config):
    yield from WorkspaceLedger(config).iter_workspaces()


def save_workspace(config, data):
    WorkspaceLedger(config).save(data)


def write_vscode_workspace(config, data):
    return WorkspaceLedger(config).write_vscode_workspace(data)


def build_vscode_workspace(config, data):
    return WorkspaceLedger(config).build_vscode_workspace(data)


def print_vscode_workspace_block(config, data):
    """Append the current .code-workspace JSON as the final stdout block.

    Skipped once a workspace is cleanup-done, mirroring the guard in
    WorkspaceLedger.save(): cleanup already deleted the workspace root
    (worktrees + .code-workspace) and dropped vscodeWorkspacePath, so there
    is no meaningful current folder set to report. Writing/reading here
    unconditionally would resurrect a stale .code-workspace file listing
    repos that no longer exist on disk.
    """
    if data.get("cleanupStatus") == "done":
        return
    path = vscode_workspace_path(config, data["id"])
    if not path.exists():
        path = write_vscode_workspace(config, data)
    payload = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(payload, indent=2))


def append_note(config, workspace_id, text):
    WorkspaceLedger(config).append_note(workspace_id, text)


def ensure_workspace(config, workspace_id, title=None, state="captured", input_text=None):
    return WorkspaceLedger(config).ensure(workspace_id, title, state, input_text)


def issue_body_from_args(args):
    return issue_model.issue_body_from_args(args)


def write_issue(config, workspace_id, issue_id, meta, body, *, sandcastle=False, skip_plan_invalidation=False):
    return issue_model.write_issue(
        WorkspaceLedger(config),
        workspace_id,
        issue_id,
        meta,
        body,
        sandcastle=sandcastle,
        skip_plan_invalidation=skip_plan_invalidation,
    )


def read_issue(path):
    return issue_model.read_issue(path)


def iter_issues(config, workspace_id):
    return issue_model.iter_issues(WorkspaceLedger(config), workspace_id)


def issue_index(config, workspace_id):
    return issue_model.issue_index(WorkspaceLedger(config), workspace_id)


def blockers_complete(index, issue):
    return issue_model.blockers_complete(index, issue)


def ready_issues(config, workspace_id, *, sandcastle=False):
    return issue_model.ready_issues(
        WorkspaceLedger(config), workspace_id, sandcastle=sandcastle
    )


def select_repo(data, repo_name=None):
    repos = data.get("repos", [])
    if repo_name:
        for repo in repos:
            if repo.get("name") == repo_name:
                return repo
        raise SystemExit(f"Repo not found in workspace: {repo_name}")
    if not repos:
        raise SystemExit("Workspace has no repos. Add or adopt one before running Sandcastle.")
    if len(repos) > 1:
        names = ", ".join(repo.get("name") or "?" for repo in repos)
        raise SystemExit(f"Workspace has multiple repos ({names}). Pass --repo.")
    return repos[0]


def print_sandcastle_plan_summary(view):
    for repo_name in sorted(view["byRepo"]):
        bucket = view["byRepo"][repo_name]
        print(f"Repo `{repo_name}`:")
        print(f"  targeted: {len(bucket['targeted'])}")
        for issue in bucket["targeted"]:
            print(f"    - {issue['id']}: {issue['title']}")
        print(f"  ready-but-not-targeted: {len(bucket['readyButNotTargeted'])}")
        for issue in bucket["readyButNotTargeted"]:
            reason = issue.get("reason") or issue.get("type", "")
            print(f"    - {issue['id']}: {issue['title']} ({reason})")
        print(f"  blocked/not-ready: {len(bucket['blocked'])}")
        for item in bucket["blocked"]:
            issue = item["issue"]
            print(f"    - {issue['id']}: {issue['title']} ({item['reason']})")
    print("Execution waves:")
    for wave in view["waves"]:
        ids = ", ".join(f"{item['repo']}:{item['id']}" for item in wave["issues"])
        print(f"  wave {wave['wave']}: {ids or '(empty)'}")


def cmd_sandcastle_plan(args):
    config = load_config()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be greater than 0")
    data = load_workspace(config, args.id)
    repos = select_plan_repos(data, args.repo)
    plan_path, payload, view = plan_workspace(
        config,
        data,
        args.id,
        repos,
        limit=args.limit,
    )
    save_workspace(config, data)
    targeted_ids = payload["targetedIssueIds"]
    repo_names = payload["targetedRepos"]
    append_note(
        config,
        args.id,
        f"Sandcastle plan `{plan_path}` for repo(s) {', '.join(repo_names) or '(none)'} "
        f"targeting issue(s) {', '.join(targeted_ids) or '(none)'}.",
    )
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print(f"Wrote Sandcastle plan: {plan_path}")
    print_sandcastle_plan_summary(view)
    print(json.dumps(payload, indent=2))


def cmd_sandcastle_init_runner(args):
    config = load_config()
    data = load_workspace(config, args.id)
    repo = select_repo(data, args.repo)
    written = init_sandcastle_runner(repo, force=args.force)
    append_note(
        config,
        args.id,
        f"Initialized Sandcastle runner in `{repo.get('worktreePath')}` with {len(written)} file(s).",
    )
    for path in written:
        print(path)


def cmd_sandcastle_reconcile_result(args):
    config = load_config()
    load_workspace(config, args.id)
    applied = apply_sandcastle_result(config, args.id, args.result_path)
    print(f"Reconciled {len(applied)} issue update(s).")


def cmd_sandcastle_execute(args):
    config = load_config()
    if not args.command:
        raise SystemExit("--command is required")
    workspace_id = args.id
    data = load_workspace(config, workspace_id)
    ledger = WorkspaceLedger(config)
    plan_path, payload = resolve_execution_plan(
        ledger,
        workspace_id,
        data,
        explicit_plan=args.plan,
    )

    def set_status_cb(ws_id, issue_id, status, **kwargs):
        set_issue_status(
            config,
            ws_id,
            issue_id,
            status,
            review_status=kwargs.get("review_status"),
            branch=kwargs.get("branch"),
            extra=kwargs.get("extra"),
            sandcastle=True,
        )

    def apply_result_cb(ws_id, result_path):
        # This is execute's own result reconciliation, not an external edit -- it must
        # not self-trigger the eager "plan invalidated" hook (e.g. a runner reporting a
        # normal reviewStatus change on a targeted issue would otherwise spuriously
        # invalidate the very plan this run is executing and pollute notes.md).
        apply_sandcastle_result(config, ws_id, result_path, skip_plan_invalidation=True)

    def append_note_cb(ws_id, text):
        append_note(config, ws_id, text)

    def save_workspace_cb(ws_data):
        save_workspace(config, ws_data)

    def load_workspace_cb(ws_id):
        return load_workspace(config, ws_id)

    result = execute_plan(
        config,
        data,
        workspace_id,
        plan_path,
        payload,
        command=args.command,
        set_status=set_status_cb,
        apply_result=apply_result_cb,
        append_note=append_note_cb,
        save_workspace=save_workspace_cb,
        load_workspace=load_workspace_cb,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_execute_summary(result))
        print(json.dumps(result, indent=2))
    if result.get("outcome") == "completed-with-failures":
        raise SystemExit(1)


def set_issue_status(
    config,
    workspace_id,
    issue_id,
    status,
    review_status=None,
    branch=None,
    extra=None,
    *,
    sandcastle=False,
    skip_plan_invalidation=False,
):
    return issue_model.set_issue_status(
        WorkspaceLedger(config),
        workspace_id,
        issue_id,
        status,
        review_status=review_status,
        branch=branch,
        extra=extra,
        sandcastle=sandcastle,
        skip_plan_invalidation=skip_plan_invalidation,
    )


def maybe_mark_user_review(config, workspace_id):
    return issue_model.maybe_mark_user_review(WorkspaceLedger(config), workspace_id)


def apply_sandcastle_result(config, workspace_id, result_path, *, skip_plan_invalidation=False):
    path = Path(result_path).expanduser()
    if not path.exists():
        raise SystemExit(f"Sandcastle result not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    updates = payload.get("issues") or payload.get("updates") or []
    if not isinstance(updates, list):
        raise SystemExit("Sandcastle result field `issues` must be a list")

    applied = []
    failures = []
    # Each entry is applied independently: one malformed/unknown-id entry must not
    # discard legitimately-successful entries listed after it in the same result
    # file (a runner reporting N issues shouldn't have N-1 of them silently lost,
    # and then incorrectly marked `failed` by execute's stuck-"running" backstop,
    # just because one unrelated entry in the same JSON list was bad).
    for index, update in enumerate(updates):
        try:
            if not isinstance(update, dict):
                raise SystemExit(f"Sandcastle result entry {index} must be an object")
            issue_id = update.get("id")
            status = update.get("status")
            if not issue_id or not status:
                raise SystemExit(f"Sandcastle result entry {index} needs `id` and `status`")
            extra = {
                key: value
                for key, value in update.items()
                if key not in {"id", "status", "reviewStatus", "branch", "note"}
            }
            issue_path_, meta, old_status = set_issue_status(
                config,
                workspace_id,
                str(issue_id),
                str(status),
                review_status=update.get("reviewStatus"),
                branch=update.get("branch"),
                extra=extra,
                sandcastle=True,
                skip_plan_invalidation=skip_plan_invalidation,
            )
            applied.append((meta, old_status, update.get("note"), issue_path_))
        except (Exception, SystemExit) as error:  # noqa: BLE001 - isolate per entry
            failures.append((index, update, error))

    data = load_workspace(config, workspace_id)
    save_workspace(config, data)
    summary = f"Reconciled Sandcastle result `{path}` with {len(applied)} issue update(s)"
    if failures:
        summary += f", {len(failures)} entr{'y' if len(failures) == 1 else 'ies'} failed to apply"
    append_note(config, workspace_id, summary + ".")
    for meta, old_status, note, _ in applied:
        detail = note or f"Issue `{meta['id']}` status changed from `{old_status}` to `{meta['status']}`."
        append_note(config, workspace_id, detail)
    for index, update, error in failures:
        entry_id = update.get("id") if isinstance(update, dict) else None
        append_note(
            config,
            workspace_id,
            f"Sandcastle result `{path}` entry {index} (id={entry_id!r}) failed to apply: {error}",
        )
    maybe_mark_user_review(config, workspace_id)
    if failures and not applied:
        # Preserve the existing "this result was entirely unusable" signal for
        # callers that treat a hard exception as "nothing here could be reconciled"
        # (e.g. execute's per-item backstop, which then fails whichever of this
        # result's issues never made it out of "running").
        raise SystemExit(
            f"Sandcastle result `{path}` had {len(failures)} invalid entr"
            f"{'y' if len(failures) == 1 else 'ies'} and no valid updates were applied."
        )
    return applied


def init_sandcastle_runner(repo, force=False):
    worktree = repo.get("worktreePath")
    if not worktree or not Path(worktree).exists():
        raise SystemExit(f"Repo worktree missing on disk: {worktree}")
    source = SKILL_ROOT / "templates" / "sandcastle"
    target = Path(worktree) / ".sandcastle"
    target.mkdir(parents=True, exist_ok=True)

    written = []
    for item in sorted(source.iterdir()):
        if not item.is_file():
            continue
        dest = target / item.name
        if dest.exists() and not force:
            raise SystemExit(f"Refusing to overwrite existing file: {dest}. Pass --force.")
        shutil.copyfile(item, dest)
        written.append(dest)
    return written


def cleanup_safety(config):
    return CleanupSafety(config, select_repo)


def workspace_root_extra_paths(config, data):
    return cleanup_safety(config).workspace_root_extra_paths(data)


def workspace_cleanup_candidates(config, workspace_ids):
    return cleanup_safety(config).workspace_candidates(workspace_ids)


def print_workspace_cleanup_plan(candidates):
    cleanup_safety({}).print_workspace_plan(candidates)


def write_cleanup_plan(config, candidates):
    return cleanup_safety(config).write_plan(candidates)


def load_cleanup_plan(path):
    return cleanup_safety({}).load_plan(path)


def validate_cleanup_plan_candidate(config, plan, workspace_id):
    return cleanup_safety(config).validate_plan_candidate(plan, workspace_id)


def cleanup_workspace(config, workspace_id, force=False):
    return cleanup_safety(config).cleanup_workspace(workspace_id, force=force)


def cleanup_candidates(config, workspace_ids, repo_name=None):
    return cleanup_safety(config).issue_candidates(workspace_ids, repo_name=repo_name)


def print_cleanup_plan(candidates):
    cleanup_safety({}).print_issue_plan(candidates)


def cleanup_item(config, item, repo_name=None):
    return cleanup_safety(config).cleanup_item(item, repo_name=repo_name)


def upsert_repo(data, repo):
    repos = data.setdefault("repos", [])
    for index, existing in enumerate(repos):
        if existing.get("worktreePath") == repo.get("worktreePath") or existing.get("name") == repo.get("name"):
            repos[index] = {**existing, **repo}
            return
    repos.append(repo)


def infer_workspace_id(path, info):
    current = Path(path).resolve()
    for parent in [current] + list(current.parents):
        pointer = parent / ".workspace-id"
        if pointer.exists():
            return pointer.read_text(encoding="utf-8").strip()
    branch = info.get("branch") or ""
    jira = re.search(r"[A-Z][A-Z0-9]+-\d+", branch)
    if jira:
        return jira.group(0)
    if branch:
        return branch.replace("/", "-")
    raise SystemExit("Could not infer workspace id. Pass --id.")


def cmd_init_config(args):
    if CONFIG_PATH.exists() and not args.force:
        print(f"Config already exists: {CONFIG_PATH}")
        return

    template_path = SKILL_ROOT / "templates" / "config.yaml"
    with template_path.open("r", encoding="utf-8") as f:
        defaults = yaml.safe_load(f) or {}

    if args.non_interactive:
        values = dict(defaults)
    else:
        default_source = defaults.get("source_root", DEFAULT_CONFIG["source_root"])
        default_workspace = defaults.get("workspace_root", DEFAULT_CONFIG["workspace_root"])

        answer = input(f"source_root [{default_source}]: ").strip()
        source_root = answer or default_source

        answer = input(f"workspace_root [{default_workspace}]: ").strip()
        workspace_root = answer or default_workspace

        ledger_root = f"{workspace_root}/_ledger"

        expanded_source = expand(source_root)
        if not Path(expanded_source).exists():
            print(f"Warning: source_root does not exist: {expanded_source}", file=sys.stderr)

        values = dict(defaults)
        values["source_root"] = source_root
        values["workspace_root"] = workspace_root
        values["ledger_root"] = ledger_root

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(values, f, sort_keys=False, allow_unicode=False)
    print(f"Wrote {CONFIG_PATH}")


def cmd_config(args):
    print(yaml.safe_dump(load_config(), sort_keys=False).strip())


def add_source_repo_to_workspace(config, data, repo_name, branch=None):
    workspace_id = data["id"]
    workspace_root = workspace_root_path(config, workspace_id)
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / ".workspace-id").write_text(workspace_id + "\n", encoding="utf-8")
    source = Path(config["source_root"]) / repo_name
    dest = workspace_root / repo_name
    if not source.exists():
        raise SystemExit(f"Source repo not found: {source}")
    if not dest.exists():
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
                f"{config['base_remote']}/{config['base_branch']}",
            ],
            capture=True,
        )
    info = git_info(dest, config)
    upsert_repo(
        data,
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


def cmd_create(args):
    config = load_config()
    data = ensure_workspace(config, args.id, args.title, args.state, args.input)
    for repo in args.repo or []:
        add_source_repo_to_workspace(config, data, repo, branch=args.branch)
    save_workspace(config, data)
    print(str(ledger_dir(config, args.id)))
    print_vscode_workspace_block(config, data)


def cmd_adopt(args):
    config = load_config()
    info = git_info(args.path, config)
    workspace_id = args.id or infer_workspace_id(args.path, info)
    data = ensure_workspace(config, workspace_id, args.title, "in-progress")
    upsert_repo(
        data,
        {
            "name": args.name or Path(info["root"]).name,
            "worktreePath": info["root"],
            "branch": info["branch"],
            "baseRemote": config["base_remote"],
            "baseBranch": config["base_branch"],
            "pushRemote": config["push_remote"],
            "remote": info["remote"],
            "forkRemote": info["forkRemote"],
        },
    )
    save_workspace(config, data)
    ws_root = workspace_root_path(config, workspace_id)
    ws_root.mkdir(parents=True, exist_ok=True)
    (ws_root / ".workspace-id").write_text(workspace_id + "\n", encoding="utf-8")
    append_note(config, workspace_id, f"Adopted worktree `{info['root']}` on branch `{info['branch']}`.")
    print(workspace_id)
    print_vscode_workspace_block(config, data)


def find_repo(data, repo_name):
    for index, repo in enumerate(data.get("repos", [])):
        if repo.get("name") == repo_name:
            return index, repo
    raise SystemExit(f"Repo not found in workspace: {repo_name}")


def cmd_repo_list(args):
    config = load_config()
    data = load_workspace(config, args.id)
    repos = data.get("repos", [])
    if args.json:
        print(json.dumps({"repos": repos}, indent=2))
        return
    if not repos:
        print("No repos found.")
        return
    print(f"{'NAME':24} {'BRANCH':24} PATH")
    for repo in repos:
        print(f"{str(repo.get('name') or '')[:24]:24} {str(repo.get('branch') or '')[:24]:24} {repo.get('worktreePath') or ''}")


def cmd_repo_add(args):
    config = load_config()
    data = load_workspace(config, args.id)
    dest = add_source_repo_to_workspace(config, data, args.repo, branch=args.branch)
    save_workspace(config, data)
    append_note(config, args.id, f"Added repo `{args.repo}` at `{dest}`.")
    print(dest)
    print_vscode_workspace_block(config, data)


def cmd_repo_adopt(args):
    config = load_config()
    data = load_workspace(config, args.id)
    info = git_info(args.path, config)
    upsert_repo(
        data,
        {
            "name": args.name or Path(info["root"]).name,
            "worktreePath": info["root"],
            "branch": info["branch"],
            "baseRemote": config["base_remote"],
            "baseBranch": config["base_branch"],
            "pushRemote": config["push_remote"],
            "remote": info["remote"],
            "forkRemote": info["forkRemote"],
        },
    )
    save_workspace(config, data)
    append_note(config, args.id, f"Adopted repo `{info['root']}` on branch `{info['branch']}`.")
    print(info["root"])
    print_vscode_workspace_block(config, data)


def remove_repo_worktree(config, data, repo):
    remove_linked_worktree(
        repo.get("worktreePath"),
        workspace_root_path(config, data["id"]),
        source_path=repo.get("sourcePath"),
    )


def cmd_repo_remove(args):
    config = load_config()
    data = load_workspace(config, args.id)
    index, repo = find_repo(data, args.repo)
    if args.delete_worktree:
        remove_repo_worktree(config, data, repo)
    data["repos"].pop(index)
    save_workspace(config, data)
    append_note(config, args.id, f"Removed repo `{args.repo}` from workspace metadata.")
    print(args.repo)
    print_vscode_workspace_block(config, data)


def refresh_repo(config, data, repo):
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


def cmd_repo_refresh(args):
    config = load_config()
    data = load_workspace(config, args.id)
    if args.repo:
        index, repo = find_repo(data, args.repo)
        data["repos"][index] = refresh_repo(config, data, repo)
        refreshed = [args.repo]
    else:
        data["repos"] = [refresh_repo(config, data, repo) for repo in data.get("repos", [])]
        refreshed = [repo.get("name") or "?" for repo in data.get("repos", [])]
    save_workspace(config, data)
    append_note(config, args.id, f"Refreshed repo metadata: {', '.join(refreshed)}.")
    print("\n".join(refreshed))


def find_workspace_from_cwd(config):
    current = Path.cwd().resolve()
    for parent in [current] + list(current.parents):
        pointer = parent / ".workspace-id"
        if pointer.exists():
            return pointer.read_text(encoding="utf-8").strip()
    workspaces = Path(config["ledger_root"]) / "workspaces"
    if workspaces.exists():
        for meta in workspaces.glob("*/workspace.yaml"):
            data = read_yaml(meta)
            for repo in data.get("repos", []):
                path = repo.get("worktreePath")
                if path and current.is_relative_to(Path(path).resolve()):
                    return data["id"]
    raise SystemExit("Workspace id required.")


def cmd_status(args):
    config = load_config()
    workspace_id = args.id or find_workspace_from_cwd(config)
    data = load_workspace(config, workspace_id)
    print(f"Workspace: {data['id']}")
    print(f"Title: {data.get('title', '')}")
    print(f"State: {data.get('state', '')}")
    if data.get("vscodeWorkspacePath"):
        print(f"VS Code: {data.get('vscodeWorkspacePath')}")
    print("")
    for repo in data.get("repos", []):
        path = repo.get("worktreePath")
        print(f"- {repo.get('name')}: {path}")
        if path and Path(path).exists():
            info = git_info(path, config)
            print(f"  branch: {info['branch']}")
            print(f"  dirty: {'yes' if info['dirty'] else 'no'}")
            if info["aheadOfBase"] is not None:
                print(f"  ahead {config['base_remote']}/{config['base_branch']}: {info['aheadOfBase']}")
        else:
            print("  missing on disk")
    prs = data.get("links", {}).get("githubPrs", [])
    if prs:
        print("")
        print("GitHub PRs:")
        for pr in prs:
            label = pr.get("url") or pr.get("number")
            print(f"- {pr.get('repo')} {label} {pr.get('state', '')}")
    issues = iter_issues(config, workspace_id)
    if issues:
        sandcastle = getattr(args, "sandcastle_mode", False)
        ready, hitl = ready_issues(config, workspace_id, sandcastle=sandcastle)
        counts = {}
        for issue in issues:
            status = issue["meta"].get("status", "")
            counts[status] = counts.get(status, 0) + 1
        print("")
        print("Issues:")
        print(f"  total: {len(issues)}")
        if sandcastle:
            print(f"  ready AFK: {len(ready)}")
            print(f"  ready HITL: {len(hitl)}")
        else:
            print(f"  ready: {len(ready)}")
        print("  statuses: " + " ".join(f"{k}:{v}" for k, v in sorted(counts.items())))


def cmd_open(args):
    config = load_config()
    data = load_workspace(config, args.id)
    save_workspace(config, data)
    path = Path(data.get("vscodeWorkspacePath") or vscode_workspace_path(config, args.id))
    if not path.exists():
        path = write_vscode_workspace(config, data)
    if args.print:
        print(path)
        print_vscode_workspace_block(config, data)
        return
    command = args.command or config.get("editor_command") or "code"
    parts = shlex.split(command)
    if not parts:
        raise SystemExit("Editor command is empty.")
    try:
        subprocess.Popen([*parts, str(path)])
    except FileNotFoundError as error:
        raise SystemExit(f"Editor command not found: {parts[0]}") from error
    append_note(config, args.id, f"Opened VS Code workspace `{path}`.")
    print(path)
    print_vscode_workspace_block(config, data)


def doctor_issue(severity, code, message, fixable=False, fixed=False):
    result = {
        "severity": severity,
        "code": code,
        "message": message,
        "fixable": fixable,
    }
    if fixed:
        result["fixed"] = True
    return result


def doctor_workspace(config, workspace_id, fix=False):
    data = load_workspace(config, workspace_id)
    issues = []
    root = ledger_dir(config, workspace_id)
    workspace_root = workspace_root_path(config, workspace_id)

    required_dirs = [root / "runs", root / "issues"]
    required_files = {
        root / "spec.md": template("spec.md").format(
            id=workspace_id,
            title=data.get("title") or workspace_id,
            input="TBD.",
        ),
        root / "notes.md": template("notes.md"),
    }
    for directory in required_dirs:
        if not directory.exists():
            issue = doctor_issue("warning", "missing-dir", f"Missing directory: {directory}", True)
            if fix:
                directory.mkdir(parents=True, exist_ok=True)
                issue["fixed"] = True
            issues.append(issue)
    for path, contents in required_files.items():
        if not path.exists():
            issue = doctor_issue("warning", "missing-file", f"Missing file: {path}", True)
            if fix:
                path.write_text(contents, encoding="utf-8")
                issue["fixed"] = True
            issues.append(issue)

    if data.get("cleanupStatus") != "done":
        pointer = workspace_root / ".workspace-id"
        if not pointer.exists() or pointer.read_text(encoding="utf-8").strip() != workspace_id:
            issue = doctor_issue(
                "warning",
                "workspace-pointer",
                f"Missing or stale workspace pointer: {pointer}",
                True,
            )
            if fix:
                workspace_root.mkdir(parents=True, exist_ok=True)
                pointer.write_text(workspace_id + "\n", encoding="utf-8")
                issue["fixed"] = True
            issues.append(issue)

        expected_path, expected_payload = build_vscode_workspace(config, data)
        actual_payload = None
        if expected_path.exists():
            try:
                actual_payload = json.loads(expected_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                actual_payload = None
        if actual_payload != expected_payload:
            issue = doctor_issue(
                "warning",
                "vscode-workspace",
                f"Missing or stale VS Code workspace file: {expected_path}",
                True,
            )
            if fix:
                write_vscode_workspace(config, data)
                issue["fixed"] = True
            issues.append(issue)

    for repo in data.get("repos", []):
        path = repo.get("worktreePath")
        name = repo.get("name") or path or "?"
        if not path or not Path(path).exists():
            issues.append(doctor_issue("error", "repo-missing", f"Repo worktree missing: {name}"))
            continue
        try:
            info = git_info(path, config)
        except SystemExit as error:
            issues.append(doctor_issue("error", "repo-not-git", f"Repo is not valid git: {name}: {error}"))
            continue
        if info.get("branch") != repo.get("branch"):
            issues.append(
                doctor_issue(
                    "warning",
                    "repo-branch-drift",
                    f"Repo branch drift for {name}: ledger={repo.get('branch')} current={info.get('branch')}",
                )
            )
        if info.get("dirty"):
            issues.append(doctor_issue("warning", "repo-dirty", f"Repo has uncommitted changes: {name}"))

    if fix:
        data = load_workspace(config, workspace_id)
        save_workspace(config, data)
    return issues


def print_doctor_report(workspace_id, issues):
    if not issues:
        print(f"{workspace_id}: ok")
        return
    print(f"{workspace_id}: {len(issues)} issue(s)")
    for issue in issues:
        if issue.get("fixed"):
            prefix = "fixed"
            suffix = ""
        else:
            prefix = issue["severity"]
            suffix = " fixable" if issue.get("fixable") else ""
        print(f"- {prefix} {issue['code']}{suffix}: {issue['message']}")


def cmd_doctor(args):
    config = load_config()
    if args.all:
        workspace_ids = [data["id"] for data in iter_workspaces(config)]
    elif args.id:
        workspace_ids = args.id
    else:
        workspace_ids = [find_workspace_from_cwd(config)]
    if not workspace_ids:
        raise SystemExit("No workspaces found.")
    results = []
    for workspace_id in workspace_ids:
        issues = doctor_workspace(config, workspace_id, fix=args.fix)
        results.append({"workspace": workspace_id, "issues": issues})
    if args.json:
        if len(results) == 1:
            print(json.dumps(results[0], indent=2))
        else:
            print(json.dumps(results, indent=2))
        return
    for result in results:
        print_doctor_report(result["workspace"], result["issues"])
    if args.fix:
        for result in results:
            if any(issue.get("fixed") for issue in result["issues"]):
                data = load_workspace(config, result["workspace"])
                print_vscode_workspace_block(config, data)


def repo_summary(data, config):
    repo_names = []
    dirty = 0
    ahead = 0
    missing = 0
    for repo in data.get("repos", []):
        repo_names.append(repo.get("name") or "?")
        path = repo.get("worktreePath")
        if not path or not Path(path).exists():
            missing += 1
            continue
        info = git_info(path, config)
        if info["dirty"]:
            dirty += 1
        if info["aheadOfBase"]:
            ahead += info["aheadOfBase"]

    local = []
    if ahead:
        local.append(f"+{ahead}")
    if dirty:
        local.append(f"{dirty} dirty")
    if missing:
        local.append(f"{missing} missing")
    return ",".join(repo_names) or "-", " ".join(local) or "clean"


def cmd_list(args):
    if args.json and args.ids_only:
        raise SystemExit("--json and --ids-only are mutually exclusive.")

    config = load_config()
    rows = []
    for data in iter_workspaces(config):
        state = data.get("state", "")
        if args.state and state != args.state:
            continue
        active_only = args.active or (not args.all and not args.state)
        if active_only and state in DONE_STATES:
            continue
        repos, local = repo_summary(data, config)
        prs = len(data.get("links", {}).get("githubPrs", []))
        rows.append(
            {
                "id": data.get("id", ""),
                "state": state,
                "updated": str(data.get("updatedAt", ""))[:10],
                "repos": repos,
                "local": local,
                "prs": str(prs),
                "title": data.get("title", ""),
            }
        )

    rows.sort(key=lambda row: (STATE_ORDER.get(row["state"], 99), row["updated"], row["id"]))
    if not rows:
        if args.json:
            print(json.dumps([]))
        elif not args.ids_only:
            print("No workspaces found.")
        return

    if args.ids_only:
        for row in rows:
            print(row["id"])
        return

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    state_counts = {}
    for row in rows:
        state_counts[row["state"]] = state_counts.get(row["state"], 0) + 1
    summary = "  ".join(f"{state}:{count}" for state, count in sorted(state_counts.items()))
    scope = args.state or ("all" if args.all else "active")
    print(f"Workspaces ({scope}): {len(rows)}  {summary}")
    print("")
    print(f"{'ID':32} {'STATE':19} {'UPDATED':10} {'REPOS':24} {'LOCAL':12} {'PRS':3} TITLE")
    for row in rows:
        print(
            f"{row['id'][:32]:32} "
            f"{row['state'][:19]:19} "
            f"{row['updated'][:10]:10} "
            f"{row['repos'][:24]:24} "
            f"{row['local'][:12]:12} "
            f"{row['prs'][:3]:3} "
            f"{row['title']}"
        )


def cmd_note(args):
    config = load_config()
    append_note(config, args.id, args.text)
    data = load_workspace(config, args.id)
    save_workspace(config, data)


def cmd_close(args):
    config = load_config()
    data = load_workspace(config, args.id)
    data["state"] = "closed"
    save_workspace(config, data)
    append_note(config, args.id, "Closed workspace.")


def cmd_state(args):
    config = load_config()
    data = load_workspace(config, args.id)
    old_state = data.get("state")
    data["state"] = args.state
    save_workspace(config, data)
    note = args.note or f"State changed from `{old_state}` to `{args.state}`."
    append_note(config, args.id, note)


def print_issue_row(issue, *, sandcastle=False):
    meta = issue["meta"]
    blockers = ",".join(str(item) for item in meta.get("blockedBy") or []) or "-"
    if sandcastle:
        sc = sandcastle_issue_meta(meta)
        print(
            f"{str(meta.get('id', ''))[:16]:16} "
            f"{str(sc.get('type', ''))[:4]:4} "
            f"{str(meta.get('status', ''))[:12]:12} "
            f"{blockers[:18]:18} "
            f"{str(sc.get('reviewStatus', ''))[:12]:12} "
            f"{meta.get('title', '')}"
        )
    else:
        print(
            f"{str(meta.get('id', ''))[:16]:16} "
            f"{str(meta.get('status', ''))[:12]:12} "
            f"{blockers[:18]:18} "
            f"{meta.get('title', '')}"
        )


def cmd_issue_create(args):
    config = load_config()
    sandcastle = getattr(args, "sandcastle_mode", False)
    data = load_workspace(config, args.workspace_id)
    path = issue_path(config, args.workspace_id, args.issue_id)
    if path.exists() and not args.force:
        raise SystemExit(f"Issue already exists: {path}")
    meta = {
        "id": args.issue_id,
        "title": args.title,
        "status": args.status,
        "blockedBy": args.blocked_by or [],
    }
    if sandcastle:
        meta["type"] = args.type.upper()
    if args.branch:
        meta["branch"] = args.branch
    if getattr(args, "repo", None):
        meta["repo"] = args.repo
    else:
        default_repo = default_issue_repo(data.get("repos", []))
        if default_repo:
            meta["repo"] = default_repo
    body = issue_body_from_args(args)
    path, meta = write_issue(
        config, args.workspace_id, args.issue_id, meta, body, sandcastle=sandcastle
    )
    data = load_workspace(config, args.workspace_id)
    save_workspace(config, data)
    append_note(config, args.workspace_id, f"Created issue `{meta['id']}`: {meta['title']}")
    print(str(path))


def cmd_issue_list(args):
    config = load_config()
    sandcastle = getattr(args, "sandcastle_mode", False)
    load_workspace(config, args.workspace_id)
    issues = iter_issues(config, args.workspace_id)
    if args.status:
        issues = [issue for issue in issues if issue["meta"].get("status") == args.status]
    if sandcastle and getattr(args, "type", None):
        issue_type = args.type.upper()
        issues = [
            issue
            for issue in issues
            if sandcastle_issue_meta(issue["meta"])["type"] == issue_type
        ]
    if not args.all:
        issues = [
            issue
            for issue in issues
            if issue["meta"].get("status") not in ISSUE_DONE_STATUSES
        ]
    if not issues:
        print("No issues found.")
        return
    if sandcastle:
        print(f"{'ID':16} {'TYPE':4} {'STATUS':12} {'BLOCKED BY':18} {'REVIEW':12} TITLE")
    else:
        print(f"{'ID':16} {'STATUS':12} {'BLOCKED BY':18} TITLE")
    for issue in issues:
        print_issue_row(issue, sandcastle=sandcastle)


def cmd_issue_ready(args):
    config = load_config()
    sandcastle = getattr(args, "sandcastle_mode", False)
    load_workspace(config, args.workspace_id)
    ready, hitl = ready_issues(config, args.workspace_id, sandcastle=sandcastle)
    if args.json:
        payload = {
            "issues": [
                {
                    "id": issue["meta"]["id"],
                    "title": issue["meta"]["title"],
                    "branch": issue["meta"]["branch"],
                    "status": issue["meta"]["status"],
                    "path": str(issue["path"]),
                    **(
                        {
                            "type": sandcastle_issue_meta(issue["meta"])["type"],
                        }
                        if sandcastle
                        else {}
                    ),
                }
                for issue in ready
            ],
        }
        if sandcastle:
            payload["hitl"] = [
                {
                    "id": issue["meta"]["id"],
                    "title": issue["meta"]["title"],
                    "status": issue["meta"]["status"],
                    "path": str(issue["path"]),
                }
                for issue in hitl
            ]
        print(json.dumps(payload, indent=2))
        return
    if sandcastle:
        if ready:
            print("Ready AFK issues:")
            print(f"{'ID':16} {'TYPE':4} {'STATUS':12} {'BLOCKED BY':18} {'REVIEW':12} TITLE")
            for issue in ready:
                print_issue_row(issue, sandcastle=True)
        else:
            print("Ready AFK issues: none")
        if hitl:
            print("")
            print("Ready HITL issues:")
            print(f"{'ID':16} {'TYPE':4} {'STATUS':12} {'BLOCKED BY':18} {'REVIEW':12} TITLE")
            for issue in hitl:
                print_issue_row(issue, sandcastle=True)
    else:
        if ready:
            print("Ready issues:")
            print(f"{'ID':16} {'STATUS':12} {'BLOCKED BY':18} TITLE")
            for issue in ready:
                print_issue_row(issue, sandcastle=False)
        else:
            print("Ready issues: none")


def cmd_issue_show(args):
    config = load_config()
    load_workspace(config, args.workspace_id)
    path = issue_path(config, args.workspace_id, args.issue_id)
    if not path.exists():
        raise SystemExit(f"Issue not found: {args.issue_id}")
    print(path.read_text(encoding="utf-8").rstrip())


def cmd_issue_set_status(args):
    config = load_config()
    sandcastle = getattr(args, "sandcastle_mode", False)
    load_workspace(config, args.workspace_id)
    path, meta, old_status = set_issue_status(
        config,
        args.workspace_id,
        args.issue_id,
        args.status,
        review_status=getattr(args, "review_status", None) if sandcastle else None,
        branch=args.branch,
        sandcastle=sandcastle,
    )
    data = load_workspace(config, args.workspace_id)
    save_workspace(config, data)
    note = args.note or f"Issue `{meta['id']}` status changed from `{old_status}` to `{args.status}`."
    append_note(config, args.workspace_id, note)
    maybe_mark_user_review(config, args.workspace_id)
    print(str(path))


def cmd_cleanup_plan(args):
    config = load_config()
    workspace_ids = args.id
    if args.all:
        workspace_ids = [data["id"] for data in iter_workspaces(config)]
    if not workspace_ids:
        raise SystemExit("Pass at least one workspace id, or --all.")
    if args.issues:
        if args.write:
            raise SystemExit("--write is only supported for workspace cleanup plans.")
        candidates = cleanup_candidates(config, workspace_ids, repo_name=args.repo)
        if args.json:
            print(json.dumps({"candidates": candidates}, indent=2))
            return
        print_cleanup_plan(candidates)
        return
    candidates = workspace_cleanup_candidates(config, workspace_ids)
    plan_path = None
    if args.write:
        plan_path, _ = write_cleanup_plan(config, candidates)
    if args.json:
        payload = {"candidates": candidates}
        if plan_path:
            payload["planPath"] = str(plan_path)
        print(json.dumps(payload, indent=2))
        return
    print_workspace_cleanup_plan(candidates)
    if plan_path:
        print(f"\nWrote cleanup plan: {plan_path}")


def cmd_cleanup(args):
    config = load_config()
    if args.force:
        if args.from_plan:
            raise SystemExit("--force cannot be used with --from-plan")
        if args.item:
            raise SystemExit("--force is only supported for workspace cleanup")
    if args.item and (args.id or args.from_plan):
        raise SystemExit("Pass workspace ids/from-plan or --item values, not both.")
    if args.from_plan and not args.id:
        raise SystemExit("--from-plan requires one or more approved workspace ids.")
    results = []
    if args.item:
        for item in args.item:
            results.append(cleanup_item(config, item, repo_name=args.repo))
    else:
        if not args.id:
            raise SystemExit("Pass one or more workspace ids, or --item WORKSPACE:ISSUE values.")
        plan = load_cleanup_plan(args.from_plan) if args.from_plan else None
        for workspace_id in args.id:
            if plan:
                validate_cleanup_plan_candidate(config, plan, workspace_id)
            results.append(cleanup_workspace(config, workspace_id, force=args.force))
    if args.json:
        print(json.dumps({"cleaned": results}, indent=2))
        return
    for result in results:
        label = result.get("item") or result.get("workspace")
        action = result.get("cleanupAction") or result.get("recommendedAction")
        print(f"Cleaned {label}: {action}")


def cmd_sync_github(args):
    config = load_config()
    if shutil.which("gh") is None:
        raise SystemExit("gh is not installed or not on PATH")
    data = load_workspace(config, args.id)
    links = data.setdefault("links", {})
    existing = links.setdefault("githubPrs", [])
    seen = []
    for repo in data.get("repos", []):
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
        replaced = False
        for index, old in enumerate(existing):
            if old.get("repo") == item.get("repo") and old.get("branch") == item.get("branch"):
                existing[index] = {**old, **item}
                replaced = True
                break
        if not replaced:
            existing.append(item)
    save_workspace(config, data)
    append_note(config, args.id, f"Synced GitHub PR snapshots. Found {len(seen)} PR(s).")
    print(f"Found {len(seen)} PR(s).")


def cmd_pr(args):
    config = load_config()
    data = load_workspace(config, args.id)
    state_by_event = {
        "opened": "pr-review",
        "feedback": "review-feedback",
        "merged": "dev-complete",
    }
    data["state"] = state_by_event[args.event]
    if args.url or args.number:
        links = data.setdefault("links", {})
        prs = links.setdefault("githubPrs", [])
        item = {
            "url": args.url,
            "number": args.number,
            "state": "MERGED" if args.event == "merged" else "OPEN",
            "merged": args.event == "merged",
            "lastSeenAt": now(),
        }
        prs.append({key: value for key, value in item.items() if value is not None})
    save_workspace(config, data)
    note = args.note or f"PR lifecycle event `{args.event}` set workspace state to `{data['state']}`."
    append_note(config, args.id, note)
    print(data["state"])


def add_issue_subcommands(issue_sub, *, sandcastle=False):
    create_help = (
        "Create a local issue with YAML frontmatter."
        if not sandcastle
        else "Create a local issue with YAML frontmatter, including AFK/HITL type for Sandcastle runs."
    )
    ready_help = (
        "List dependency-ready issues."
        if not sandcastle
        else "List dependency-ready AFK issues and ready HITL issues for Sandcastle."
    )
    set_status_help = (
        "Update issue status."
        if not sandcastle
        else "Update issue status, including Sandcastle reviewStatus when applicable."
    )

    p = issue_sub.add_parser("create", help=create_help)
    p.add_argument("workspace_id")
    p.add_argument("issue_id")
    p.add_argument("--title", required=True)
    if sandcastle:
        p.add_argument("--type", default="AFK", help="AFK or HITL")
    p.add_argument("--status", default="planned")
    p.add_argument("--blocked-by", action="append")
    p.add_argument("--acceptance", action="append")
    p.add_argument("--body")
    p.add_argument("--branch")
    p.add_argument("--repo", help="Workspace repo this issue targets")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_issue_create)

    p = issue_sub.add_parser("list")
    p.add_argument("workspace_id")
    p.add_argument("--status")
    if sandcastle:
        p.add_argument("--type", help="Filter by AFK or HITL")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_issue_list)

    p = issue_sub.add_parser("ready", help=ready_help)
    p.add_argument("workspace_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_issue_ready)

    p = issue_sub.add_parser("show")
    p.add_argument("workspace_id")
    p.add_argument("issue_id")
    p.set_defaults(func=cmd_issue_show)

    p = issue_sub.add_parser("set-status", help=set_status_help)
    p.add_argument("workspace_id")
    p.add_argument("issue_id")
    p.add_argument("status")
    if sandcastle:
        p.add_argument("--review-status", help="Sandcastle review status")
    p.add_argument("--branch")
    p.add_argument("--note")
    p.set_defaults(func=cmd_issue_set_status)


def build_parser(*, sandcastle=False, prog=None):
    description = SANDCASTLE_DESCRIPTION if sandcastle else BASE_DESCRIPTION
    parser = argparse.ArgumentParser(description=description, prog=prog)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-config")
    p.add_argument("--force", action="store_true")
    p.add_argument("--non-interactive", action="store_true")
    p.set_defaults(func=cmd_init_config)

    p = sub.add_parser("config")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("create")
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--state", default="captured")
    p.add_argument("--input")
    p.add_argument("--repo", action="append")
    p.add_argument("--branch")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("adopt")
    p.add_argument("path")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--name")
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("status")
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("open")
    p.add_argument("id")
    p.add_argument("--command")
    p.add_argument("--print", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("doctor")
    p.add_argument("id", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--fix", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("list")
    p.add_argument("--state")
    p.add_argument("--active", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--ids-only", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("note")
    p.add_argument("id")
    p.add_argument("text")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("close")
    p.add_argument("id")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("state")
    p.add_argument("id")
    p.add_argument("state")
    p.add_argument("--note")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("sync-github")
    p.add_argument("id")
    p.set_defaults(func=cmd_sync_github)

    p = sub.add_parser("pr")
    p.add_argument("id")
    p.add_argument("event", choices=["opened", "feedback", "merged"])
    p.add_argument("--url")
    p.add_argument("--number", type=int)
    p.add_argument("--note")
    p.set_defaults(func=cmd_pr)

    repo = sub.add_parser("repo")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)

    p = repo_sub.add_parser("list")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_repo_list)

    p = repo_sub.add_parser("add")
    p.add_argument("id")
    p.add_argument("repo")
    p.add_argument("--branch")
    p.set_defaults(func=cmd_repo_add)

    p = repo_sub.add_parser("adopt")
    p.add_argument("id")
    p.add_argument("path")
    p.add_argument("--name")
    p.set_defaults(func=cmd_repo_adopt)

    p = repo_sub.add_parser("remove")
    p.add_argument("id")
    p.add_argument("repo")
    p.add_argument("--delete-worktree", action="store_true")
    p.set_defaults(func=cmd_repo_remove)

    p = repo_sub.add_parser("refresh")
    p.add_argument("id")
    p.add_argument("repo", nargs="?")
    p.set_defaults(func=cmd_repo_refresh)

    p = sub.add_parser("cleanup-plan")
    p.add_argument("id", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--issues", action="store_true")
    p.add_argument("--repo")
    p.add_argument("--write", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cleanup_plan)

    p = sub.add_parser("cleanup")
    p.add_argument("id", nargs="*")
    p.add_argument("--item", action="append")
    p.add_argument("--from-plan")
    p.add_argument("--repo")
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cleanup)

    if sandcastle:
        sandcastle = sub.add_parser("sandcastle")
        sandcastle_sub = sandcastle.add_subparsers(dest="sandcastle_command", required=True)

        p = sandcastle_sub.add_parser("plan", help="Plan dependency-ordered Sandcastle execution")
        p.add_argument("id")
        p.add_argument("--repo", help="Limit planning to one workspace repo")
        p.add_argument(
            "--limit",
            type=int,
            help="Cap the seed selection of ready AFK issues (dependency closure may still add more to the locked scope)",
        )
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=cmd_sandcastle_plan)

        p = sandcastle_sub.add_parser("init-runner", help="Install the Sandcastle runner scaffold")
        p.add_argument("id")
        p.add_argument("--repo")
        p.add_argument("--force", action="store_true")
        p.set_defaults(func=cmd_sandcastle_init_runner)

        p = sandcastle_sub.add_parser(
            "reconcile-result",
            help="Apply a Sandcastle result file to workspace issues",
        )
        p.add_argument("id")
        p.add_argument("result_path")
        p.set_defaults(func=cmd_sandcastle_reconcile_result)

        p = sandcastle_sub.add_parser("execute", help="Execute a Sandcastle plan")
        p.add_argument("id")
        p.add_argument("--plan", help="Explicit plan artifact path override")
        p.add_argument("--command", required=True, help="Shell command to run per repo per wave")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=cmd_sandcastle_execute)

    issue = sub.add_parser("issue")
    issue_sub = issue.add_subparsers(dest="issue_command", required=True)
    add_issue_subcommands(issue_sub, sandcastle=sandcastle)

    return parser


def main(*, sandcastle=False, prog=None):
    parser = build_parser(sandcastle=sandcastle, prog=prog)
    args = parser.parse_args()
    args.sandcastle_mode = sandcastle
    args.func(args)
