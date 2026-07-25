#!/usr/bin/env python3
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import support
from tests.support import ROOT, assert_contains, make_source_repo, run

from workspaces import doctor
from workspaces import git
from workspaces import github_sync
from workspaces import issues as issue_model
from workspaces import repo
from workspaces import sandcastle_reconcile
from workspaces.common import DEFAULT_CONFIG, read_yaml
from workspaces.ledger import WorkspaceLedger
from workspaces.workspace_model import Workspace


def test_workspace_model_state_roundtrip():
    workspace = Workspace({"id": "w1", "state": "captured"})
    if workspace.state != "captured":
        raise AssertionError(f"Expected initial state 'captured', got {workspace.state!r}")
    workspace.set_state("in-progress")
    if workspace.state != "in-progress":
        raise AssertionError(f"Expected state 'in-progress' after set_state, got {workspace.state!r}")


def test_workspace_model_repo_upsert_dedups_by_worktree_path_or_name():
    workspace = Workspace({"id": "w1", "repos": []})
    workspace.upsert_repo({"name": "app", "worktreePath": "/tmp/app", "branch": "main"})
    workspace.upsert_repo({"name": "app", "worktreePath": "/tmp/app", "branch": "feature"})
    if len(workspace.repos) != 1:
        raise AssertionError(f"Expected dedup by name to keep one repo, got {workspace.repos}")
    if workspace.repos[0]["branch"] != "feature":
        raise AssertionError(f"Expected upsert to merge fields onto existing repo, got {workspace.repos[0]}")

    workspace.upsert_repo({"name": "renamed", "worktreePath": "/tmp/app"})
    if len(workspace.repos) != 1 or workspace.repos[0]["name"] != "renamed":
        raise AssertionError(f"Expected dedup by worktreePath to merge even with a new name, got {workspace.repos}")

    workspace.upsert_repo({"name": "other", "worktreePath": "/tmp/other"})
    if len(workspace.repos) != 2:
        raise AssertionError(f"Expected a genuinely new repo to be appended, got {workspace.repos}")


def test_workspace_model_find_repo_missing_raises():
    workspace = Workspace({"id": "w1", "repos": [{"name": "app"}]})
    if workspace.find_repo("app")["name"] != "app":
        raise AssertionError("Expected find_repo to return the matching repo")
    try:
        workspace.find_repo("missing")
        raise AssertionError("Expected find_repo to raise SystemExit for an unknown repo")
    except SystemExit:
        pass


def test_workspace_model_remove_and_replace_repo():
    workspace = Workspace(
        {"id": "w1", "repos": [{"name": "a", "branch": "main"}, {"name": "b", "branch": "main"}]}
    )
    removed = workspace.remove_repo("a")
    if removed["name"] != "a" or [repo["name"] for repo in workspace.repos] != ["b"]:
        raise AssertionError(f"Expected remove_repo to drop only `a`, got {workspace.repos}")

    workspace.replace_repo("b", {"name": "b", "branch": "renamed"})
    if workspace.repos[0]["branch"] != "renamed":
        raise AssertionError(f"Expected replace_repo to update the entry in place, got {workspace.repos}")

    workspace.replace_all_repos([{"name": "c"}, {"name": "d"}])
    if [repo["name"] for repo in workspace.repos] != ["c", "d"]:
        raise AssertionError(f"Expected replace_all_repos to replace the whole list, got {workspace.repos}")


def test_workspace_model_github_pr_upsert_vs_append():
    workspace = Workspace({"id": "w1"})
    workspace.record_github_pr({"repo": "app", "branch": "feature", "number": 1, "state": "OPEN"})
    workspace.record_github_pr({"repo": "app", "branch": "feature", "number": 1, "state": "MERGED"})
    if len(workspace.github_prs) != 1 or workspace.github_prs[0]["state"] != "MERGED":
        raise AssertionError(f"Expected matching (repo, branch) to update in place, got {workspace.github_prs}")

    # No repo/branch on the item (e.g. a manual `workspace pr` event): never matches, always appends.
    workspace.record_github_pr({"number": 2, "state": "OPEN"})
    if len(workspace.github_prs) != 2:
        raise AssertionError(f"Expected an unmatched PR item to be appended, got {workspace.github_prs}")


