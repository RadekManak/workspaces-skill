"""Health checks for a single workspace.

Detects and (optionally) fixes: missing ledger directories/files, a missing
or stale workspace pointer, a stale `.code-workspace` file, and repo
worktree problems (missing worktree, branch drift, uncommitted changes).
"""
from pathlib import Path

from workspaces.common import (
    WORKSPACE_POINTER,
    parse_json,
    template,
    write_workspace_pointer,
)
from workspaces.git import git_info
from workspaces.ledger import WorkspaceLedger


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


def check_workspace(config, workspace, fix=False):
    """Run health checks against an already-loaded `workspace`.

    When `fix` is True, issues are repaired in place (filesystem side
    effects only — this never mutates `workspace` itself) and `workspace`
    is unconditionally persisted via `WorkspaceLedger(config).save(...)`
    at the end, regardless of whether any issues were found or fixed.
    """
    ledger = WorkspaceLedger(config)
    workspace_id = workspace.id
    issues = []
    root = ledger.ledger_dir(workspace_id)
    workspace_root = ledger.workspace_root_path(workspace_id)

    required_dirs = [root / "runs", root / "issues"]
    required_files = {
        root / "spec.md": template("spec.md").format(
            id=workspace_id,
            title=workspace.to_dict().get("title") or workspace_id,
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

    if workspace.cleanup_status != "done":
        pointer = workspace_root / WORKSPACE_POINTER
        if not pointer.exists() or pointer.read_text(encoding="utf-8").strip() != workspace_id:
            issue = doctor_issue(
                "warning",
                "workspace-pointer",
                f"Missing or stale workspace pointer: {pointer}",
                True,
            )
            if fix:
                write_workspace_pointer(workspace_root, workspace_id)
                issue["fixed"] = True
            issues.append(issue)

        expected_path, expected_payload = ledger.build_vscode_workspace(workspace)
        actual_payload = None
        if expected_path.exists():
            actual_payload = parse_json(expected_path.read_text(encoding="utf-8"))
        if actual_payload != expected_payload:
            issue = doctor_issue(
                "warning",
                "vscode-workspace",
                f"Missing or stale VS Code workspace file: {expected_path}",
                True,
            )
            if fix:
                ledger.write_vscode_workspace(workspace)
                issue["fixed"] = True
            issues.append(issue)

    for repo in workspace.repos:
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
        ledger.save(workspace)
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
