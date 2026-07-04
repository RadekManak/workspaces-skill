#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "scripts" / "workspace.py"
WORKSPACE_SANDCASTLE = ROOT / "scripts" / "workspace_with_sandcastle.py"
sys.path.insert(0, str(ROOT / "scripts"))

from workspaces.git import branch_exists


def run(cmd, *, env=None, cwd=None, check=True):
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            "Command failed:\n"
            f"  {' '.join(str(part) for part in cmd)}\n"
            f"exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def assert_contains(haystack, needle):
    if needle not in haystack:
        raise AssertionError(f"Expected to find {needle!r} in:\n{haystack}")


def assert_not_contains(haystack, needle):
    if needle in haystack:
        raise AssertionError(f"Expected not to find {needle!r} in:\n{haystack}")


def assert_path(path):
    if not Path(path).exists():
        raise AssertionError(f"Expected path to exist: {path}")


def first_stdout_line(stdout):
    lines = stdout.splitlines()
    if not lines:
        raise AssertionError(f"Expected stdout line, got empty output:\n{stdout}")
    return lines[0]


def parse_final_stdout_json(stdout):
    text = stdout.rstrip()
    idx = text.rfind("\n{")
    if idx == -1:
        if text.startswith("{"):
            return json.loads(text)
        raise AssertionError(f"No JSON object found in stdout:\n{stdout}")
    return json.loads(text[idx + 1 :])


def parse_trailing_stdout_json_blocks(stdout):
    blocks = []
    remaining = stdout.rstrip()
    while remaining:
        idx = remaining.rfind("\n{")
        if idx == -1:
            if remaining.startswith("{"):
                blocks.insert(0, json.loads(remaining))
            break
        blocks.insert(0, json.loads(remaining[idx + 1 :]))
        remaining = remaining[:idx].rstrip()
    return blocks


def assert_final_vscode_json(stdout, workspace_file):
    on_disk = json.loads(Path(workspace_file).read_text(encoding="utf-8"))
    printed = parse_final_stdout_json(stdout)
    if printed != on_disk:
        raise AssertionError(
            f"Final stdout JSON does not match on-disk .code-workspace:\n"
            f"printed={printed}\non_disk={on_disk}"
        )
    return printed


def write_config(config_path, tmp, workspace_cli):
    run([str(workspace_cli), "init-config", "--non-interactive"], env={"WORKSPACES_CONFIG": str(config_path)})
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["source_root"] = str(tmp / "src")
    config["workspace_root"] = str(tmp / "workspaces")
    config["ledger_root"] = str(tmp / "ledger")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return {
        **os.environ,
        "WORKSPACES_CONFIG": str(config_path),
    }


def make_source_repo(tmp, name="repo"):
    source = tmp / "src" / name
    source.mkdir(parents=True)
    run(["git", "-C", str(source), "init", "-b", "main"])
    run(["git", "-C", str(source), "config", "user.email", "test@example.com"])
    run(["git", "-C", str(source), "config", "user.name", "Test User"])
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    run(["git", "-C", str(source), "add", "README.md"])
    run(["git", "-C", str(source), "commit", "-m", "init"])
    run(["git", "-C", str(source), "remote", "add", "origin", "."])
    run(["git", "-C", str(source), "update-ref", "refs/remotes/origin/main", "HEAD"])
    return source


def read_yaml(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def read_frontmatter(path):
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"Expected frontmatter in {path}")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise AssertionError(f"Unclosed frontmatter in {path}")
    return yaml.safe_load(text[4:end])


