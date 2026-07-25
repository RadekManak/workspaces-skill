#!/usr/bin/env python3
"""Unit self-checks for `workspaces.cleanup` safety classification."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import support
from tests.support import assert_contains, make_source_repo, run

from workspaces import github_sync
from workspaces.cleanup import CleanupSafety
from workspaces.common import DEFAULT_CONFIG
from workspaces.git import branch_exists
from workspaces.issues import write_issue
from workspaces.repo import select_repo


@contextlib.contextmanager
def no_gh():
    """Hide `gh` so recorded PR snapshots are never refreshed over the network."""
    original_which = github_sync.shutil.which
    github_sync.shutil.which = lambda *args, **kwargs: None
    try:
        yield
    finally:
        github_sync.shutil.which = original_which


def _cleanup_test_config(tmp_dir):
    config = dict(DEFAULT_CONFIG)
    config["source_root"] = str(tmp_dir / "src")
    config["workspace_root"] = str(tmp_dir / "workspaces")
    config["ledger_root"] = str(tmp_dir / "ledger")
    return config


def _workspace_with_worktree(tmp_dir, workspace_id, *, branch="feature", state="dev-complete"):
    """Build a done workspace whose single repo is a clean linked worktree."""
    config = _cleanup_test_config(tmp_dir)
    safety = CleanupSafety(config, select_repo=select_repo)
    source = make_source_repo(tmp_dir, "app")
    workspace = safety.ledger.ensure(workspace_id, title=workspace_id, state=state)
    worktree = safety.ledger.workspace_root_path(workspace_id) / "app"
    run(["git", "-C", str(source), "worktree", "add", "-b", branch, str(worktree)])
    workspace.upsert_repo(
        {
            "name": "app",
            "worktreePath": str(worktree),
            "sourcePath": str(source),
            "branch": branch,
        }
    )
    safety.ledger.save(workspace)
    return safety, workspace, source, worktree


def _capture_stdout(callable_):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        callable_()
    return buffer.getvalue()


def test_cleanup_workspace_candidate_is_safe_to_remove_when_done_and_clean():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        safety, workspace, _, _ = _workspace_with_worktree(Path(tmp_dir), "CL-SAFE")
        with no_gh():
            candidate = safety.workspace_candidate(workspace.id)
        if candidate["recommendedAction"] != "safe-to-remove":
            raise AssertionError(f"Expected a done, clean workspace to be safe-to-remove, got {candidate}")
        if candidate["existingRepoCount"] != 1 or candidate["repos"][0]["linkedWorktree"] is not True:
            raise AssertionError(f"Expected one existing linked worktree, got {candidate['repos']}")


def test_cleanup_workspace_candidate_soft_reasons_are_force_eligible():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        safety, workspace, _, worktree = _workspace_with_worktree(tmp_dir, "CL-SOFT", state="in-progress")
        (worktree / "wip.txt").write_text("wip\n", encoding="utf-8")
        (safety.ledger.workspace_root_path(workspace.id) / "stray.txt").write_text("x\n", encoding="utf-8")
        workspace.record_github_pr({"repo": "app", "branch": "feature", "number": 1, "state": "OPEN"})
        safety.ledger.save(workspace)

        with no_gh():
            candidate = safety.workspace_candidate(workspace.id)
        if candidate["recommendedAction"] != "force-eligible":
            raise AssertionError(f"Expected soft reasons alone to be force-eligible, got {candidate}")
        for fragment in (
            "workspace is not closed or dev-complete",
            "repo worktree has uncommitted changes",
            "workspace root contains extra files",
            "workspace has open PR snapshots",
        ):
            assert_contains(candidate["reason"], fragment)
        if candidate["dirtyRepos"] != ["app"] or candidate["openPrCount"] != 1:
            raise AssertionError(f"Expected the dirty repo and open PR to be counted, got {candidate}")


def test_cleanup_workspace_candidate_hard_reasons_need_review():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        config = _cleanup_test_config(tmp_dir)
        safety = CleanupSafety(config, select_repo=select_repo)
        source = make_source_repo(tmp_dir, "app")
        workspace = safety.ledger.ensure("CL-HARD", title="Hard reasons", state="closed")
        workspace.upsert_repo({"name": "app", "worktreePath": str(source), "branch": "main"})
        safety.ledger.save(workspace)

        with no_gh():
            candidate = safety.workspace_candidate("CL-HARD")
        if candidate["recommendedAction"] != "needs-review":
            raise AssertionError(f"Expected a non-linked, external repo to need review, got {candidate}")
        assert_contains(candidate["reason"], "repo is not a linked git worktree")
        assert_contains(candidate["reason"], "repo worktree is outside the workspace root")

        with no_gh():
            try:
                safety.cleanup_workspace("CL-HARD", force=True)
                raise AssertionError("Expected cleanup to refuse a needs-review workspace even with --force")
            except SystemExit:
                pass


def test_cleanup_workspace_candidate_marks_done_when_worktrees_are_gone():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        config = _cleanup_test_config(tmp_dir)
        safety = CleanupSafety(config, select_repo=select_repo)
        workspace = safety.ledger.ensure("CL-GONE", title="Already gone", state="closed")
        workspace.upsert_repo({"name": "app", "worktreePath": str(tmp_dir / "gone" / "app"), "branch": "feature"})
        safety.ledger.save(workspace)
        # `save` writes the .code-workspace file, which recreates the workspace root.
        shutil.rmtree(safety.ledger.workspace_root_path("CL-GONE"))

        with no_gh():
            candidate = safety.workspace_candidate("CL-GONE")
        if candidate["recommendedAction"] != "mark-done":
            raise AssertionError(f"Expected an already-gone workspace to be mark-done, got {candidate}")
        if candidate["missingRepos"] != ["app"]:
            raise AssertionError(f"Expected the missing repo to be reported, got {candidate}")

        with no_gh():
            result = safety.cleanup_workspace("CL-GONE")
        if result["cleanupAction"] != "mark-done" or result["cleanupStatus"] != "done":
            raise AssertionError(f"Expected cleanup to record a mark-done cleanup, got {result}")


def test_cleanup_workspace_force_removes_dirty_worktrees_and_notes_reason():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        safety, workspace, _, worktree = _workspace_with_worktree(tmp_dir, "CL-FORCE")
        (worktree / "wip.txt").write_text("wip\n", encoding="utf-8")
        run(["git", "-C", str(worktree), "add", "wip.txt"])
        run(["git", "-C", str(worktree), "commit", "-m", "wip"])
        (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with no_gh():
            try:
                safety.cleanup_workspace(workspace.id)
                raise AssertionError("Expected a force-eligible workspace to refuse cleanup without force")
            except SystemExit:
                pass
            result = safety.cleanup_workspace(workspace.id, force=True)

        if result["cleanupAction"] != "force-removed":
            raise AssertionError(f"Expected a force-removed cleanup action, got {result}")
        if safety.ledger.workspace_root_path(workspace.id).exists():
            raise AssertionError("Expected the workspace root to be removed")
        notes = (safety.ledger.ledger_dir(workspace.id) / "notes.md").read_text(encoding="utf-8")
        assert_contains(notes, "Force-cleaned workspace.")
        assert_contains(notes, "Ahead of base: 1.")
        assert_contains(notes, "Task branches kept on source repos.")


def test_cleanup_plan_round_trip_and_validation():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        safety, workspace, _, worktree = _workspace_with_worktree(tmp_dir, "CL-PLAN")
        with no_gh():
            candidates = safety.workspace_candidates([workspace.id])
        path, payload = safety.write_plan(candidates)
        if payload["type"] != "workspace-cleanup" or not path.exists():
            raise AssertionError(f"Expected a workspace-cleanup plan on disk, got {payload}")

        plan = CleanupSafety.load_plan(path)
        with no_gh():
            current = safety.validate_plan_candidate(plan, workspace.id)
        if current["recommendedAction"] != "safe-to-remove":
            raise AssertionError(f"Expected an unchanged workspace to validate, got {current}")

        with no_gh():
            try:
                safety.validate_plan_candidate(plan, "CL-MISSING")
                raise AssertionError("Expected a workspace absent from the plan to be rejected")
            except SystemExit:
                pass

            # Drift the workspace after planning: the plan must be treated as stale.
            (worktree / "wip.txt").write_text("wip\n", encoding="utf-8")
            try:
                safety.validate_plan_candidate(plan, workspace.id)
                raise AssertionError("Expected a stale plan to be rejected")
            except SystemExit:
                pass


def test_cleanup_load_plan_rejects_unusable_plans():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        missing = tmp_dir / "missing.json"
        invalid = tmp_dir / "invalid.json"
        invalid.write_text("{not json", encoding="utf-8")
        wrong_type = tmp_dir / "wrong.json"
        wrong_type.write_text(json.dumps({"type": "issue-cleanup", "candidates": []}), encoding="utf-8")

        for path in (missing, invalid, wrong_type):
            try:
                CleanupSafety.load_plan(path)
                raise AssertionError(f"Expected load_plan to reject {path.name}")
            except SystemExit:
                pass


def test_cleanup_validate_plan_candidate_rejects_unapproved_action():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        safety, workspace, _, _ = _workspace_with_worktree(tmp_dir, "CL-UNAPPROVED")
        plan = {
            "type": "workspace-cleanup",
            "candidates": [{"workspace": workspace.id, "recommendedAction": "force-eligible"}],
        }
        with no_gh():
            try:
                safety.validate_plan_candidate(plan, workspace.id)
                raise AssertionError("Expected a force-eligible plan entry to not count as approval")
            except SystemExit:
                pass


def test_cleanup_issue_candidates_classify_each_child_branch():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        safety, workspace, _, worktree = _workspace_with_worktree(tmp_dir, "CL-ISSUES")
        ledger = safety.ledger

        run(["git", "-C", str(worktree), "branch", "unmerged-child"])
        merged_tree = tmp_dir / "merged-tree"
        run(["git", "-C", str(worktree), "worktree", "add", "-b", "merged-child", str(merged_tree)])
        unmerged_tree = tmp_dir / "unmerged-tree"
        run(["git", "-C", str(worktree), "worktree", "add", str(unmerged_tree), "unmerged-child"])
        (unmerged_tree / "child.txt").write_text("child\n", encoding="utf-8")
        run(["git", "-C", str(unmerged_tree), "add", "child.txt"])
        run(["git", "-C", str(unmerged_tree), "commit", "-m", "child"])

        dirty_tree = tmp_dir / "dirty-tree"
        run(["git", "-C", str(worktree), "worktree", "add", "-b", "dirty-child", str(dirty_tree)])
        (dirty_tree / "wip.txt").write_text("wip\n", encoding="utf-8")

        specs = [
            ("1", {"status": "merged", "cleanupStatus": "pending", "branch": "merged-child"}),
            ("2", {"status": "merged", "cleanupStatus": "pending", "branch": "unmerged-child"}),
            ("3", {"status": "merged", "cleanupStatus": "pending", "branch": "dirty-child"}),
            ("4", {"status": "merged", "cleanupStatus": "pending", "branch": "already-gone"}),
            ("5", {"status": "merged", "cleanupStatus": "pending", "branch": "feature"}),
            ("7", {"status": "ready", "cleanupStatus": "pending", "branch": "merged-child"}),
            ("8", {"status": "merged", "cleanupStatus": "done", "branch": "merged-child"}),
        ]
        for issue_id, meta in specs:
            write_issue(ledger, workspace.id, issue_id, {"title": f"Issue {issue_id}", **meta}, "body\n")

        # `write_issue` always defaults a branch, so a branchless issue can only
        # come from a hand-edited issue file.
        ledger.issue_path(workspace.id, "6").write_text(
            "---\nid: '6'\ntitle: Issue 6\nstatus: merged\ncleanupStatus: pending\n---\nbody\n",
            encoding="utf-8",
        )

        candidates = safety.issue_candidates([workspace.id])
        by_issue = {candidate["issue"]: candidate for candidate in candidates}
        if set(by_issue) != {"1", "2", "3", "4", "5", "6"}:
            raise AssertionError(f"Expected only pending, done-status issues to be candidates, got {sorted(by_issue)}")

        expected = {
            "1": ("safe-to-delete", "child branch is merged into task branch"),
            "2": ("needs-review", "child branch is not safely merged into task branch"),
            "3": ("needs-review", "child worktree has uncommitted changes"),
            "4": ("mark-done", "child branch and worktree are already gone"),
            "5": ("needs-review", "child branch equals workspace task branch"),
            "6": ("needs-review", "issue has no child branch"),
        }
        for issue_id, (action, reason) in expected.items():
            candidate = by_issue[issue_id]
            if candidate["recommendedAction"] != action or candidate["reason"] != reason:
                raise AssertionError(f"Expected issue {issue_id} to be {action}/{reason!r}, got {candidate}")

        result = safety.cleanup_item(f"{workspace.id}:1")
        if result["cleanupStatus"] != "done":
            raise AssertionError(f"Expected cleanup_item to mark the issue cleaned up, got {result}")
        if branch_exists(worktree, "merged-child"):
            raise AssertionError("Expected the merged child branch to be deleted")
        if merged_tree.exists():
            raise AssertionError("Expected the merged child worktree to be removed")

        try:
            safety.cleanup_item(f"{workspace.id}:2")
            raise AssertionError("Expected cleanup_item to refuse an unmerged child branch")
        except SystemExit:
            pass


def test_cleanup_issue_candidates_report_a_missing_repo_worktree():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        config = _cleanup_test_config(tmp_dir)
        safety = CleanupSafety(config, select_repo=select_repo)
        workspace = safety.ledger.ensure("CL-NOREPO", title="Missing repo", state="dev-complete")
        workspace.upsert_repo({"name": "app", "worktreePath": str(tmp_dir / "gone"), "branch": "feature"})
        safety.ledger.save(workspace)
        write_issue(
            safety.ledger,
            "CL-NOREPO",
            "1",
            {"title": "Issue 1", "status": "merged", "cleanupStatus": "pending", "branch": "child"},
            "body\n",
        )

        candidates = safety.issue_candidates(["CL-NOREPO"])
        if candidates[0]["reason"] != "repo worktree is missing":
            raise AssertionError(f"Expected a missing repo worktree reason, got {candidates}")

        try:
            safety.cleanup_item("CL-NOREPO:1")
            raise AssertionError("Expected cleanup_item to refuse a needs-review candidate")
        except SystemExit:
            pass


def test_cleanup_item_rejects_malformed_and_unknown_items():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        safety, workspace, _, _ = _workspace_with_worktree(tmp_dir, "CL-BADITEM")
        for item in ("no-colon", f"{workspace.id}:404"):
            try:
                safety.cleanup_item(item)
                raise AssertionError(f"Expected cleanup_item to reject {item!r}")
            except SystemExit:
                pass


def test_cleanup_issue_candidates_require_a_repo_selector():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        safety = CleanupSafety(_cleanup_test_config(Path(tmp_dir)))
        try:
            safety.issue_candidates(["CL-ANY"])
            raise AssertionError("Expected issue cleanup without a repo selector to raise SystemExit")
        except SystemExit:
            pass


def test_cleanup_plan_printers_render_rows_and_empty_state():
    empty_workspace_plan = _capture_stdout(lambda: CleanupSafety.print_workspace_plan([]))
    assert_contains(empty_workspace_plan, "No workspace cleanup candidates found.")
    empty_issue_plan = _capture_stdout(lambda: CleanupSafety.print_issue_plan([]))
    assert_contains(empty_issue_plan, "No cleanup candidates found.")

    workspace_plan = _capture_stdout(
        lambda: CleanupSafety.print_workspace_plan(
            [
                {
                    "workspace": "CL-PRINT",
                    "state": "dev-complete",
                    "recommendedAction": "safe-to-remove",
                    "existingRepoCount": 1,
                    "dirtyRepos": [],
                    "aheadOfBase": 0,
                    "openPrCount": 0,
                    "reason": "workspace is done",
                }
            ]
        )
    )
    assert_contains(workspace_plan, "WORKSPACE")
    assert_contains(workspace_plan, "CL-PRINT")
    assert_contains(workspace_plan, "safe-to-remove")

    issue_plan = _capture_stdout(
        lambda: CleanupSafety.print_issue_plan(
            [
                {
                    "item": "CL-PRINT:1",
                    "recommendedAction": "safe-to-delete",
                    "branch": "child",
                    "mergedIntoTaskBranch": True,
                    "worktreeDirty": False,
                    "reason": "child branch is merged into task branch",
                }
            ]
        )
    )
    assert_contains(issue_plan, "ITEM")
    assert_contains(issue_plan, "CL-PRINT:1")
    assert_contains(issue_plan, "safe-to-delete")


CLEANUP_TESTS = [
    test_cleanup_workspace_candidate_is_safe_to_remove_when_done_and_clean,
    test_cleanup_workspace_candidate_soft_reasons_are_force_eligible,
    test_cleanup_workspace_candidate_hard_reasons_need_review,
    test_cleanup_workspace_candidate_marks_done_when_worktrees_are_gone,
    test_cleanup_workspace_force_removes_dirty_worktrees_and_notes_reason,
    test_cleanup_plan_round_trip_and_validation,
    test_cleanup_load_plan_rejects_unusable_plans,
    test_cleanup_validate_plan_candidate_rejects_unapproved_action,
    test_cleanup_issue_candidates_classify_each_child_branch,
    test_cleanup_issue_candidates_report_a_missing_repo_worktree,
    test_cleanup_item_rejects_malformed_and_unknown_items,
    test_cleanup_issue_candidates_require_a_repo_selector,
    test_cleanup_plan_printers_render_rows_and_empty_state,
]


def main():
    passed = support.run_plain(CLEANUP_TESTS)
    print(f"{passed} self-check(s) passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"self-check failed: {error}", file=sys.stderr)
        sys.exit(1)