def test_workspace_model_sandcastle_plan_pointer_lifecycle():
    workspace = Workspace({"id": "w1"})
    if workspace.current_sandcastle_plan is not None:
        raise AssertionError("Expected no plan pointer on a fresh workspace")
    if workspace.clear_current_sandcastle_plan():
        raise AssertionError("Expected clearing an absent pointer to report no change")

    workspace.set_current_sandcastle_plan({"path": "/tmp/plan.json", "targetedIssueIds": ["a", "b"]})
    if workspace.targeted_issue_ids() != {"a", "b"}:
        raise AssertionError(f"Expected targeted_issue_ids from the pointer, got {workspace.targeted_issue_ids()}")

    changed = workspace.clear_current_sandcastle_plan()
    if not changed or workspace.current_sandcastle_plan is not None:
        raise AssertionError("Expected clear_current_sandcastle_plan to drop the pointer")
    if "sandcastle" in workspace.to_dict():
        raise AssertionError("Expected the empty sandcastle block to be dropped entirely, not left as {}")


def test_workspace_model_to_dict_is_a_read_only_deep_copy():
    workspace = Workspace({"id": "w1", "repos": [{"name": "app"}]})
    snapshot = workspace.to_dict()
    snapshot["repos"][0]["name"] = "mutated"
    snapshot["id"] = "mutated"
    if workspace.repos[0]["name"] != "app" or workspace.id != "w1":
        raise AssertionError("Expected mutating a to_dict() snapshot to never affect the real Workspace")


def test_workspace_model_mark_cleanup_done():
    workspace = Workspace({"id": "w1", "state": "dev-complete"})
    workspace.mark_cleanup_done(action="safe-to-remove", at="2026-01-01T00:00:00+00:00")
    if workspace.state != "closed" or workspace.cleanup_status != "done":
        raise AssertionError(f"Expected mark_cleanup_done to close and mark done, got {workspace.to_dict()}")


def _doctor_test_config(tmp_dir):
    config = dict(DEFAULT_CONFIG)
    config["source_root"] = str(tmp_dir / "src")
    config["workspace_root"] = str(tmp_dir / "workspaces")
    config["ledger_root"] = str(tmp_dir / "ledger")
    return config


def test_doctor_check_workspace_fixes_missing_dirs_and_files():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        config = _doctor_test_config(tmp_dir)
        ledger = WorkspaceLedger(config)
        workspace = ledger.ensure("DOC-UNIT", title="Doctor unit test")

        shutil.rmtree(ledger.runs_dir("DOC-UNIT"))
        (ledger.ledger_dir("DOC-UNIT") / "spec.md").unlink()

        issues = doctor.check_workspace(config, workspace, fix=False)
        codes = {issue["code"] for issue in issues}
        if "missing-dir" not in codes or "missing-file" not in codes:
            raise AssertionError(f"Expected missing-dir and missing-file issues, got {issues}")
        if any(issue.get("fixed") for issue in issues):
            raise AssertionError(f"Expected fix=False to leave issues unfixed, got {issues}")
        if ledger.runs_dir("DOC-UNIT").exists():
            raise AssertionError("Expected fix=False to not create the missing directory")

        fixed_issues = doctor.check_workspace(config, workspace, fix=True)
        fixed_codes = {issue["code"] for issue in fixed_issues if issue.get("fixed")}
        if "missing-dir" not in fixed_codes or "missing-file" not in fixed_codes:
            raise AssertionError(f"Expected fix=True to repair missing dir/file issues, got {fixed_issues}")
        if not ledger.runs_dir("DOC-UNIT").exists():
            raise AssertionError("Expected fix=True to recreate the missing runs directory")
        if not (ledger.ledger_dir("DOC-UNIT") / "spec.md").exists():
            raise AssertionError("Expected fix=True to recreate the missing spec.md")

        clean_issues = doctor.check_workspace(config, workspace, fix=False)
        if any(issue["code"] in ("missing-dir", "missing-file") for issue in clean_issues):
            raise AssertionError(f"Expected no missing-dir/file issues after fixing, got {clean_issues}")


