#!/usr/bin/env python3
"""Unit self-checks for `workspaces.github_sync` snapshot gathering."""
import contextlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import support
from tests.support import assert_contains, make_source_repo, run

from workspaces import github_sync
from workspaces.common import DEFAULT_CONFIG
from workspaces.workspace_model import Workspace


@contextlib.contextmanager
def fake_gh(responses, *, available=True, calls=None):
    """Patch `gh` discovery and command execution inside `github_sync`.

    `responses` maps a marker found in the argv (e.g. a branch or PR number) to
    the raw stdout `gh` should return, or to an exception it should raise.
    """

    def fake_run(cmd, *args, **kwargs):
        if calls is not None:
            calls.append(cmd)
        for marker, raw in responses.items():
            if marker in cmd:
                if isinstance(raw, BaseException):
                    raise raw
                return raw
        return ""

    original_run = github_sync.run
    original_which = github_sync.shutil.which
    github_sync.run = fake_run
    github_sync.shutil.which = lambda *args, **kwargs: "/usr/bin/gh" if available else None
    try:
        yield
    finally:
        github_sync.run = original_run
        github_sync.shutil.which = original_which


def _sync_test_config():
    config = dict(DEFAULT_CONFIG)
    config["base_remote"] = "origin"
    config["base_branch"] = "main"
    config["push_remote"] = "fork"
    return config


def _github_source_repo(tmp_dir, name, branch):
    source = make_source_repo(tmp_dir, name)
    # A non-github.com host keeps `git remote get-url` free of any local
    # `url.<base>.insteadOf` rewriting while still parsing to `org/<name>`.
    run(["git", "-C", str(source), "remote", "set-url", "origin", f"https://github.example.com/org/{name}.git"])
    run(["git", "-C", str(source), "checkout", "-b", branch])
    return source


def test_github_sync_sync_prs_records_snapshots_per_repo():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = _github_source_repo(tmp_dir, "app", "feature")
        workspace = Workspace({"id": "GH-SYNC", "repos": [{"name": "app", "worktreePath": str(source)}]})

        payload = json.dumps(
            [
                {
                    "number": 7,
                    "url": "https://github.com/org/app/pull/7",
                    "state": "OPEN",
                    "headRefName": "feature",
                    "mergedAt": None,
                    "title": "Add export",
                }
            ]
        )
        calls = []
        with fake_gh({"feature": payload}, calls=calls):
            seen, failures = github_sync.sync_prs(_sync_test_config(), workspace)

        if not calls or calls[0][:5] != ["gh", "pr", "list", "--repo", "org/app"]:
            raise AssertionError(f"Expected `gh pr list --repo org/app`, got {calls}")
        if failures:
            raise AssertionError(f"Expected no per-repo failures, got {failures}")
        if len(seen) != 1:
            raise AssertionError(f"Expected exactly one seen PR, got {seen}")
        item = seen[0]
        if item["repo"] != "app" or item["number"] != 7 or item["branch"] != "feature":
            raise AssertionError(f"Expected the PR snapshot to carry repo/number/branch, got {item}")
        if item["state"] != "OPEN" or item["merged"]:
            raise AssertionError(f"Expected an open, unmerged snapshot, got {item}")
        if not item["lastSeenAt"]:
            raise AssertionError(f"Expected lastSeenAt to be stamped, got {item}")
        if workspace.github_prs != seen:
            raise AssertionError(f"Expected seen PRs to be recorded on the workspace, got {workspace.github_prs}")


def test_github_sync_sync_prs_marks_merged_from_merged_at():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = _github_source_repo(tmp_dir, "app", "feature")
        workspace = Workspace({"id": "GH-SYNC-MERGED", "repos": [{"name": "app", "worktreePath": str(source)}]})

        payload = json.dumps(
            [
                {
                    "number": 8,
                    "url": "https://github.com/org/app/pull/8",
                    "state": "MERGED",
                    "headRefName": None,
                    "mergedAt": "2026-01-01T00:00:00Z",
                    "title": "Merged work",
                }
            ]
        )
        with fake_gh({"feature": payload}):
            seen, _ = github_sync.sync_prs(_sync_test_config(), workspace)

        if not seen[0]["merged"]:
            raise AssertionError(f"Expected mergedAt to mark the snapshot merged, got {seen}")
        if seen[0]["branch"] != "feature":
            raise AssertionError(f"Expected a missing headRefName to fall back to the local branch, got {seen}")


