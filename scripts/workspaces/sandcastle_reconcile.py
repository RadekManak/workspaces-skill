"""Sandcastle reconciliation: applying runner result files and provisioning runners.

Covers the two Sandcastle-runner-facing operations that don't fit the plan/execute
lifecycle itself: reconciling a runner's reported issue-status updates back onto the
ledger (`apply_sandcastle_result`), and scaffolding a worktree's `.sandcastle`
directory with the runner's prompt/entrypoint templates (`init_sandcastle_runner`).
"""
import json
import shutil
from pathlib import Path

from workspaces import issues as issue_model
from workspaces.common import SKILL_ROOT
from workspaces.ledger import WorkspaceLedger


def apply_sandcastle_result(config, workspace_id, result_path, *, skip_plan_invalidation=False):
    ledger = WorkspaceLedger(config)
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
            issue_path_, meta, old_status = issue_model.set_issue_status(
                ledger,
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

    workspace = ledger.load(workspace_id)
    ledger.save(workspace)
    summary = f"Reconciled Sandcastle result `{path}` with {len(applied)} issue update(s)"
    if failures:
        summary += f", {len(failures)} entr{'y' if len(failures) == 1 else 'ies'} failed to apply"
    ledger.append_note(workspace_id, summary + ".")
    for meta, old_status, note, _ in applied:
        detail = note or f"Issue `{meta['id']}` status changed from `{old_status}` to `{meta['status']}`."
        ledger.append_note(workspace_id, detail)
    for index, update, error in failures:
        entry_id = update.get("id") if isinstance(update, dict) else None
        ledger.append_note(
            workspace_id,
            f"Sandcastle result `{path}` entry {index} (id={entry_id!r}) failed to apply: {error}",
        )
    issue_model.maybe_mark_user_review(ledger, workspace_id)
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