def test_doctor_check_workspace_detects_repo_branch_drift():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        repo_path = tmp_dir / "repo"
        repo_path.mkdir(parents=True)
        run(["git", "-C", str(repo_path), "init", "-b", "main"])
        run(["git", "-C", str(repo_path), "config", "user.email", "test@example.com"])
        run(["git", "-C", str(repo_path), "config", "user.name", "Test User"])
        (repo_path / "README.md").write_text("hello\n", encoding="utf-8")
        run(["git", "-C", str(repo_path), "add", "README.md"])
        run(["git", "-C", str(repo_path), "commit", "-m", "init"])
        run(["git", "-C", str(repo_path), "checkout", "-b", "feature"])

        config = _doctor_test_config(tmp_dir)
        ledger = WorkspaceLedger(config)
        workspace = ledger.ensure("DOC-DRIFT", title="Doctor drift test")
        workspace.upsert_repo({"name": "repo", "worktreePath": str(repo_path), "branch": "main"})
        ledger.save(workspace)

        issues = doctor.check_workspace(config, workspace, fix=False)
        drift_issues = [issue for issue in issues if issue["code"] == "repo-branch-drift"]
        if not drift_issues:
            raise AssertionError(f"Expected repo-branch-drift issue, got {issues}")
        if drift_issues[0].get("fixable"):
            raise AssertionError(f"Expected repo-branch-drift to be reported as not fixable, got {drift_issues[0]}")


def test_doctor_check_workspace_saves_unconditionally_on_fix():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        config = _doctor_test_config(tmp_dir)
        ledger = WorkspaceLedger(config)
        workspace = ledger.ensure("DOC-SAVE", title="Doctor save test")

        # Fix everything ensure() itself doesn't set up (e.g. the workspace pointer)
        # so the next pass is genuinely clean.
        doctor.check_workspace(config, workspace, fix=True)
        clean_issues = doctor.check_workspace(config, workspace, fix=False)
        if clean_issues:
            raise AssertionError(f"Expected a fixed-up workspace to be clean, got {clean_issues}")

        yaml_path = ledger.workspace_yaml_path("DOC-SAVE")
        before = yaml_path.read_text(encoding="utf-8")
        time.sleep(1.1)  # updatedAt has second resolution
        no_op_issues = doctor.check_workspace(config, workspace, fix=True)
        if no_op_issues:
            raise AssertionError(f"Expected no issues on a clean workspace, got {no_op_issues}")
        after = yaml_path.read_text(encoding="utf-8")
        if before == after:
            raise AssertionError(
                "Expected fix=True to unconditionally persist the workspace via "
                "WorkspaceLedger.save even when no issues were found"
            )


def test_repo_select_repo_single_and_multi_and_named():
    empty_workspace = Workspace({"id": "w1", "repos": []})
    try:
        repo.select_repo(empty_workspace)
        raise AssertionError("Expected select_repo to raise SystemExit for a workspace with no repos")
    except SystemExit:
        pass

    single_workspace = Workspace({"id": "w2", "repos": [{"name": "solo"}]})
    if repo.select_repo(single_workspace) != {"name": "solo"}:
        raise AssertionError("Expected select_repo to default to the only repo")

    multi_workspace = Workspace(
        {"id": "w3", "repos": [{"name": "alpha"}, {"name": "beta"}]}
    )
    try:
        repo.select_repo(multi_workspace)
        raise AssertionError("Expected select_repo to raise SystemExit when multiple repos and no --repo")
    except SystemExit:
        pass
    if repo.select_repo(multi_workspace, repo_name="beta") != {"name": "beta"}:
        raise AssertionError("Expected select_repo to return the named repo")


def test_repo_add_source_repo_creates_worktree_and_upserts():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        make_source_repo(tmp_dir, "app")
        config = _doctor_test_config(tmp_dir)
        ledger = WorkspaceLedger(config)
        workspace = ledger.ensure("REPO-ADD", title="Repo add test")

        dest = repo.add_source_repo_to_workspace(config, workspace, "app")

        if not dest.exists():
            raise AssertionError(f"Expected worktree to be created at {dest}")
        source = tmp_dir / "src" / "app"
        worktrees = run(["git", "-C", str(source), "worktree", "list", "--porcelain"]).stdout
        if str(dest) not in worktrees:
            raise AssertionError(f"Expected `git worktree add` to register {dest}, got:\n{worktrees}")

        repos = workspace.repos
        if len(repos) != 1 or repos[0]["name"] != "app":
            raise AssertionError(f"Expected upserted repo named 'app', got {repos}")
        if repos[0]["branch"] != "REPO-ADD":
            raise AssertionError(f"Expected default branch to be the workspace id, got {repos[0]}")
        if repos[0]["worktreePath"] != str(dest) or repos[0]["sourcePath"] != str(source):
            raise AssertionError(f"Expected worktree/source paths recorded, got {repos[0]}")


