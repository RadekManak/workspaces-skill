"""Shared Sandcastle workspace state: plan pointer and eager invalidation."""
from .common import repo_identity_snapshot
from .issues import sandcastle_issue_meta


def current_plan(data):
    return (data.get("sandcastle") or {}).get("currentPlan")


def targeted_issue_ids(data):
    pointer = current_plan(data)
    if not pointer:
        return set()
    return {str(item) for item in pointer.get("targetedIssueIds") or []}


def clear_current_plan_pointer(data):
    sandcastle = dict(data.get("sandcastle") or {})
    if "currentPlan" not in sandcastle:
        return False
    sandcastle.pop("currentPlan", None)
    if sandcastle:
        data["sandcastle"] = sandcastle
    else:
        data.pop("sandcastle", None)
    return True


def invalidate_current_plan(ledger, workspace_id, *, reason):
    data = ledger.load(workspace_id)
    if not clear_current_plan_pointer(data):
        return False
    ledger.save(data)
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
    data = ledger.load(workspace_id)
    if issue_id not in targeted_issue_ids(data):
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


def maybe_invalidate_plan_for_repo_change(ledger, workspace_id, old_data, new_data):
    if not current_plan(new_data):
        return False
    old_repos = repo_identity_snapshot(old_data.get("repos"))
    new_repos = repo_identity_snapshot(new_data.get("repos"))
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