def test_github_sync_sync_prs_skips_repos_it_cannot_query():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        no_remote = make_source_repo(tmp_dir, "no-remote")
        run(["git", "-C", str(no_remote), "remote", "set-url", "origin", "mirror.git"])
        detached = _github_source_repo(tmp_dir, "detached", "feature")
        run(["git", "-C", str(detached), "checkout", "--detach", "HEAD"])

        workspace = Workspace(
            {
                "id": "GH-SKIP",
                "repos": [
                    {"name": "ledger-only"},
                    {"name": "missing", "worktreePath": str(tmp_dir / "gone")},
                    {"name": "no-remote", "worktreePath": str(no_remote)},
                    {"name": "detached", "worktreePath": str(detached)},
                ],
            }
        )

        calls = []
        with fake_gh({}, calls=calls):
            seen, failures = github_sync.sync_prs(_sync_test_config(), workspace)

        if failures:
            raise AssertionError(f"Expected no failures when no repo is queryable, got {failures}")
        if seen or workspace.github_prs:
            raise AssertionError(f"Expected no PR snapshots for unqueryable repos, got {seen}")
        if calls:
            raise AssertionError(f"Expected `gh` to never be invoked for unqueryable repos, got {calls}")


def test_github_sync_sync_prs_reports_unparsable_gh_output_as_a_failure():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        source = _github_source_repo(tmp_dir, "app", "feature")
        workspace = Workspace({"id": "GH-BADJSON", "repos": [{"name": "app", "worktreePath": str(source)}]})

        with fake_gh({"feature": "not json"}):
            seen, failures = github_sync.sync_prs(_sync_test_config(), workspace)

        if seen or workspace.github_prs:
            raise AssertionError(f"Expected unparsable `gh` output to yield no snapshots, got {seen}")
        if [failure["repo"] for failure in failures] != ["app"]:
            raise AssertionError(f"Expected the repo's failure to be reported, not swallowed, got {failures}")
        assert_contains(failures[0]["error"], "as JSON")


def test_github_sync_sync_prs_keeps_syncing_after_a_failing_repo():
    with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        broken = _github_source_repo(tmp_dir, "broken", "broken-branch")
        healthy = _github_source_repo(tmp_dir, "healthy", "healthy-branch")
        workspace = Workspace(
            {
                "id": "GH-PARTIAL",
                "repos": [
                    {"name": "broken", "worktreePath": str(broken)},
                    {"name": "healthy", "worktreePath": str(healthy)},
                ],
            }
        )

        healthy_payload = json.dumps(
            [{"number": 1, "url": "https://github.com/org/healthy/pull/1", "state": "OPEN"}]
        )
        responses = {
            "broken-branch": SystemExit("gh: HTTP 401"),
            "healthy-branch": healthy_payload,
        }
        with fake_gh(responses):
            seen, failures = github_sync.sync_prs(_sync_test_config(), workspace)

        if [item["repo"] for item in seen] != ["healthy"]:
            raise AssertionError(f"Expected the healthy repo to still sync, got {seen}")
        if [failure["repo"] for failure in failures] != ["broken"]:
            raise AssertionError(f"Expected only the broken repo to be reported as failed, got {failures}")


def test_github_sync_pr_view_command_prefers_url_then_repo_and_number():
    build = github_sync._pr_view_command

    by_url = build({"url": " https://github.com/org/app/pull/3 ", "number": 99, "repo": "other/repo"})
    if by_url[:4] != ["gh", "pr", "view", "https://github.com/org/app/pull/3"]:
        raise AssertionError(f"Expected a recorded url to be used verbatim, got {by_url}")

    by_number = build({"number": 3, "repo": "org/app"})
    if by_number[:6] != ["gh", "pr", "view", "3", "--repo", "org/app"]:
        raise AssertionError(f"Expected number + owner/name repo to be used, got {by_number}")

    from_repo_url = build({"number": 3, "repo": "https://github.com/org/app/pull/3"})
    if from_repo_url[4:6] != ["--repo", "org/app"]:
        raise AssertionError(f"Expected owner/name to be parsed out of a PR url in `repo`, got {from_repo_url}")

    for pr in ({"repo": "org/app"}, {"number": 3}, {"number": 3, "repo": "app"}):
        if build(pr) is not None:
            raise AssertionError(f"Expected an underspecified snapshot {pr} to yield no command")