def test_repo_add_fetches_base_before_worktree():
    """Stale origin/main must be refreshed before the new worktree is created."""
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = make_source_repo(tmp_dir, "app")
        stale = run(["git", "-C", str(source), "rev-parse", "refs/remotes/origin/main"]).stdout.strip()
        (source / "README.md").write_text("hello\nupdated\n", encoding="utf-8")
        run(["git", "-C", str(source), "add", "README.md"])
        run(["git", "-C", str(source), "commit", "-m", "advance main"])
        fresh = run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
        if fresh == stale:
            raise AssertionError("Expected a new commit on main before testing fetch")
        # Leave origin/main pointing at the old tip; add_source_repo must fetch first.
        current_origin = run(
            ["git", "-C", str(source), "rev-parse", "refs/remotes/origin/main"]
        ).stdout.strip()
        if current_origin != stale:
            raise AssertionError(f"Expected origin/main to stay stale at {stale}, got {current_origin}")

        config = _doctor_test_config(tmp_dir)
        ledger = WorkspaceLedger(config)
        workspace = ledger.ensure("REPO-FETCH", title="Repo fetch test")
        dest = repo.add_source_repo_to_workspace(config, workspace, "app")

        worktree_head = run(["git", "-C", str(dest), "rev-parse", "HEAD"]).stdout.strip()
        if worktree_head != fresh:
            raise AssertionError(
                f"Expected worktree based on fetched tip {fresh}, got {worktree_head} (stale was {stale})"
            )
        origin_after = run(
            ["git", "-C", str(source), "rev-parse", "refs/remotes/origin/main"]
        ).stdout.strip()
        if origin_after != fresh:
            raise AssertionError(f"Expected fetch to update origin/main to {fresh}, got {origin_after}")


def test_repo_refresh_repo_updates_git_metadata():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        repo_path = tmp_dir / "repo"
        repo_path.mkdir(parents=True)
        run(["git", "-C", str(repo_path), "init", "-b", "main"])
        run(["git", "-C", str(repo_path), "config", "user.email", "test@example.com"])
        run(["git", "-C", str(repo_path), "config", "user.name", "Test User"])
        (repo_path / "README.md").write_text("hello\n", encoding="utf-8")
        run(["git", "-C", str(repo_path), "add", "README.md"])
        run(["git", "-C", str(repo_path), "commit", "-m", "init"])
        run(["git", "-C", str(repo_path), "checkout", "-b", "feature"])

        config = _doctor_test_config(tmp_dir)
        repo_dict = {
            "name": "repo",
            "worktreePath": str(repo_path),
            "branch": "main",
            "baseRemote": config["base_remote"],
            "baseBranch": config["base_branch"],
            "pushRemote": config["push_remote"],
        }

        refreshed = repo.refresh_repo(config, repo_dict)

        if refreshed["branch"] != "feature":
            raise AssertionError(f"Expected refreshed branch to be 'feature', got {refreshed}")
        if not refreshed.get("worktreePath"):
            raise AssertionError(f"Expected refreshed repo to keep a worktreePath, got {refreshed}")
        if refreshed["baseRemote"] != config["base_remote"] or refreshed["pushRemote"] != config["push_remote"]:
            raise AssertionError(f"Expected refresh_repo to preserve base/push remote config, got {refreshed}")


def test_github_sync_apply_pr_event_maps_state_and_records_pr():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        config = _doctor_test_config(tmp_dir)
        ledger = WorkspaceLedger(config)

        workspace = ledger.ensure("GH-OPEN", title="GitHub sync opened test")
        github_sync.apply_pr_event(workspace, "opened", url="https://github.com/x/y/pull/1", number=1)
        if workspace.state != "pr-review":
            raise AssertionError(f"Expected 'opened' event to set state to 'pr-review', got {workspace.state}")
        prs = workspace.github_prs
        if len(prs) != 1 or prs[0].get("number") != 1 or prs[0].get("merged"):
            raise AssertionError(f"Expected a single unmerged PR#1 recorded, got {prs}")

        workspace = ledger.ensure("GH-MERGED", title="GitHub sync merged test")
        github_sync.apply_pr_event(workspace, "merged", url="https://github.com/x/y/pull/2", number=2)
        if workspace.state != "dev-complete":
            raise AssertionError(f"Expected 'merged' event to set state to 'dev-complete', got {workspace.state}")
        prs = workspace.github_prs
        if len(prs) != 1 or prs[0].get("number") != 2 or not prs[0].get("merged"):
            raise AssertionError(f"Expected a single merged PR#2 recorded, got {prs}")

        workspace = ledger.ensure("GH-FEEDBACK", title="GitHub sync feedback test")
        github_sync.apply_pr_event(workspace, "feedback")
        if workspace.state != "review-feedback":
            raise AssertionError(f"Expected 'feedback' event to set state to 'review-feedback', got {workspace.state}")
        if workspace.github_prs:
            raise AssertionError(f"Expected no PR entry when url/number omitted, got {workspace.github_prs}")