def test_config_and_ledger_workspace(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    assert_path(config_path)

    result = run(
        [
            str(workspace_cli),
            "create",
            "TASK-1",
            "--title",
            "Ledger only",
            "--input",
            "Build something.",
        ],
        env=env,
    )
    ledger = Path(first_stdout_line(result.stdout))
    assert_path(ledger / "workspace.yaml")
    assert_path(ledger / "spec.md")
    assert_path(ledger / "notes.md")
    assert_path(ledger / "issues")
    workspace_file = tmp / "workspaces" / "TASK-1" / "TASK-1.code-workspace"
    assert_path(workspace_file)
    assert_final_vscode_json(result.stdout, workspace_file)
    workspace_payload = json.loads(workspace_file.read_text(encoding="utf-8"))
    folder_names = [folder["name"] for folder in workspace_payload["folders"]]
    if folder_names != ["TASK-1 ledger"]:
        raise AssertionError(f"Unexpected VS Code workspace folders: {workspace_payload}")
    open_result = run([str(workspace_cli), "open", "TASK-1", "--print"], env=env)
    open_path = first_stdout_line(open_result.stdout)
    assert_final_vscode_json(open_result.stdout, workspace_file)
    if Path(open_path) != workspace_file:
        raise AssertionError(f"Unexpected open path: {open_path}")
    assert_contains((ledger / "spec.md").read_text(encoding="utf-8"), "Build something.")


def test_worktree_adopt_status_note_close(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")

    run([str(workspace_cli), "create", "TASK-2", "--repo", "repo"], env=env)
    worktree = tmp / "workspaces" / "TASK-2" / "repo"
    assert_path(worktree)
    workspace_file = tmp / "workspaces" / "TASK-2" / "TASK-2.code-workspace"
    assert_path(workspace_file)
    status = run([str(workspace_cli), "status", "TASK-2"], env=env).stdout
    assert_contains(status, "VS Code:")
    assert_contains(status, "branch: TASK-2")

    workspace_file.unlink()
    doctor = json.loads(run([str(workspace_cli), "doctor", "TASK-2", "--json"], env=env).stdout)
    if "vscode-workspace" not in [issue["code"] for issue in doctor["issues"]]:
        raise AssertionError(f"Expected doctor to report stale VS Code workspace: {doctor}")
    run([str(workspace_cli), "doctor", "TASK-2", "--fix"], env=env)
    assert_path(workspace_file)

    before = tmp / "ledger" / "workspaces" / "TASK-2" / "notes.md"
    before_text = before.read_text(encoding="utf-8")
    run([str(workspace_cli), "note", "TASK-2", "Manual note."], env=env)
    after_text = before.read_text(encoding="utf-8")
    assert_contains(after_text, before_text.strip())
    assert_contains(after_text, "Manual note.")

    run([str(workspace_cli), "adopt", str(worktree), "--id", "TASK-2-ADOPTED"], env=env)
    assert_path(tmp / "ledger" / "workspaces" / "TASK-2-ADOPTED" / "workspace.yaml")
    adopted_workspace_file = tmp / "workspaces" / "TASK-2-ADOPTED" / "TASK-2-ADOPTED.code-workspace"
    assert_path(adopted_workspace_file)
    adopted_payload = json.loads(adopted_workspace_file.read_text(encoding="utf-8"))
    adopted_names = [folder["name"] for folder in adopted_payload["folders"]]
    if adopted_names != ["TASK-2-ADOPTED ledger", "repo"]:
        raise AssertionError(f"Unexpected adopted VS Code workspace folders: {adopted_payload}")

    run([str(workspace_cli), "close", "TASK-2"], env=env)
    assert_path(worktree)
    closed = read_yaml(tmp / "ledger" / "workspaces" / "TASK-2" / "workspace.yaml")
    if closed["state"] != "closed":
        raise AssertionError(f"Expected closed state, got {closed['state']}")


def test_repo_crud_and_pr_lifecycle(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo-a")
    make_source_repo(tmp, "repo-b")
    run([str(workspace_cli), "create", "TASK-CRUD", "--title", "Repo CRUD"], env=env)

    run([str(workspace_cli), "repo", "add", "TASK-CRUD", "repo-a"], env=env)
    worktree_a = tmp / "workspaces" / "TASK-CRUD" / "repo-a"
    assert_path(worktree_a)
    repos = json.loads(run([str(workspace_cli), "repo", "list", "TASK-CRUD", "--json"], env=env).stdout)["repos"]
    if [repo["name"] for repo in repos] != ["repo-a"]:
        raise AssertionError(f"Unexpected repos after add: {repos}")

    run([str(workspace_cli), "repo", "refresh", "TASK-CRUD"], env=env)
    run([str(workspace_cli), "repo", "adopt", "TASK-CRUD", str(worktree_a), "--name", "repo-a-adopted"], env=env)
    repos = json.loads(run([str(workspace_cli), "repo", "list", "TASK-CRUD", "--json"], env=env).stdout)["repos"]
    if repos[0]["name"] != "repo-a-adopted":
        raise AssertionError(f"Expected repo adopt to update metadata, got {repos}")

    run([str(workspace_cli), "repo", "add", "TASK-CRUD", "repo-b"], env=env)
    worktree_b = tmp / "workspaces" / "TASK-CRUD" / "repo-b"
    assert_path(worktree_b)
    run([str(workspace_cli), "repo", "remove", "TASK-CRUD", "repo-b", "--delete-worktree"], env=env)
    if worktree_b.exists():
        raise AssertionError(f"Expected repo remove to delete linked worktree: {worktree_b}")

    state = run(
        [
            str(workspace_cli),
            "pr",
            "TASK-CRUD",
            "feedback",
            "--url",
            "https://github.com/example/repo/pull/1",
        ],
        env=env,
    ).stdout.strip()
    if state != "review-feedback":
        raise AssertionError(f"Expected review-feedback state, got {state}")
    data = read_yaml(tmp / "ledger" / "workspaces" / "TASK-CRUD" / "workspace.yaml")
    if data["state"] != "review-feedback" or not data["links"]["githubPrs"]:
        raise AssertionError(f"Expected PR lifecycle metadata, got {data}")


def test_issue_graph_and_user_review(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    run([str(workspace_cli), "create", "TASK-3", "--title", "Issue graph"], env=env)
    run([str(workspace_cli), "issue", "create", "TASK-3", "1", "--title", "First"], env=env)
    run([str(workspace_cli), "issue", "create", "TASK-3", "2", "--title", "Second"], env=env)
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "TASK-3",
            "3",
            "--title",
            "Dependent",
            "--blocked-by",
            "1",
            "--blocked-by",
            "2",
        ],
        env=env,
    )
    ready = json.loads(run([str(workspace_cli), "issue", "ready", "TASK-3", "--json"], env=env).stdout)
    if [issue["id"] for issue in ready["issues"]] != ["1", "2"]:
        raise AssertionError(f"Unexpected ready issues: {ready}")
    run([str(workspace_cli), "issue", "set-status", "TASK-3", "1", "merged"], env=env)
    run([str(workspace_cli), "issue", "set-status", "TASK-3", "2", "merged"], env=env)
    ready = json.loads(run([str(workspace_cli), "issue", "ready", "TASK-3", "--json"], env=env).stdout)
    if [issue["id"] for issue in ready["issues"]] != ["3"]:
        raise AssertionError(f"Expected issue 3 to unblock, got {ready}")
    run([str(workspace_cli), "issue", "set-status", "TASK-3", "3", "merged"], env=env)
    status = run([str(workspace_cli), "status", "TASK-3"], env=env).stdout
    assert_contains(status, "State: user-review")


def test_sandcastle_plan_init_runner_and_reconcile(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")

    run([str(workspace_cli), "create", "TASK-4", "--title", "Sandcastle", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "TASK-4", "1", "--title", "First"], env=env)
    run(
        [str(workspace_cli), "issue", "create", "TASK-4", "2", "--title", "Human", "--type", "hitl"],
        env=env,
    )

    plan_result = run(
        [str(workspace_cli), "sandcastle", "plan", "TASK-4", "--json"],
        env=env,
    )
    plan = parse_final_stdout_json(plan_result.stdout)
    if plan["targetedIssueIds"] != ["1"]:
        raise AssertionError(f"Unexpected targeted issues: {plan}")
    if plan["issuesByRepo"]["repo"]["readyButNotTargeted"][0]["id"] != "2":
        raise AssertionError(f"Unexpected HITL bucket: {plan}")
    wave = plan["execution"]["waves"][0]["issues"]
    if wave[0]["id"] != "1" or wave[0]["repo"] != "repo":
        raise AssertionError(f"Unexpected execution wave: {wave}")
    plan_path = Path(
        read_yaml(tmp / "ledger" / "workspaces" / "TASK-4" / "workspace.yaml")["sandcastle"][
            "currentPlan"
        ]["path"]
    )
    assert_path(plan_path)

    workspace_yaml = read_yaml(tmp / "ledger" / "workspaces" / "TASK-4" / "workspace.yaml")
    current = workspace_yaml.get("sandcastle", {}).get("currentPlan") or {}
    if current.get("path") != str(plan_path):
        raise AssertionError(f"Expected current-plan pointer, got {workspace_yaml}")
    if current.get("targetedIssueIds") != ["1"]:
        raise AssertionError(f"Unexpected pointer targetedIssueIds: {current}")
    if not str(current.get("fingerprint", "")).startswith("sha256:"):
        raise AssertionError(f"Expected fingerprint on pointer, got {current}")

    notes = (tmp / "ledger" / "workspaces" / "TASK-4" / "notes.md").read_text(encoding="utf-8")
    assert_contains(notes, str(plan_path))
    assert_contains(notes, "repo")

    issue1 = read_frontmatter(tmp / "ledger" / "workspaces" / "TASK-4" / "issues" / "1.md")
    sc1 = issue1.get("sandcastle") or {}
    if sc1.get("type") != "AFK":
        raise AssertionError(f"Expected lazy sandcastle block on targeted issue 1, got {issue1}")
    if issue1.get("repo") != "repo":
        raise AssertionError(f"Expected default repo on issue 1, got {issue1}")

    run([str(workspace_cli), "sandcastle", "init-runner", "TASK-4"], env=env)
    scaffold = tmp / "workspaces" / "TASK-4" / "repo" / ".sandcastle"
    assert_path(scaffold / "main.mts")
    assert_path(scaffold / "implement-prompt.md")
    assert_path(scaffold / "review-prompt.md")
    repeat = run(
        [str(workspace_cli), "sandcastle", "init-runner", "TASK-4"],
        env=env,
        check=False,
    )
    if repeat.returncode == 0:
        raise AssertionError("Expected init-runner to refuse overwrite")
    run([str(workspace_cli), "sandcastle", "init-runner", "TASK-4", "--force"], env=env)

    result_path = tmp / "manual-result.json"
    result_path.write_text(
        json.dumps({"issues": [{"id": "1", "status": "merged", "reviewStatus": "approved"}]}),
        encoding="utf-8",
    )
    run(
        [str(workspace_cli), "sandcastle", "reconcile-result", "TASK-4", str(result_path)],
        env=env,
    )
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "TASK-4" / "issues" / "1.md")
    sc = issue.get("sandcastle") or {}
    if issue["status"] != "merged" or sc.get("reviewStatus") != "approved":
        raise AssertionError(f"Expected reconciled issue, got {issue}")


def test_sandcastle_plan_multi_repo_cross_dependency(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "frontend")
    make_source_repo(tmp, "backend")
    run(
        [str(workspace_cli), "create", "CROSS", "--title", "Cross repo", "--repo", "frontend"],
        env=env,
    )
    run([str(workspace_cli), "repo", "add", "CROSS", "backend"], env=env)
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "CROSS",
            "fe-1",
            "--title",
            "Frontend first",
            "--repo",
            "frontend",
        ],
        env=env,
    )
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "CROSS",
            "be-1",
            "--title",
            "Backend after frontend",
            "--repo",
            "backend",
            "--blocked-by",
            "fe-1",
        ],
        env=env,
    )

    plan = parse_final_stdout_json(
        run([str(workspace_cli), "sandcastle", "plan", "CROSS", "--json"], env=env).stdout
    )
    if plan["targetedIssueIds"] != ["be-1", "fe-1"]:
        raise AssertionError(
            f"Expected both issues in execution scope, got {plan['targetedIssueIds']}"
        )
    blocked = plan["issuesByRepo"]["backend"]["blocked"]
    if len(blocked) != 1 or blocked[0]["issue"]["id"] != "be-1":
        raise AssertionError(f"Expected backend issue in blocked bucket, got {blocked}")
    assert_contains(blocked[0]["reason"], "fe-1")
    waves = plan["execution"]["waves"]
    if len(waves) != 2:
        raise AssertionError(f"Expected two execution waves, got {waves}")
    if waves[0]["issues"][0]["id"] != "fe-1" or waves[0]["issues"][0]["repo"] != "frontend":
        raise AssertionError(f"Expected wave 0 frontend fe-1, got {waves[0]}")
    if waves[1]["issues"][0]["id"] != "be-1" or waves[1]["issues"][0]["repo"] != "backend":
        raise AssertionError(f"Expected wave 1 backend be-1, got {waves[1]}")
    if waves[1]["issues"][0]["dependsOn"] != ["fe-1"]:
        raise AssertionError(f"Expected be-1 to depend on fe-1, got {waves[1]}")

    run([str(workspace_cli), "issue", "set-status", "CROSS", "fe-1", "merged"], env=env)
    replan = parse_final_stdout_json(
        run([str(workspace_cli), "sandcastle", "plan", "CROSS", "--json"], env=env).stdout
    )
    if replan["targetedIssueIds"] != ["be-1"]:
        raise AssertionError(f"Expected backend issue after frontend merged, got {replan['targetedIssueIds']}")
    if replan["execution"]["waves"][0]["issues"][0]["repo"] != "backend":
        raise AssertionError(f"Expected backend wave after replan, got {replan['execution']['waves']}")