def test_github_sync_refresh_recorded_prs_skips_unchanged_and_unviewable():
    workspace = Workspace(
        {
            "id": "GH-REFRESH-SKIP",
            "links": {
                "githubPrs": [
                    {"repo": "app", "number": None},
                    {
                        "repo": "app",
                        "number": 4,
                        "url": "https://github.com/org/app/pull/4",
                        "branch": "feature",
                        "state": "OPEN",
                        "merged": False,
                        "title": "Unchanged",
                    },
                ]
            },
        }
    )
    live = json.dumps(
        {
            "number": 4,
            "url": "https://github.com/org/app/pull/4",
            "state": "OPEN",
            "mergedAt": None,
            "title": "Unchanged",
            "headRefName": "feature",
        }
    )
    calls = []
    with fake_gh({"https://github.com/org/app/pull/4": live}, calls=calls):
        changed = github_sync.refresh_recorded_prs(workspace)

    if changed:
        raise AssertionError("Expected an identical live snapshot to report no change")
    if len(calls) != 1:
        raise AssertionError(f"Expected only the viewable snapshot to be queried, got {calls}")


def test_github_sync_refresh_recorded_prs_ignores_unusable_gh_responses():
    def workspace_with_open_pr():
        return Workspace(
            {
                "id": "GH-REFRESH-BAD",
                "links": {
                    "githubPrs": [
                        {
                            "repo": "app",
                            "number": 5,
                            "url": "https://github.com/org/app/pull/5",
                            "state": "OPEN",
                            "merged": False,
                        }
                    ]
                },
            }
        )

    for label, raw in (
        ("empty output", ""),
        ("unparsable json", "not json"),
        ("non-object json", "[]"),
        ("object without a number", json.dumps({"state": "MERGED"})),
    ):
        workspace = workspace_with_open_pr()
        with fake_gh({"https://github.com/org/app/pull/5": raw}):
            changed = github_sync.refresh_recorded_prs(workspace)
        if changed:
            raise AssertionError(f"Expected {label} to leave the snapshot unchanged")
        if workspace.github_prs[0]["state"] != "OPEN":
            raise AssertionError(f"Expected {label} to preserve the recorded snapshot, got {workspace.github_prs}")


def test_github_sync_refresh_recorded_prs_treats_merged_state_without_merged_at_as_merged():
    workspace = Workspace(
        {
            "id": "GH-REFRESH-STATE",
            "links": {
                "githubPrs": [
                    {
                        "repo": "app",
                        "number": 6,
                        "url": "https://github.com/org/app/pull/6",
                        "state": "OPEN",
                        "merged": False,
                    }
                ]
            },
        }
    )
    live = json.dumps({"number": 6, "state": "merged", "mergedAt": None, "title": "Landed"})
    with fake_gh({"https://github.com/org/app/pull/6": live}):
        changed = github_sync.refresh_recorded_prs(workspace)

    if not changed:
        raise AssertionError("Expected a MERGED state to be picked up as a change")
    pr = workspace.github_prs[0]
    if not pr["merged"] or pr["state"] != "merged":
        raise AssertionError(f"Expected the snapshot to be marked merged, got {pr}")
    if pr["url"] != "https://github.com/org/app/pull/6":
        raise AssertionError(f"Expected the recorded url to be preserved when `gh` omits it, got {pr}")


def test_github_sync_gh_json_applies_command_timeout():
    """Every `gh` invocation must be bounded so a stuck call cannot hang cleanup."""
    captured = {}
    original_run = github_sync.run

    def fake_run(cmd, *args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return "{}"

    github_sync.run = fake_run
    try:
        github_sync._gh_json(["gh", "pr", "view", "1", "--json", "state"])
    finally:
        github_sync.run = original_run
    if captured.get("timeout") != github_sync._GH_TIMEOUT_SECONDS:
        raise AssertionError(
            f"Expected _gh_json to bound gh with a {github_sync._GH_TIMEOUT_SECONDS}s timeout, "
            f"got {captured.get('timeout')}"
        )


GITHUB_SYNC_TESTS = [
    test_github_sync_gh_json_applies_command_timeout,
    test_github_sync_sync_prs_records_snapshots_per_repo,
    test_github_sync_sync_prs_marks_merged_from_merged_at,
    test_github_sync_sync_prs_skips_repos_it_cannot_query,
    test_github_sync_sync_prs_reports_unparsable_gh_output_as_a_failure,
    test_github_sync_sync_prs_keeps_syncing_after_a_failing_repo,
    test_github_sync_pr_view_command_prefers_url_then_repo_and_number,
    test_github_sync_refresh_recorded_prs_skips_unchanged_and_unviewable,
    test_github_sync_refresh_recorded_prs_ignores_unusable_gh_responses,
    test_github_sync_refresh_recorded_prs_treats_merged_state_without_merged_at_as_merged,
]


def main():
    passed = support.run_plain(GITHUB_SYNC_TESTS)
    print(f"{passed} self-check(s) passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"self-check failed: {error}", file=sys.stderr)
        sys.exit(1)