def test_github_sync_sync_prs_raises_when_gh_missing():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        config = _doctor_test_config(tmp_dir)
        ledger = WorkspaceLedger(config)
        workspace = ledger.ensure("GH-NO-CLI", title="GitHub sync missing gh test")

        original_which = github_sync.shutil.which
        github_sync.shutil.which = lambda *args, **kwargs: None
        try:
            try:
                github_sync.sync_prs(config, workspace)
                raise AssertionError("Expected sync_prs to raise SystemExit when gh is missing from PATH")
            except SystemExit:
                pass
        finally:
            github_sync.shutil.which = original_which


def test_github_sync_refresh_recorded_prs_updates_merged_from_gh():
    workspace = Workspace(
        {
            "id": "GH-REFRESH",
            "links": {
                "githubPrs": [
                    {
                        "url": "https://github.com/openshift/machine-api-provider-ibmcloud/pull/97",
                        "number": 97,
                        "repo": "machine-api-provider-ibmcloud",
                        "state": "OPEN",
                        "merged": False,
                    }
                ]
            },
        }
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] != ["gh", "pr"] or "view" not in cmd:
            raise AssertionError(f"Unexpected command: {cmd}")
        return json.dumps(
            {
                "number": 97,
                "url": "https://github.com/openshift/machine-api-provider-ibmcloud/pull/97",
                "state": "MERGED",
                "mergedAt": "2026-07-21T14:11:20Z",
                "title": "boot volume",
                "headRefName": "sdp-boot-volume",
            }
        )

    original_run = github_sync.run
    original_which = github_sync.shutil.which
    github_sync.run = fake_run
    github_sync.shutil.which = lambda *args, **kwargs: "/usr/bin/gh"
    try:
        changed = github_sync.refresh_recorded_prs(workspace)
        if not changed:
            raise AssertionError("Expected refresh_recorded_prs to report a change")
        pr = workspace.github_prs[0]
        if pr.get("state") != "MERGED" or not pr.get("merged"):
            raise AssertionError(f"Expected PR snapshot refreshed to MERGED, got {pr}")
        if pr.get("branch") != "sdp-boot-volume":
            raise AssertionError(f"Expected headRefName recorded as branch, got {pr}")
    finally:
        github_sync.run = original_run
        github_sync.shutil.which = original_which


def test_github_sync_refresh_recorded_prs_soft_fails_when_gh_missing():
    workspace = Workspace(
        {
            "id": "GH-REFRESH-NO-CLI",
            "links": {
                "githubPrs": [
                    {
                        "url": "https://github.com/openshift/foo/pull/1",
                        "number": 1,
                        "state": "OPEN",
                        "merged": False,
                    }
                ]
            },
        }
    )
    original_which = github_sync.shutil.which
    github_sync.shutil.which = lambda *args, **kwargs: None
    try:
        changed = github_sync.refresh_recorded_prs(workspace)
        if changed:
            raise AssertionError("Expected no change when gh is missing")
        if workspace.github_prs[0].get("state") != "OPEN":
            raise AssertionError("Expected stale snapshot to remain when gh is missing")
    finally:
        github_sync.shutil.which = original_which


