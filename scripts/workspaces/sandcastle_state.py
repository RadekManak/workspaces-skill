"""Shared Sandcastle workspace state: plan pointer and eager invalidation."""
from .common import repo_identity_snapshot
from .issues import sandcastle_issue_meta


def invalidate_current_plan(ledger, workspace_id, *, reason):
    workspace = ledger.load(workspace_id)
    if not workspace.clear_current_sandcastle_plan():
        return False
    ledger.save(workspace)
    ledger.append_note(workspace_id, reason)
    return True


def _sandcastle_meta_snapshot(meta):
    block = sandcastle_issue_meta(meta)
    return {
        "blockedBy": sorted(str(item) for item in meta.get("blockedBy") or []),
        "branch": meta.get("branch"),
        "type": block["type"],
        "reviewStatus": block["reviewStatus"],
    }


def maybe_invalidate_plan_for_issue_edit(ledger, workspace_id, issue_id, old_meta, new_meta):
    workspace = ledger.load(workspace_id)
    if issue_id not in workspace.targeted_issue_ids():
        return False
    old_snapshot = _sandcastle_meta_snapshot(old_meta)
    new_snapshot = _sandcastle_meta_snapshot(new_meta)
    if old_snapshot == new_snapshot:
        return False
    return invalidate_current_plan(
        ledger,
        workspace_id,
        reason=(
            f"Sandcastle current plan invalidated because targeted issue `{issue_id}` "
            f"Sandcastle planning inputs changed ({', '.join(sorted(old_snapshot.keys()))}). "
            f"Run `sandcastle plan` again."
        ),
    )


def maybe_invalidate_plan_for_repo_change(ledger, workspace_id, old_workspace, new_workspace):
    if not new_workspace.current_sandcastle_plan:
        return False
    old_repos = repo_identity_snapshot(old_workspace.repos)
    new_repos = repo_identity_snapshot(new_workspace.repos)
    if old_repos == new_repos:
        return False
    return invalidate_current_plan(
        ledger,
        workspace_id,
        reason=(
            "Sandcastle current plan invalidated because the workspace repo set changed. "
            "Run `sandcastle plan` again."
        ),
    )