def test_sandcastle_plan_limit_excludes_independently_ready_dependent(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "LIMIT", "--title", "Limit bound", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "LIMIT", "0", "--title", "Already done"], env=env)
    run([str(workspace_cli), "issue", "set-status", "LIMIT", "0", "merged"], env=env)
    # Both "1" and "2" are independently ready: their only blocker ("0") is
    # already merged, so neither one's readiness depends on the other.
    run(
        [str(workspace_cli), "issue", "create", "LIMIT", "1", "--title", "First", "--blocked-by", "0"],
        env=env,
    )
    run(
        [str(workspace_cli), "issue", "create", "LIMIT", "2", "--title", "Second", "--blocked-by", "0"],
        env=env,
    )

    plan = parse_final_stdout_json(
        run(
            [str(workspace_cli), "sandcastle", "plan", "LIMIT", "--limit", "1", "--json"],
            env=env,
        ).stdout
    )
    if plan["targetedIssueIds"] != ["1"]:
        raise AssertionError(
            f"Expected --limit to bound execution scope to just issue 1, got {plan['targetedIssueIds']}"
        )
    if len(plan["execution"]["waves"]) != 1 or plan["execution"]["waves"][0]["issues"] != [
        {"id": "1", "repo": "repo", "dependsOn": []}
    ]:
        raise AssertionError(f"Unexpected execution waves under --limit, got {plan['execution']['waves']}")
    excluded = plan["issuesByRepo"]["repo"]["readyButNotTargeted"]
    if not any(item["id"] == "2" and item.get("reason") == "excluded by --limit" for item in excluded):
        raise AssertionError(f"Expected issue 2 excluded by --limit, got {excluded}")


def test_sandcastle_plan_repo_filter_does_not_mistarget_unset_repo_issue(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "frontend")
    make_source_repo(tmp, "backend")
    run(
        [str(workspace_cli), "create", "SCOPE", "--title", "Repo filter", "--repo", "frontend"],
        env=env,
    )
    run([str(workspace_cli), "repo", "add", "SCOPE", "backend"], env=env)
    # No --repo given: on this multi-repo workspace the issue's repo stays
    # genuinely ambiguous/unset.
    run(
        [str(workspace_cli), "issue", "create", "SCOPE", "unset-issue", "--title", "No repo set"],
        env=env,
    )
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "SCOPE" / "issues" / "unset-issue.md")
    if issue.get("repo") is not None:
        raise AssertionError(f"Expected no repo default on multi-repo workspace, got {issue}")

    plan = parse_final_stdout_json(
        run(
            [str(workspace_cli), "sandcastle", "plan", "SCOPE", "--repo", "frontend", "--json"],
            env=env,
        ).stdout
    )
    if "unset-issue" in plan["targetedIssueIds"]:
        raise AssertionError(
            f"--repo frontend must not mistarget the repo-unset issue, got {plan['targetedIssueIds']}"
        )
    frontend_bucket = plan["issuesByRepo"]["frontend"]
    if any(
        item.get("id") == "unset-issue" or item.get("issue", {}).get("id") == "unset-issue"
        for bucket in frontend_bucket.values()
        for item in bucket
    ):
        raise AssertionError(f"Repo-unset issue must not appear under frontend, got {frontend_bucket}")
    unspecified = plan["issuesByRepo"].get("(unspecified)", {}).get("blocked", [])
    if not any(item["issue"]["id"] == "unset-issue" for item in unspecified):
        raise AssertionError(f"Expected repo-unset issue in (unspecified) bucket, got {plan['issuesByRepo']}")
    entry = next(item for item in unspecified if item["issue"]["id"] == "unset-issue")
    assert_contains(entry["reason"], "repo not specified")


def test_sandcastle_plan_unknown_repo_blocked(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo-a")
    make_source_repo(tmp, "repo-b")
    run(
        [str(workspace_cli), "create", "UNK", "--title", "Unknown repo", "--repo", "repo-a"],
        env=env,
    )
    run([str(workspace_cli), "repo", "add", "UNK", "repo-b"], env=env)
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "UNK",
            "orphan",
            "--title",
            "Points at removed repo",
            "--repo",
            "repo-a",
        ],
        env=env,
    )
    run([str(workspace_cli), "repo", "remove", "UNK", "repo-a"], env=env)
    plan = parse_final_stdout_json(
        run([str(workspace_cli), "sandcastle", "plan", "UNK", "--json"], env=env).stdout
    )
    if plan["targetedIssueIds"]:
        raise AssertionError(f"Expected no targeted issues, got {plan}")
    blocked = plan["issuesByRepo"].get("repo-a", {}).get("blocked", [])
    if len(blocked) != 1 or blocked[0]["issue"]["id"] != "orphan":
        raise AssertionError(f"Expected orphan issue in repo-a blocked bucket, got {plan['issuesByRepo']}")
    assert_contains(blocked[0]["reason"], "unknown repo")
    assert_contains(blocked[0]["reason"], "repo-a")


def test_issue_create_default_repo_single_repo_workspace(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE)
    make_source_repo(tmp, "only-repo")
    run(
        [str(WORKSPACE), "create", "ONE-REPO", "--title", "Single repo", "--repo", "only-repo"],
        env=env,
    )
    run(
        [str(WORKSPACE), "issue", "create", "ONE-REPO", "1", "--title", "Default repo"],
        env=env,
    )
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "ONE-REPO" / "issues" / "1.md")
    if issue.get("repo") != "only-repo":
        raise AssertionError(f"Expected default repo on single-repo workspace, got {issue}")