def test_sandcastle_reconcile_apply_result_updates_issue_status_and_notes():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        config = _doctor_test_config(tmp_dir)
        ledger = WorkspaceLedger(config)
        ledger.ensure("SC-RECONCILE", title="Sandcastle reconcile test")
        issue_model.write_issue(
            ledger,
            "SC-RECONCILE",
            "1",
            {"title": "Issue one"},
            "## What to build\n\nTBD.\n",
            sandcastle=True,
        )

        result_path = tmp_dir / "result.json"
        result_path.write_text(
            json.dumps({"issues": [{"id": "1", "status": "merged", "reviewStatus": "approved"}]}),
            encoding="utf-8",
        )

        result = sandcastle_reconcile.apply_sandcastle_result(config, "SC-RECONCILE", str(result_path))
        if len(result.applied) != 1 or result.failures:
            raise AssertionError(f"Expected exactly one applied update and no failures, got {result}")

        issue = issue_model.read_issue(ledger.issue_path("SC-RECONCILE", "1"))
        if issue["meta"]["status"] != "merged":
            raise AssertionError(f"Expected issue status 'merged', got {issue['meta']}")

        notes = (tmp_dir / "ledger" / "workspaces" / "SC-RECONCILE" / "notes.md").read_text(encoding="utf-8")
        assert_contains(notes, "Reconciled Sandcastle result")
        assert_contains(notes, "status changed from")


