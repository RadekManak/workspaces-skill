#!/usr/bin/env python3
"""Unit self-checks for `workspaces.git` remote parsing and worktree helpers."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import support
from tests.support import make_source_repo, run

from workspaces import git as git_model
from workspaces.common import DEFAULT_CONFIG


def _git_test_config():
    config = dict(DEFAULT_CONFIG)
    config["base_remote"] = "origin"
    config["base_branch"] = "main"
    config["push_remote"] = "fork"
    return config


def _add_worktree(source, path, branch):
    run(["git", "-C", str(source), "worktree", "add", "-b", branch, str(path)])
    return Path(path)


def test_git_repo_name_from_remote_handles_every_remote_form():
    cases = {
        "git@github.com:org/repo.git": "org/repo",
        "git@github.com:org/repo": "org/repo",
        "https://github.com/org/repo.git": "org/repo",
        "https://github.com/org/repo": "org/repo",
        "ssh://git@github.com/org/repo.git": "org/repo",
        "  https://github.com/org/repo.git  ": "org/repo",
        "/local/path/repo.git": "/local/path/repo",
        "repo": "repo",
    }
    for remote, expected in cases.items():
        actual = git_model.repo_name_from_remote(remote)
        if actual != expected:
            raise AssertionError(f"Expected {remote!r} to parse to {expected!r}, got {actual!r}")


def test_git_github_repo_from_remote_requires_owner_and_name():
    if git_model.github_repo_from_remote("git@github.com:org/repo.git") != "org/repo":
        raise AssertionError("Expected an owner/name remote to resolve to `org/repo`")
    for remote in ("repo", "repo.git", ""):
        if git_model.github_repo_from_remote(remote) is not None:
            raise AssertionError(f"Expected {remote!r} without an owner segment to resolve to None")


def test_git_path_is_under_only_matches_real_descendants():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        parent = tmp_dir / "root"
        child = parent / "nested" / "deep"
        sibling = tmp_dir / "other"
        child.mkdir(parents=True)
        sibling.mkdir()

        if not git_model.path_is_under(child, parent):
            raise AssertionError("Expected a nested path to be reported under its parent")
        if not git_model.path_is_under(parent, parent):
            raise AssertionError("Expected a path to be reported under itself")
        if git_model.path_is_under(sibling, parent):
            raise AssertionError("Expected a sibling path to not be reported under the parent")
        if git_model.path_is_under(parent, child):
            raise AssertionError("Expected a parent path to not be reported under its own child")


def test_git_root_and_git_info_outside_a_repo():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        plain = tmp_dir / "plain"
        plain.mkdir()

        if git_model.git_root(plain) is not None:
            raise AssertionError("Expected git_root to return None outside a git repo")
        try:
            git_model.git_info(plain, _git_test_config())
            raise AssertionError("Expected git_info to raise SystemExit outside a git repo")
        except SystemExit:
            pass


def test_git_info_reports_branch_remote_dirty_and_ahead():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = make_source_repo(tmp_dir, "app")
        config = _git_test_config()

        info = git_model.git_info(source, config)
        if info["name"] != "app" or info["branch"] != "main":
            raise AssertionError(f"Expected name `app` on branch `main`, got {info}")
        if info["remote"] is None:
            raise AssertionError(f"Expected the origin remote to be reported, got {info}")
        if info["forkRemote"] is not None:
            raise AssertionError(f"Expected a missing push remote to be reported as None, got {info}")
        if info["dirty"] or info["aheadOfBase"] != 0:
            raise AssertionError(f"Expected a clean repo with no commits ahead of origin/main, got {info}")

        (source / "README.md").write_text("changed\n", encoding="utf-8")
        run(["git", "-C", str(source), "add", "README.md"])
        run(["git", "-C", str(source), "commit", "-m", "second"])
        (source / "dirty.txt").write_text("wip\n", encoding="utf-8")

        info = git_model.git_info(source, config)
        if not info["dirty"]:
            raise AssertionError(f"Expected an untracked file to mark the repo dirty, got {info}")
        if info["aheadOfBase"] != 1:
            raise AssertionError(f"Expected aheadOfBase 1 after one commit, got {info}")


def test_git_info_reports_ahead_none_when_base_ref_is_unknown():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = make_source_repo(tmp_dir, "app")
        config = _git_test_config()
        config["base_branch"] = "does-not-exist"

        info = git_model.git_info(source, config)
        if info["aheadOfBase"] is not None:
            raise AssertionError(f"Expected aheadOfBase None for an unresolvable base ref, got {info}")


def test_git_success_reports_command_exit_status():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = make_source_repo(tmp_dir, "app")

        if not git_model.git_success(source, ["rev-parse", "--verify", "--quiet", "refs/heads/main"]):
            raise AssertionError("Expected git_success to be True for an existing ref")
        if git_model.git_success(source, ["rev-parse", "--verify", "--quiet", "refs/heads/nope"]):
            raise AssertionError("Expected git_success to be False for a missing ref")


def test_git_is_linked_worktree_distinguishes_main_checkout():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = make_source_repo(tmp_dir, "app")
        linked = _add_worktree(source, tmp_dir / "linked", "feature")

        if git_model.is_linked_worktree(source):
            raise AssertionError("Expected the main checkout to not be a linked worktree")
        if not git_model.is_linked_worktree(linked):
            raise AssertionError("Expected an added worktree to be a linked worktree")

        plain = tmp_dir / "plain"
        plain.mkdir()
        if git_model.is_linked_worktree(plain):
            raise AssertionError("Expected a non-repo path to not be a linked worktree")


def test_git_worktrees_and_worktree_for_branch():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = make_source_repo(tmp_dir, "app")
        linked = _add_worktree(source, tmp_dir / "linked", "feature")

        entries = git_model.git_worktrees(source)
        branches = {entry["branch"] for entry in entries}
        if branches != {"main", "feature"}:
            raise AssertionError(f"Expected both worktrees to be listed, got {entries}")

        found = git_model.worktree_for_branch(source, "feature")
        if found is None or Path(found).resolve() != linked.resolve():
            raise AssertionError(f"Expected the `feature` worktree path, got {found}")
        if git_model.worktree_for_branch(source, "missing") is not None:
            raise AssertionError("Expected None for a branch with no worktree")


def test_git_branch_exists_and_branch_merged():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = make_source_repo(tmp_dir, "app")

        run(["git", "-C", str(source), "branch", "merged-child"])
        run(["git", "-C", str(source), "checkout", "-b", "unmerged-child"])
        (source / "child.txt").write_text("child\n", encoding="utf-8")
        run(["git", "-C", str(source), "add", "child.txt"])
        run(["git", "-C", str(source), "commit", "-m", "child"])
        run(["git", "-C", str(source), "checkout", "main"])

        if not git_model.branch_exists(source, "merged-child"):
            raise AssertionError("Expected branch_exists to be True for an existing branch")
        if git_model.branch_exists(source, "nope"):
            raise AssertionError("Expected branch_exists to be False for a missing branch")

        if git_model.branch_merged(source, "nope", "main") is not None:
            raise AssertionError("Expected branch_merged to be None when the branch is gone")
        if git_model.branch_merged(source, "merged-child", None) is not False:
            raise AssertionError("Expected branch_merged to be False without a target branch")
        if git_model.branch_merged(source, "merged-child", "missing-target") is not False:
            raise AssertionError("Expected branch_merged to be False for a missing target branch")
        if git_model.branch_merged(source, "merged-child", "main") is not True:
            raise AssertionError("Expected an ancestor branch to be reported as merged")
        if git_model.branch_merged(source, "unmerged-child", "main") is not False:
            raise AssertionError("Expected a branch with extra commits to be reported as unmerged")


def test_git_worktree_dirty_tracks_uncommitted_changes():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = make_source_repo(tmp_dir, "app")

        if git_model.worktree_dirty(source):
            raise AssertionError("Expected a freshly committed repo to be clean")
        (source / "README.md").write_text("dirty\n", encoding="utf-8")
        if not git_model.worktree_dirty(source):
            raise AssertionError("Expected a modified tracked file to mark the worktree dirty")


def test_git_remove_linked_worktree_guardrails():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = make_source_repo(tmp_dir, "app")
        workspace_root = tmp_dir / "workspaces" / "W-1"
        workspace_root.mkdir(parents=True)

        # Missing paths are a no-op, not an error.
        git_model.remove_linked_worktree(None, workspace_root)
        git_model.remove_linked_worktree(str(tmp_dir / "gone"), workspace_root)

        try:
            git_model.remove_linked_worktree(str(source), workspace_root)
            raise AssertionError("Expected removing a non-linked checkout to raise SystemExit")
        except SystemExit:
            pass

        outside = _add_worktree(source, tmp_dir / "outside", "outside")
        try:
            git_model.remove_linked_worktree(str(outside), workspace_root)
            raise AssertionError("Expected removing a worktree outside the workspace root to raise SystemExit")
        except SystemExit:
            pass

        dirty = _add_worktree(source, workspace_root / "dirty", "dirty")
        (dirty / "wip.txt").write_text("wip\n", encoding="utf-8")
        try:
            git_model.remove_linked_worktree(str(dirty), workspace_root)
            raise AssertionError("Expected removing a dirty worktree to raise SystemExit")
        except SystemExit:
            pass
        git_model.remove_linked_worktree(str(dirty), workspace_root, force=True)
        if dirty.exists():
            raise AssertionError("Expected force=True to remove a dirty linked worktree")


def test_git_remove_linked_worktree_falls_back_when_source_is_gone():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = make_source_repo(tmp_dir, "app")
        workspace_root = tmp_dir / "workspaces" / "W-1"
        workspace_root.mkdir(parents=True)
        clean = _add_worktree(source, workspace_root / "clean", "clean")

        git_model.remove_linked_worktree(
            str(clean),
            workspace_root,
            source_path=str(tmp_dir / "missing-source"),
        )
        if clean.exists():
            raise AssertionError("Expected removal to fall back to running git inside the worktree itself")


GIT_TESTS = [
    test_git_repo_name_from_remote_handles_every_remote_form,
    test_git_github_repo_from_remote_requires_owner_and_name,
    test_git_path_is_under_only_matches_real_descendants,
    test_git_root_and_git_info_outside_a_repo,
    test_git_info_reports_branch_remote_dirty_and_ahead,
    test_git_info_reports_ahead_none_when_base_ref_is_unknown,
    test_git_success_reports_command_exit_status,
    test_git_is_linked_worktree_distinguishes_main_checkout,
    test_git_worktrees_and_worktree_for_branch,
    test_git_branch_exists_and_branch_merged,
    test_git_worktree_dirty_tracks_uncommitted_changes,
    test_git_remove_linked_worktree_guardrails,
    test_git_remove_linked_worktree_falls_back_when_source_is_gone,
]


def main():
    passed = support.run_plain(GIT_TESTS)
    print(f"{passed} self-check(s) passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"self-check failed: {error}", file=sys.stderr)
        sys.exit(1)