def test_sandcastle_plan_empty_still_writes_artifact(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "EMPTY", "--title", "Empty plan", "--repo", "repo"], env=env)
    plan = parse_final_stdout_json(
        run([str(workspace_cli), "sandcastle", "plan", "EMPTY", "--json"], env=env).stdout
    )
    if plan["targetedIssueIds"]:
        raise AssertionError(f"Expected empty targeted set, got {plan}")
    workspace_yaml = read_yaml(tmp / "ledger" / "workspaces" / "EMPTY" / "workspace.yaml")
    assert_path(workspace_yaml["sandcastle"]["currentPlan"]["path"])


def test_base_preserves_workspace_sandcastle_section(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE_SANDCASTLE)
    make_source_repo(tmp, "repo")
    run([str(WORKSPACE_SANDCASTLE), "create", "WS-SC", "--title", "Workspace sandcastle", "--repo", "repo"], env=env)
    run([str(WORKSPACE_SANDCASTLE), "sandcastle", "plan", "WS-SC", "--json"], env=env)
    before = read_yaml(tmp / "ledger" / "workspaces" / "WS-SC" / "workspace.yaml")
    sandcastle_before = before.get("sandcastle")
    if not sandcastle_before:
        raise AssertionError(f"Expected sandcastle section after plan, got {before}")
    run([str(WORKSPACE), "issue", "create", "WS-SC", "note-issue", "--title", "Trigger save"], env=env)
    after = read_yaml(tmp / "ledger" / "workspaces" / "WS-SC" / "workspace.yaml")
    if after.get("sandcastle") != sandcastle_before:
        raise AssertionError(
            f"Base entrypoint should preserve workspace sandcastle section, "
            f"before={sandcastle_before!r} after={after.get('sandcastle')!r}"
        )


def test_cleanup_plan_and_execution(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "TASK-6", "--title", "Cleanup", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "TASK-6", "1", "--title", "First"], env=env)

    worktree = tmp / "workspaces" / "TASK-6" / "repo"
    run(["git", "-C", str(worktree), "config", "user.email", "test@example.com"])
    run(["git", "-C", str(worktree), "config", "user.name", "Test User"])
    run(["git", "-C", str(worktree), "switch", "-c", "sandcastle/task-6/1"])
    (worktree / "README.md").write_text("hello\nchild\n", encoding="utf-8")
    run(["git", "-C", str(worktree), "add", "README.md"])
    run(["git", "-C", str(worktree), "commit", "-m", "child"])
    run(["git", "-C", str(worktree), "switch", "TASK-6"])
    run(["git", "-C", str(worktree), "merge", "sandcastle/task-6/1", "--no-edit"])

    result_path = tmp / "cleanup-result.json"
    result_path.write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "id": "1",
                        "status": "merged",
                        "reviewStatus": "approved",
                        "cleanupStatus": "pending",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    run([str(workspace_cli), "sandcastle", "reconcile-result", "TASK-6", str(result_path)], env=env)

    plan = json.loads(
        run([str(workspace_cli), "cleanup-plan", "TASK-6", "--issues", "--json"], env=env).stdout
    )
    candidates = plan["candidates"]
    if len(candidates) != 1:
        raise AssertionError(f"Expected one cleanup candidate, got {plan}")
    if candidates[0]["recommendedAction"] != "safe-to-delete":
        raise AssertionError(f"Expected safe cleanup candidate, got {candidates[0]}")
    if (
        run(
            [
                "git",
                "-C",
                str(worktree),
                "rev-parse",
                "--verify",
                "--quiet",
                "refs/heads/sandcastle/task-6/1",
            ],
            check=False,
        ).returncode
        != 0
    ):
        raise AssertionError("cleanup-plan should not delete the child branch")

    run([str(workspace_cli), "cleanup", "--item", "TASK-6:1"], env=env)
    if (
        run(
            [
                "git",
                "-C",
                str(worktree),
                "rev-parse",
                "--verify",
                "--quiet",
                "refs/heads/sandcastle/task-6/1",
            ],
            check=False,
        ).returncode
        == 0
    ):
        raise AssertionError("cleanup should delete the approved child branch")
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "TASK-6" / "issues" / "1.md")
    if issue.get("cleanupStatus") != "done":
        raise AssertionError(f"Expected cleanupStatus done, got {issue}")


def test_workspace_cleanup_plan_and_execution(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    source = make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "TASK-7", "--title", "Workspace cleanup", "--repo", "repo"], env=env)
    worktree = tmp / "workspaces" / "TASK-7" / "repo"
    assert_path(worktree)

    active_plan = json.loads(run([str(workspace_cli), "cleanup-plan", "TASK-7", "--json"], env=env).stdout)
    if active_plan["candidates"][0]["recommendedAction"] != "force-eligible":
        raise AssertionError(f"Expected active workspace cleanup to be force-eligible, got {active_plan}")

    run([str(workspace_cli), "close", "TASK-7"], env=env)
    plan = json.loads(
        run([str(workspace_cli), "cleanup-plan", "TASK-7", "--write", "--json"], env=env).stdout
    )
    plan_path = Path(plan["planPath"])
    assert_path(plan_path)
    candidate = plan["candidates"][0]
    if candidate["recommendedAction"] != "safe-to-remove":
        raise AssertionError(f"Expected safe workspace cleanup candidate, got {candidate}")
    if not Path(candidate["workspaceRoot"]).exists():
        raise AssertionError(f"Expected cleanup-plan to leave workspace root in place: {candidate}")

    extra = tmp / "workspaces" / "TASK-7" / "manual.txt"
    extra.write_text("changed after plan\n", encoding="utf-8")
    stale = run(
        [str(workspace_cli), "cleanup", "--from-plan", str(plan_path), "TASK-7"],
        env=env,
        check=False,
    )
    if stale.returncode == 0:
        raise AssertionError("Expected cleanup from a stale plan to fail")
    extra.unlink()

    fresh_plan = json.loads(
        run([str(workspace_cli), "cleanup-plan", "TASK-7", "--write", "--json"], env=env).stdout
    )
    fresh_plan_path = Path(fresh_plan["planPath"])
    assert_path(fresh_plan_path)
    run([str(workspace_cli), "cleanup", "--from-plan", str(fresh_plan_path), "TASK-7"], env=env)
    if worktree.exists():
        raise AssertionError(f"Expected workspace cleanup to remove worktree: {worktree}")
    if (tmp / "workspaces" / "TASK-7").exists():
        raise AssertionError("Expected workspace cleanup to remove the workspace root")
    assert_path(source)
    data = read_yaml(tmp / "ledger" / "workspaces" / "TASK-7" / "workspace.yaml")
    if data.get("state") != "closed" or data.get("cleanupStatus") != "done":
        raise AssertionError(f"Expected closed cleaned workspace, got {data}")