def test_sandcastle_reconcile_apply_result_isolates_bad_entries():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        config = _doctor_test_config(tmp_dir)
        ledger = WorkspaceLedger(config)
        ledger.ensure("SC-ISOLATE", title="Sandcastle isolate test")
        issue_model.write_issue(
            ledger,
            "SC-ISOLATE",
            "1",
            {"title": "Issue one"},
            "## What to build\n\nTBD.\n",
            sandcastle=True,
        )

        result_path = tmp_dir / "result.json"
        result_path.write_text(
            json.dumps(
                {
                    "issues": [
                        {"status": "merged"},  # missing `id`
                        {"id": "1", "status": "merged"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = sandcastle_reconcile.apply_sandcastle_result(config, "SC-ISOLATE", str(result_path))
        if len(result.applied) != 1:
            raise AssertionError(f"Expected the valid entry to still be applied, got {result.applied}")
        if len(result.failures) != 1:
            raise AssertionError(f"Expected the bad entry to be reported as a failure, got {result.failures}")

        issue = issue_model.read_issue(ledger.issue_path("SC-ISOLATE", "1"))
        if issue["meta"]["status"] != "merged":
            raise AssertionError(f"Expected the valid entry's issue to be updated, got {issue['meta']}")


def test_sandcastle_reconcile_apply_result_raises_when_all_entries_fail():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        config = _doctor_test_config(tmp_dir)
        ledger = WorkspaceLedger(config)
        ledger.ensure("SC-ALLFAIL", title="Sandcastle all-fail test")

        result_path = tmp_dir / "result.json"
        result_path.write_text(
            json.dumps({"issues": [{"status": "merged"}, {"id": "missing"}]}),
            encoding="utf-8",
        )

        try:
            sandcastle_reconcile.apply_sandcastle_result(config, "SC-ALLFAIL", str(result_path))
            raise AssertionError("Expected apply_sandcastle_result to raise when every entry fails")
        except SystemExit:
            pass


def test_sandcastle_reconcile_init_runner_writes_template_files():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        repo_path = tmp_dir / "repo"
        repo_path.mkdir(parents=True)
        repo_dict = {"worktreePath": str(repo_path)}

        source = ROOT / "templates" / "sandcastle"
        expected_names = {item.name for item in source.iterdir() if item.is_file()}

        written = sandcastle_reconcile.init_sandcastle_runner(repo_dict)
        written_names = {path.name for path in written}
        if written_names != expected_names:
            raise AssertionError(f"Expected written files {expected_names}, got {written_names}")
        for name in expected_names:
            if not (repo_path / ".sandcastle" / name).exists():
                raise AssertionError(f"Expected {name} to be written under .sandcastle")

        try:
            sandcastle_reconcile.init_sandcastle_runner(repo_dict)
            raise AssertionError("Expected init_sandcastle_runner to refuse to overwrite without --force")
        except SystemExit:
            pass

        written_again = sandcastle_reconcile.init_sandcastle_runner(repo_dict, force=True)
        if {path.name for path in written_again} != expected_names:
            raise AssertionError(f"Expected force=True to rewrite all template files, got {written_again}")


def test_github_sync_sync_prs_reports_failed_gh_query():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        workspace = Workspace(
            {
                "id": "GH-FAIL",
                "repos": [{"name": "app", "worktreePath": str(tmp_dir)}],
            }
        )

        def failing_run(cmd, **kwargs):
            raise SystemExit("Command failed (exit 4): gh pr list\ngh: not authenticated")

        original_run = github_sync.run
        original_which = github_sync.shutil.which
        original_git_info = github_sync.git_info
        github_sync.run = failing_run
        github_sync.shutil.which = lambda *args, **kwargs: "/usr/bin/gh"
        github_sync.git_info = lambda *args, **kwargs: {
            "branch": "feature",
            "remote": "https://github.com/org/app.git",
        }
        try:
            seen, failures = github_sync.sync_prs(DEFAULT_CONFIG, workspace)
        finally:
            github_sync.run = original_run
            github_sync.shutil.which = original_which
            github_sync.git_info = original_git_info

        if seen:
            raise AssertionError(f"Expected no PR snapshots from a failing gh query, got {seen}")
        if len(failures) != 1 or failures[0]["repo"] != "app":
            raise AssertionError(f"Expected the failed repo to be reported, got {failures}")
        assert_contains(failures[0]["error"], "not authenticated")


def test_issue_read_issue_rejects_unreadable_frontmatter():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        for name, text in {
            "no-frontmatter.md": "## What to build\n\nTBD.\n",
            "unclosed.md": "---\nid: 1\nstatus: merged\n",
            "not-a-mapping.md": "---\n- 1\n- 2\n---\nbody\n",
            "invalid-yaml.md": "---\nid: [1\n---\nbody\n",
        }.items():
            path = tmp_dir / name
            path.write_text(text, encoding="utf-8")
            try:
                issue_model.read_issue(path)
                raise AssertionError(f"Expected read_issue to reject {name}")
            except SystemExit as error:
                assert_contains(str(error), name)


def test_read_yaml_rejects_invalid_and_non_mapping_documents():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        invalid = tmp_dir / "invalid.yaml"
        invalid.write_text("id: [1\n", encoding="utf-8")
        try:
            read_yaml(invalid)
            raise AssertionError("Expected read_yaml to reject invalid YAML")
        except SystemExit as error:
            assert_contains(str(error), str(invalid))

        sequence = tmp_dir / "sequence.yaml"
        sequence.write_text("- one\n- two\n", encoding="utf-8")
        try:
            read_yaml(sequence)
            raise AssertionError("Expected read_yaml to reject a non-mapping document")
        except SystemExit as error:
            assert_contains(str(error), str(sequence))


def test_worktree_dirty_propagates_git_failure():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        try:
            git.worktree_dirty(Path(tmp_dir))
            raise AssertionError("Expected worktree_dirty to raise when git status fails")
        except SystemExit as error:
            assert_contains(str(error), "Command failed")


WORKSPACE_MODEL_TESTS = [
    test_workspace_model_state_roundtrip,
    test_workspace_model_repo_upsert_dedups_by_worktree_path_or_name,
    test_workspace_model_find_repo_missing_raises,
    test_workspace_model_remove_and_replace_repo,
    test_workspace_model_github_pr_upsert_vs_append,
    test_workspace_model_sandcastle_plan_pointer_lifecycle,
    test_workspace_model_to_dict_is_a_read_only_deep_copy,
    test_workspace_model_mark_cleanup_done,
    test_doctor_check_workspace_fixes_missing_dirs_and_files,
    test_doctor_check_workspace_detects_repo_branch_drift,
    test_doctor_check_workspace_saves_unconditionally_on_fix,
    test_repo_select_repo_single_and_multi_and_named,
    test_repo_add_source_repo_creates_worktree_and_upserts,
    test_repo_add_fetches_base_before_worktree,
    test_repo_refresh_repo_updates_git_metadata,
    test_github_sync_apply_pr_event_maps_state_and_records_pr,
    test_github_sync_sync_prs_raises_when_gh_missing,
    test_github_sync_refresh_recorded_prs_updates_merged_from_gh,
    test_github_sync_refresh_recorded_prs_soft_fails_when_gh_missing,
    test_sandcastle_reconcile_apply_result_updates_issue_status_and_notes,
    test_sandcastle_reconcile_apply_result_isolates_bad_entries,
    test_sandcastle_reconcile_apply_result_raises_when_all_entries_fail,
    test_sandcastle_reconcile_init_runner_writes_template_files,
    test_github_sync_sync_prs_reports_failed_gh_query,
    test_issue_read_issue_rejects_unreadable_frontmatter,
    test_read_yaml_rejects_invalid_and_non_mapping_documents,
    test_worktree_dirty_propagates_git_failure,
]


def main():
    passed = support.run_plain(WORKSPACE_MODEL_TESTS)
    print(f"{passed} self-check(s) passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"self-check failed: {error}", file=sys.stderr)
        sys.exit(1)