def test_workspace_force_cleanup(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    source = make_source_repo(tmp, "repo")
    run(
        [str(workspace_cli), "create", "TASK-8", "--title", "Force cleanup", "--repo", "repo"],
        env=env,
    )
    worktree = tmp / "workspaces" / "TASK-8" / "repo"
    assert_path(worktree)
    branch = run(
        ["git", "-C", str(worktree), "branch", "--show-current"],
        env=env,
    ).stdout.strip()

    (worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    plan = json.loads(run([str(workspace_cli), "cleanup-plan", "TASK-8", "--json"], env=env).stdout)
    candidate = plan["candidates"][0]
    if candidate["recommendedAction"] != "force-eligible":
        raise AssertionError(f"Expected force-eligible workspace, got {candidate}")

    refused = run([str(workspace_cli), "cleanup", "TASK-8"], env=env, check=False)
    if refused.returncode == 0:
        raise AssertionError("Expected cleanup without --force to fail")

    run([str(workspace_cli), "cleanup", "TASK-8", "--force"], env=env)
    if worktree.exists():
        raise AssertionError(f"Expected force cleanup to remove worktree: {worktree}")
    if (tmp / "workspaces" / "TASK-8").exists():
        raise AssertionError("Expected force cleanup to remove the workspace root")
    if not branch_exists(source, branch):
        raise AssertionError(f"Expected task branch to remain on source repo: {branch}")

    data = read_yaml(tmp / "ledger" / "workspaces" / "TASK-8" / "workspace.yaml")
    if data.get("cleanupAction") != "force-removed":
        raise AssertionError(f"Expected force-removed cleanup action, got {data}")
    notes = (tmp / "ledger" / "workspaces" / "TASK-8" / "notes.md").read_text(encoding="utf-8")
    if "Force-cleaned workspace." not in notes:
        raise AssertionError(f"Expected force cleanup note, got:\n{notes}")


def test_doctor_fix_output(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")

    run([str(workspace_cli), "create", "TASK-DOC", "--title", "Doctor fix", "--repo", "repo"], env=env)
    workspace_file = tmp / "workspaces" / "TASK-DOC" / "TASK-DOC.code-workspace"
    assert_path(workspace_file)

    workspace_file.unlink()

    doctor_json = json.loads(
        run([str(workspace_cli), "doctor", "TASK-DOC", "--json"], env=env).stdout
    )
    vscode_issues = [i for i in doctor_json["issues"] if i["code"] == "vscode-workspace"]
    if not vscode_issues:
        raise AssertionError("Expected vscode-workspace issue before fix")
    if vscode_issues[0].get("fixed"):
        raise AssertionError("Expected no fixed flag before --fix")

    fix_json = json.loads(
        run([str(workspace_cli), "doctor", "TASK-DOC", "--fix", "--json"], env=env).stdout
    )
    fixed_issues = [i for i in fix_json["issues"] if i.get("fixed")]
    if not fixed_issues:
        raise AssertionError(f"Expected fixed flag in --fix --json output: {fix_json}")
    vscode_fixed = [i for i in fix_json["issues"] if i["code"] == "vscode-workspace"]
    if not vscode_fixed or not vscode_fixed[0].get("fixed"):
        raise AssertionError(f"Expected vscode-workspace to be marked fixed: {fix_json}")

    workspace_file.unlink()

    fix_text = run([str(workspace_cli), "doctor", "TASK-DOC", "--fix"], env=env).stdout
    assert_contains(fix_text, "- fixed vscode-workspace:")
    if "- warning vscode-workspace" in fix_text:
        raise AssertionError(f"Expected fixed prefix instead of warning: {fix_text}")
    assert_final_vscode_json(fix_text, workspace_file)

    worktree = tmp / "workspaces" / "TASK-DOC" / "repo"
    (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    dirty_text = run([str(workspace_cli), "doctor", "TASK-DOC", "--fix"], env=env).stdout
    assert_contains(dirty_text, "- warning repo-dirty:")
    if parse_trailing_stdout_json_blocks(dirty_text):
        raise AssertionError(f"Expected no .code-workspace JSON when doctor --fix made no repairs:\n{dirty_text}")


def test_doctor_multi_id_and_all(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")

    run([str(workspace_cli), "create", "DOC-1", "--title", "Doctor 1", "--repo", "repo"], env=env)
    run([str(workspace_cli), "create", "DOC-2", "--title", "Doctor 2"], env=env)
    run([str(workspace_cli), "create", "DOC-3", "--title", "Doctor 3"], env=env)

    result = run([str(workspace_cli), "doctor", "DOC-1", "DOC-2"], env=env)
    assert_contains(result.stdout, "DOC-1:")
    assert_contains(result.stdout, "DOC-2:")

    result = run([str(workspace_cli), "doctor", "DOC-1", "DOC-2", "--fix"], env=env)
    assert_contains(result.stdout, "DOC-1:")
    assert_contains(result.stdout, "DOC-2:")

    json_result = json.loads(
        run([str(workspace_cli), "doctor", "DOC-1", "DOC-2", "--json"], env=env).stdout
    )
    if not isinstance(json_result, list):
        raise AssertionError(f"Expected JSON array for multiple workspaces, got {type(json_result)}")
    if len(json_result) != 2:
        raise AssertionError(f"Expected 2 entries in JSON array, got {len(json_result)}")
    if {entry["workspace"] for entry in json_result} != {"DOC-1", "DOC-2"}:
        raise AssertionError(f"Unexpected workspace ids in JSON: {json_result}")

    single_json = json.loads(
        run([str(workspace_cli), "doctor", "DOC-1", "--json"], env=env).stdout
    )
    if not isinstance(single_json, dict) or "workspace" not in single_json:
        raise AssertionError(f"Expected single JSON object for one workspace, got {single_json}")

    all_result = run([str(workspace_cli), "doctor", "--all"], env=env)
    assert_contains(all_result.stdout, "DOC-1:")
    assert_contains(all_result.stdout, "DOC-2:")
    assert_contains(all_result.stdout, "DOC-3:")

    all_json = json.loads(
        run([str(workspace_cli), "doctor", "--all", "--json"], env=env).stdout
    )
    if not isinstance(all_json, list):
        raise AssertionError(f"Expected JSON array for --all, got {type(all_json)}")
    all_ids = {entry["workspace"] for entry in all_json}
    for expected in ("DOC-1", "DOC-2", "DOC-3"):
        if expected not in all_ids:
            raise AssertionError(f"Expected {expected} in --all output, got {all_ids}")

    # `--fix --json` for multi-id/--all must stay pure JSON: json.loads on the
    # whole stdout fails with "Extra data" if a .code-workspace block leaked
    # in after the doctor report, so a clean parse is itself the assertion.
    fix_json_result = json.loads(
        run([str(workspace_cli), "doctor", "DOC-1", "DOC-2", "--fix", "--json"], env=env).stdout
    )
    if not isinstance(fix_json_result, list) or len(fix_json_result) != 2:
        raise AssertionError(f"Expected 2-entry JSON array for --fix --json, got {fix_json_result}")

    fix_all_json = json.loads(
        run([str(workspace_cli), "doctor", "--all", "--fix", "--json"], env=env).stdout
    )
    if not isinstance(fix_all_json, list) or len(fix_all_json) != 3:
        raise AssertionError(f"Expected 3-entry JSON array for --all --fix --json, got {fix_all_json}")

    no_args = run([str(workspace_cli), "doctor"], env=env, check=False)
    if no_args.returncode == 0:
        raise AssertionError("Expected doctor with no args and no CWD workspace to fail")


def test_doctor_fix_skips_vscode_json_for_cleanup_done_workspace(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run(
        [str(workspace_cli), "create", "TASK-CLEANUP-FIX", "--title", "Cleanup fix", "--repo", "repo"],
        env=env,
    )

    workspace_root = tmp / "workspaces" / "TASK-CLEANUP-FIX"
    workspace_file = workspace_root / "TASK-CLEANUP-FIX.code-workspace"
    assert_path(workspace_file)

    ledger_root = tmp / "ledger" / "workspaces" / "TASK-CLEANUP-FIX"
    workspace_yaml = ledger_root / "workspace.yaml"
    notes_path = ledger_root / "notes.md"
    assert_path(notes_path)

    # Simulate a completed cleanup: the workspace root (worktrees +
    # .code-workspace) is gone from disk, and workspace.yaml is marked
    # cleanupStatus: done with no vscodeWorkspacePath, mirroring what the
    # real `cleanup` command does.
    shutil.rmtree(workspace_root)
    data = read_yaml(workspace_yaml)
    data["cleanupStatus"] = "done"
    data.pop("vscodeWorkspacePath", None)
    workspace_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    # Break something unrelated to the .code-workspace file so `--fix` has
    # a real, unrelated repair to make.
    notes_path.unlink()

    fix_result = run([str(workspace_cli), "doctor", "TASK-CLEANUP-FIX", "--fix"], env=env)
    assert_contains(fix_result.stdout, "fixed missing-file")
    assert_path(notes_path)

    if workspace_root.exists():
        raise AssertionError(
            f"Expected doctor --fix on a cleanup-done workspace not to resurrect the "
            f"workspace root: {workspace_root}"
        )
    if workspace_file.exists():
        raise AssertionError(
            f"Expected doctor --fix on a cleanup-done workspace not to resurrect "
            f".code-workspace: {workspace_file}"
        )
    if parse_trailing_stdout_json_blocks(fix_result.stdout):
        raise AssertionError(
            f"Expected no .code-workspace JSON block for a cleanup-done workspace, got:\n{fix_result.stdout}"
        )

    after = read_yaml(workspace_yaml)
    if "vscodeWorkspacePath" in after:
        raise AssertionError(f"Expected vscodeWorkspacePath to remain absent, got {after}")
    if after.get("cleanupStatus") != "done":
        raise AssertionError(f"Expected cleanupStatus to remain done, got {after}")


def test_topology_commands_print_vscode_json(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE)
    make_source_repo(tmp, "repo-a")
    make_source_repo(tmp, "repo-b")

    create_result = run(
        [str(WORKSPACE), "create", "TOPO-1", "--title", "Topology JSON", "--repo", "repo-a"],
        env=env,
    )
    workspace_file = tmp / "workspaces" / "TOPO-1" / "TOPO-1.code-workspace"
    payload = assert_final_vscode_json(create_result.stdout, workspace_file)
    folder_names = [folder["name"] for folder in payload["folders"]]
    if folder_names != ["TOPO-1 ledger", "repo-a"]:
        raise AssertionError(f"Unexpected create folders: {payload}")

    worktree_a = tmp / "workspaces" / "TOPO-1" / "repo-a"
    adopt_result = run(
        [str(WORKSPACE), "adopt", str(worktree_a), "--id", "TOPO-ADOPT"],
        env=env,
    )
    adopted_file = tmp / "workspaces" / "TOPO-ADOPT" / "TOPO-ADOPT.code-workspace"
    payload = assert_final_vscode_json(adopt_result.stdout, adopted_file)
    if [folder["name"] for folder in payload["folders"]] != ["TOPO-ADOPT ledger", "repo-a"]:
        raise AssertionError(f"Unexpected adopt folders: {payload}")

    add_result = run([str(WORKSPACE), "repo", "add", "TOPO-1", "repo-b"], env=env)
    payload = assert_final_vscode_json(add_result.stdout, workspace_file)
    if [folder["name"] for folder in payload["folders"]] != ["TOPO-1 ledger", "repo-a", "repo-b"]:
        raise AssertionError(f"Unexpected repo add folders: {payload}")

    worktree_b = tmp / "workspaces" / "TOPO-1" / "repo-b"
    adopt_repo_result = run(
        [str(WORKSPACE), "repo", "adopt", "TOPO-1", str(worktree_b), "--name", "repo-b-renamed"],
        env=env,
    )
    payload = assert_final_vscode_json(adopt_repo_result.stdout, workspace_file)
    if "repo-b-renamed" not in [folder["name"] for folder in payload["folders"]]:
        raise AssertionError(f"Unexpected repo adopt folders: {payload}")

    remove_result = run(
        [str(WORKSPACE), "repo", "remove", "TOPO-1", "repo-b-renamed", "--delete-worktree"],
        env=env,
    )
    payload = assert_final_vscode_json(remove_result.stdout, workspace_file)
    if [folder["name"] for folder in payload["folders"]] != ["TOPO-1 ledger", "repo-a"]:
        raise AssertionError(f"Unexpected repo remove folders: {payload}")
    if worktree_b.exists():
        raise AssertionError(f"Expected deleted worktree to be gone: {worktree_b}")

    workspace_file.unlink()
    fix_result = run([str(WORKSPACE), "doctor", "TOPO-1", "--fix"], env=env)
    assert_final_vscode_json(fix_result.stdout, workspace_file)

    open_result = run([str(WORKSPACE), "open", "TOPO-1", "--print"], env=env)
    assert_final_vscode_json(open_result.stdout, workspace_file)

    workspace_file.unlink()
    adopted_file.unlink()
    multi_fix = run([str(WORKSPACE), "doctor", "TOPO-1", "TOPO-ADOPT", "--fix"], env=env)
    assert_path(workspace_file)
    assert_path(adopted_file)
    json_blocks = parse_trailing_stdout_json_blocks(multi_fix.stdout)
    if len(json_blocks) != 2:
        raise AssertionError(
            f"Expected two .code-workspace JSON blocks for multi-id doctor --fix, got {len(json_blocks)}"
        )
    topo_on_disk = json.loads(workspace_file.read_text(encoding="utf-8"))
    adopted_on_disk = json.loads(adopted_file.read_text(encoding="utf-8"))
    if json_blocks != [topo_on_disk, adopted_on_disk]:
        raise AssertionError(
            f"Unexpected multi-id doctor --fix JSON blocks:\n"
            f"blocks={json_blocks}\n"
            f"topo={topo_on_disk}\n"
            f"adopted={adopted_on_disk}"
        )
    if parse_final_stdout_json(multi_fix.stdout) != adopted_on_disk:
        raise AssertionError("Expected final JSON block to match last fixed workspace")


def test_cli_split_help_and_restrictions(tmp):
    base_help = run([str(WORKSPACE), "--help"]).stdout
    assert_contains(base_help, "workspace_with_sandcastle.py")
    assert_not_contains(base_help, "{sandcastle")

    sandcastle_help = run([str(WORKSPACE_SANDCASTLE), "--help"]).stdout
    assert_contains(sandcastle_help, "WARNING:")
    assert_contains(sandcastle_help, "workspace.py")
    assert_contains(sandcastle_help, "sandcastle")

    rejected = run([str(WORKSPACE), "sandcastle", "X"], env=os.environ, check=False)
    if rejected.returncode == 0:
        raise AssertionError("Expected base CLI to reject sandcastle subcommand")

    rejected_type = run(
        [
            str(WORKSPACE),
            "issue",
            "create",
            "X",
            "1",
            "--title",
            "T",
            "--type",
            "AFK",
        ],
        env=os.environ,
        check=False,
    )
    if rejected_type.returncode == 0:
        raise AssertionError("Expected base CLI to reject issue create --type")


def test_base_issue_create_omits_sandcastle_fields(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE)
    run([str(WORKSPACE), "create", "BASE-ISSUE", "--title", "Base issue"], env=env)
    run([str(WORKSPACE), "issue", "create", "BASE-ISSUE", "1", "--title", "Plain"], env=env)
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "BASE-ISSUE" / "issues" / "1.md")
    if "type" in issue or "reviewStatus" in issue or "sandcastle" in issue:
        raise AssertionError(f"Base issue create should omit Sandcastle fields, got {issue}")


def test_open_identical_between_entrypoints(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE)
    run([str(WORKSPACE), "create", "OPEN-CMP", "--title", "Open compare"], env=env)
    workspace_file = tmp / "workspaces" / "OPEN-CMP" / "OPEN-CMP.code-workspace"
    base_result = run([str(WORKSPACE), "open", "OPEN-CMP", "--print"], env=env)
    sandcastle_result = run(
        [str(WORKSPACE_SANDCASTLE), "open", "OPEN-CMP", "--print"], env=env
    )
    base_open = first_stdout_line(base_result.stdout)
    sandcastle_open = first_stdout_line(sandcastle_result.stdout)
    if base_open != sandcastle_open:
        raise AssertionError(
            f"Expected identical open output, base={base_open!r} sandcastle={sandcastle_open!r}"
        )
    base_json = parse_final_stdout_json(base_result.stdout)
    sandcastle_json = parse_final_stdout_json(sandcastle_result.stdout)
    if base_json != sandcastle_json:
        raise AssertionError(
            f"Expected identical open JSON, base={base_json!r} sandcastle={sandcastle_json!r}"
        )
    assert_final_vscode_json(base_result.stdout, workspace_file)


def test_cross_entrypoint_issue_compat(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE)
    run([str(WORKSPACE), "create", "XENT", "--title", "Cross entrypoint"], env=env)

    # Issue created via the base entrypoint (no `type`/`reviewStatus` fields at all)
    # must still be readable via workspace_with_sandcastle.py without crashing.
    run([str(WORKSPACE), "issue", "create", "XENT", "1", "--title", "Base-authored"], env=env)
    ready_json = json.loads(
        run([str(WORKSPACE_SANDCASTLE), "issue", "ready", "XENT", "--json"], env=env).stdout
    )
    ids = {issue["id"] for issue in ready_json["issues"]}
    if "1" not in ids:
        raise AssertionError(f"Expected base-authored issue 1 in sandcastle ready output, got {ready_json}")

    # Round trip the other direction: issue created via the sandcastle entrypoint
    # (carries nested sandcastle metadata) must be updatable via the base entrypoint
    # without losing that pre-existing Sandcastle metadata.
    run(
        [str(WORKSPACE_SANDCASTLE), "issue", "create", "XENT", "2", "--title", "Sandcastle-authored", "--type", "HITL"],
        env=env,
    )
    run([str(WORKSPACE), "issue", "set-status", "XENT", "2", "ready"], env=env)
    issue2 = read_frontmatter(tmp / "ledger" / "workspaces" / "XENT" / "issues" / "2.md")
    sc2 = issue2.get("sandcastle") or {}
    if sc2.get("type") != "HITL":
        raise AssertionError(f"Expected base-entrypoint update to preserve sandcastle.type=HITL, got {issue2}")
    if issue2.get("status") != "ready":
        raise AssertionError(f"Expected status=ready after base update, got {issue2}")


def write_issue_fixture(path, meta, body="## What to build\n\nTBD.\n"):
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=False).strip()
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


def test_sandcastle_issue_create_writes_nested_block(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE_SANDCASTLE)
    run([str(WORKSPACE_SANDCASTLE), "create", "SC-CREATE", "--title", "Sandcastle create"], env=env)
    run(
        [
            str(WORKSPACE_SANDCASTLE),
            "issue",
            "create",
            "SC-CREATE",
            "1",
            "--title",
            "Nested",
            "--type",
            "HITL",
        ],
        env=env,
    )
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "SC-CREATE" / "issues" / "1.md")
    if "type" in issue or "reviewStatus" in issue:
        raise AssertionError(f"Expected no top-level Sandcastle fields, got {issue}")
    sc = issue.get("sandcastle") or {}
    if sc.get("type") != "HITL" or sc.get("reviewStatus") != "pending":
        raise AssertionError(f"Expected nested sandcastle block, got {issue}")


def test_lazy_sandcastle_block_only_on_targeted_issue(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE)
    run([str(WORKSPACE), "create", "LAZY", "--title", "Lazy enrich"], env=env)
    run([str(WORKSPACE), "issue", "create", "LAZY", "1", "--title", "One"], env=env)
    run([str(WORKSPACE), "issue", "create", "LAZY", "2", "--title", "Two"], env=env)
    issue1_before = read_frontmatter(tmp / "ledger" / "workspaces" / "LAZY" / "issues" / "1.md")
    issue2_before = read_frontmatter(tmp / "ledger" / "workspaces" / "LAZY" / "issues" / "2.md")
    if "sandcastle" in issue1_before or "sandcastle" in issue2_before:
        raise AssertionError("Generic issues should not start with sandcastle metadata")
    run(
        [str(WORKSPACE_SANDCASTLE), "issue", "set-status", "LAZY", "1", "ready", "--review-status", "approved"],
        env=env,
    )
    run([str(WORKSPACE_SANDCASTLE), "issue", "ready", "LAZY", "--json"], env=env)
    issue1 = read_frontmatter(tmp / "ledger" / "workspaces" / "LAZY" / "issues" / "1.md")
    issue2 = read_frontmatter(tmp / "ledger" / "workspaces" / "LAZY" / "issues" / "2.md")
    sc1 = issue1.get("sandcastle") or {}
    if sc1.get("type") != "AFK" or sc1.get("reviewStatus") != "approved":
        raise AssertionError(f"Expected sandcastle block on targeted issue 1, got {issue1}")
    if "sandcastle" in issue2:
        raise AssertionError(f"issue ready must not rewrite unrelated issue 2, got {issue2}")
    if issue2 != issue2_before:
        raise AssertionError(f"Unrelated issue 2 changed during sandcastle set-status, got {issue2}")


def test_base_preserves_existing_sandcastle_block(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE_SANDCASTLE)
    run([str(WORKSPACE_SANDCASTLE), "create", "PRESERVE", "--title", "Preserve"], env=env)
    run(
        [
            str(WORKSPACE_SANDCASTLE),
            "issue",
            "create",
            "PRESERVE",
            "1",
            "--title",
            "Keep block",
            "--type",
            "HITL",
        ],
        env=env,
    )
    before = read_frontmatter(tmp / "ledger" / "workspaces" / "PRESERVE" / "issues" / "1.md")
    run([str(WORKSPACE), "issue", "set-status", "PRESERVE", "1", "ready"], env=env)
    after = read_frontmatter(tmp / "ledger" / "workspaces" / "PRESERVE" / "issues" / "1.md")
    if after.get("sandcastle") != before.get("sandcastle"):
        raise AssertionError(
            f"Base entrypoint should preserve sandcastle block, before={before.get('sandcastle')!r} after={after.get('sandcastle')!r}"
        )
    if after.get("status") != "ready":
        raise AssertionError(f"Expected status update via base entrypoint, got {after}")


def test_legacy_fields_normalized_on_sandcastle_write(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE)
    run([str(WORKSPACE), "create", "LEGACY", "--title", "Legacy"], env=env)
    issue_path = tmp / "ledger" / "workspaces" / "LEGACY" / "issues" / "legacy-1.md"
    issue_path.parent.mkdir(parents=True, exist_ok=True)
    write_issue_fixture(
        issue_path,
        {
            "id": "legacy-1",
            "title": "Legacy issue",
            "status": "planned",
            "blockedBy": [],
            "branch": "sandcastle/legacy/legacy-1",
            "type": "HITL",
            "reviewStatus": "pending",
            "createdAt": "2026-01-01T00:00:00+00:00",
            "updatedAt": "2026-01-01T00:00:00+00:00",
        },
    )
    ready_json = json.loads(
        run([str(WORKSPACE_SANDCASTLE), "issue", "ready", "LEGACY", "--json"], env=env).stdout
    )
    hitl_ids = {issue["id"] for issue in ready_json.get("hitl", [])}
    if "legacy-1" not in hitl_ids:
        raise AssertionError(f"Expected legacy HITL issue readable before migration, got {ready_json}")
    run([str(WORKSPACE_SANDCASTLE), "issue", "set-status", "LEGACY", "legacy-1", "ready"], env=env)
    issue = read_frontmatter(issue_path)
    if "type" in issue or "reviewStatus" in issue:
        raise AssertionError(f"Expected legacy top-level fields migrated away, got {issue}")
    sc = issue.get("sandcastle") or {}
    if sc.get("type") != "HITL" or sc.get("reviewStatus") != "pending":
        raise AssertionError(f"Expected legacy values nested unchanged, got {issue}")


def test_base_does_not_migrate_legacy_fields(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE)
    run([str(WORKSPACE), "create", "NO-MIG", "--title", "No migration"], env=env)
    issue_path = tmp / "ledger" / "workspaces" / "NO-MIG" / "issues" / "legacy-1.md"
    issue_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_meta = {
        "id": "legacy-1",
        "title": "Legacy issue",
        "status": "planned",
        "blockedBy": [],
        "branch": "sandcastle/no-mig/legacy-1",
        "type": "HITL",
        "reviewStatus": "pending",
        "createdAt": "2026-01-01T00:00:00+00:00",
        "updatedAt": "2026-01-01T00:00:00+00:00",
    }
    write_issue_fixture(issue_path, legacy_meta)
    run([str(WORKSPACE), "issue", "set-status", "NO-MIG", "legacy-1", "ready"], env=env)
    issue = read_frontmatter(issue_path)
    if issue.get("type") != "HITL" or issue.get("reviewStatus") != "pending":
        raise AssertionError(f"Base entrypoint must not migrate legacy fields, got {issue}")
    if "sandcastle" in issue:
        raise AssertionError(f"Base entrypoint must not add sandcastle block, got {issue}")
    if issue.get("status") != "ready":
        raise AssertionError(f"Expected status update, got {issue}")


def test_mixed_state_sandcastle_block_preserved_and_idempotent(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE)
    run([str(WORKSPACE), "create", "MIXED", "--title", "Mixed state"], env=env)
    issue_path = tmp / "ledger" / "workspaces" / "MIXED" / "issues" / "legacy-1.md"
    issue_path.parent.mkdir(parents=True, exist_ok=True)
    write_issue_fixture(
        issue_path,
        {
            "id": "legacy-1",
            "title": "Mixed issue",
            "status": "planned",
            "blockedBy": [],
            "branch": "sandcastle/mixed/legacy-1",
            "sandcastle": {"type": "AFK"},
            # Stray legacy top-level field alongside an already-nested block;
            # the nested block's (absent) reviewStatus must NOT be silently
            # overwritten with the default, and the legacy value must win
            # since the block doesn't already define reviewStatus.
            "reviewStatus": "approved",
            "createdAt": "2026-01-01T00:00:00+00:00",
            "updatedAt": "2026-01-01T00:00:00+00:00",
        },
    )
    run([str(WORKSPACE_SANDCASTLE), "issue", "set-status", "MIXED", "legacy-1", "ready"], env=env)
    issue = read_frontmatter(issue_path)
    if "type" in issue or "reviewStatus" in issue:
        raise AssertionError(f"Expected legacy top-level fields removed, got {issue}")
    sc = issue.get("sandcastle") or {}
    if sc.get("type") != "AFK" or sc.get("reviewStatus") != "approved":
        raise AssertionError(
            f"Expected stray legacy reviewStatus merged into block without being dropped, got {issue}"
        )
    # updatedAt legitimately changes on every write; compare everything else
    # for byte-for-byte-equivalent (as parsed YAML) idempotency.
    first_write = {k: v for k, v in issue.items() if k != "updatedAt"}

    run([str(WORKSPACE_SANDCASTLE), "issue", "set-status", "MIXED", "legacy-1", "ready"], env=env)
    issue_again = read_frontmatter(issue_path)
    second_write = {k: v for k, v in issue_again.items() if k != "updatedAt"}
    if first_write != second_write:
        raise AssertionError(
            f"Expected idempotent normalization, first={first_write} second={second_write}"
        )

    # A conflicting nested-block value must win over a stray legacy one.
    conflict_path = tmp / "ledger" / "workspaces" / "MIXED" / "issues" / "legacy-2.md"
    write_issue_fixture(
        conflict_path,
        {
            "id": "legacy-2",
            "title": "Conflicting mixed issue",
            "status": "planned",
            "blockedBy": [],
            "branch": "sandcastle/mixed/legacy-2",
            "sandcastle": {"type": "AFK", "reviewStatus": "approved"},
            "reviewStatus": "pending",
            "createdAt": "2026-01-01T00:00:00+00:00",
            "updatedAt": "2026-01-01T00:00:00+00:00",
        },
    )
    run([str(WORKSPACE_SANDCASTLE), "issue", "set-status", "MIXED", "legacy-2", "ready"], env=env)
    conflict_issue = read_frontmatter(conflict_path)
    conflict_sc = conflict_issue.get("sandcastle") or {}
    if conflict_sc.get("reviewStatus") != "approved":
        raise AssertionError(
            f"Expected existing nested block value to win over stray legacy value, got {conflict_issue}"
        )


BASE_TESTS = [
    test_config_and_ledger_workspace,
    test_worktree_adopt_status_note_close,
    test_issue_graph_and_user_review,
    test_repo_crud_and_pr_lifecycle,
    test_workspace_cleanup_plan_and_execution,
    test_workspace_force_cleanup,
    test_doctor_fix_output,
    test_doctor_multi_id_and_all,
    test_doctor_fix_skips_vscode_json_for_cleanup_done_workspace,
]

SANDCASTLE_TESTS = [
    test_sandcastle_plan_init_runner_and_reconcile,
    test_sandcastle_plan_multi_repo_cross_dependency,
    test_sandcastle_plan_limit_excludes_independently_ready_dependent,
    test_sandcastle_plan_repo_filter_does_not_mistarget_unset_repo_issue,
    test_sandcastle_plan_unknown_repo_blocked,
    test_sandcastle_plan_empty_still_writes_artifact,
    test_cleanup_plan_and_execution,
]

CLI_SPLIT_TESTS = [
    test_cli_split_help_and_restrictions,
    test_topology_commands_print_vscode_json,
    test_base_issue_create_omits_sandcastle_fields,
    test_issue_create_default_repo_single_repo_workspace,
    test_open_identical_between_entrypoints,
    test_cross_entrypoint_issue_compat,
    test_sandcastle_issue_create_writes_nested_block,
    test_lazy_sandcastle_block_only_on_targeted_issue,
    test_base_preserves_existing_sandcastle_block,
    test_legacy_fields_normalized_on_sandcastle_write,
    test_base_does_not_migrate_legacy_fields,
    test_mixed_state_sandcastle_block_preserved_and_idempotent,
    test_base_preserves_workspace_sandcastle_section,
]


def main():
    passed = 0
    for test in BASE_TESTS:
        for workspace_cli in (WORKSPACE, WORKSPACE_SANDCASTLE):
            with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
                test(Path(tmp_dir), workspace_cli)
            print(f"ok {test.__name__} [{workspace_cli.name}]")
            passed += 1

    for test in SANDCASTLE_TESTS:
        with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
            test(Path(tmp_dir), WORKSPACE_SANDCASTLE)
        print(f"ok {test.__name__} [{WORKSPACE_SANDCASTLE.name}]")
        passed += 1

    for test in CLI_SPLIT_TESTS:
        with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
            test(Path(tmp_dir))
        print(f"ok {test.__name__}")
        passed += 1

    print(f"{passed} self-check(s) passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"self-check failed: {error}", file=sys.stderr)
        sys.exit(1)
