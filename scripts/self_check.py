#!/usr/bin/env python3
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "scripts" / "workspace.py"
WORKSPACE_SANDCASTLE = ROOT / "scripts" / "workspace_with_sandcastle.py"
sys.path.insert(0, str(ROOT / "scripts"))

from workspaces import doctor
from workspaces import github_sync
from workspaces import repo
from workspaces.common import DEFAULT_CONFIG
from workspaces.git import branch_exists
from workspaces.issues import sandcastle_issue_meta
from workspaces.ledger import WorkspaceLedger
from workspaces.sandcastle_execute import dependency_mounts_for_issue
from workspaces.workspace_model import Workspace


def run(cmd, *, env=None, cwd=None, check=True, input=None):
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        input=input,
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


def init_bare_git_repo(path):
    run(["git", "-C", str(path), "init", "-b", "main"])
    run(["git", "-C", str(path), "config", "user.email", "test@example.com"])
    run(["git", "-C", str(path), "config", "user.name", "Test User"])
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    run(["git", "-C", str(path), "add", "README.md"])
    run(["git", "-C", str(path), "commit", "-m", "init"])


def test_adopt_external_worktree_pointer_and_doctor(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)

    external_repo = tmp / "external-repo"
    external_repo.mkdir()
    init_bare_git_repo(external_repo)

    run([str(workspace_cli), "adopt", str(external_repo), "--id", "EXT-ADOPT"], env=env)

    ws_root = tmp / "workspaces" / "EXT-ADOPT"
    pointer = ws_root / ".workspace-id"
    assert_path(pointer)
    if pointer.read_text(encoding="utf-8").strip() != "EXT-ADOPT":
        raise AssertionError(f"Expected workspace pointer to hold adopted id, got: {pointer.read_text()!r}")

    pointer_in_repo = external_repo / ".workspace-id"
    if pointer_in_repo.exists():
        raise AssertionError(
            ".workspace-id should not be written into the adopted repo path itself "
            f"when it lives outside workspace_root/<id>/: {pointer_in_repo}"
        )

    doctor = json.loads(run([str(workspace_cli), "doctor", "EXT-ADOPT", "--json"], env=env).stdout)
    codes = [issue["code"] for issue in doctor["issues"]]
    if "workspace-pointer" in codes:
        raise AssertionError(f"doctor should not warn about workspace pointer right after adopt, got: {doctor}")


def test_list_json_and_ids_only_flags(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    run([str(workspace_cli), "create", "LIST-ALPHA", "--title", "Alpha", "--state", "in-progress"], env=env)
    run([str(workspace_cli), "create", "LIST-BETA", "--title", "Beta", "--state", "in-progress"], env=env)
    run([str(workspace_cli), "create", "LIST-GAMMA", "--title", "Gamma", "--state", "closed"], env=env)

    all_ids = run([str(workspace_cli), "list", "--ids-only", "--all"], env=env).stdout
    lines = all_ids.strip().splitlines()
    if set(lines) != {"LIST-ALPHA", "LIST-BETA", "LIST-GAMMA"}:
        raise AssertionError(f"Unexpected --ids-only --all output: {lines}")
    assert_not_contains(all_ids, "ID")
    assert_not_contains(all_ids, "Workspaces")
    assert_not_contains(all_ids, "STATE")

    active_ids = run([str(workspace_cli), "list", "--ids-only"], env=env).stdout.strip().splitlines()
    if set(active_ids) != {"LIST-ALPHA", "LIST-BETA"}:
        raise AssertionError(f"Expected closed workspace excluded from default --ids-only scope, got {active_ids}")

    closed_ids = run([str(workspace_cli), "list", "--ids-only", "--state", "closed"], env=env).stdout.strip().splitlines()
    if closed_ids != ["LIST-GAMMA"]:
        raise AssertionError(f"Expected only the closed workspace, got {closed_ids}")

    empty_ids_only = run([str(workspace_cli), "list", "--ids-only", "--state", "nonexistent"], env=env).stdout
    if empty_ids_only != "":
        raise AssertionError(f"Expected empty output for --ids-only with no matches, got {empty_ids_only!r}")

    json_rows = json.loads(run([str(workspace_cli), "list", "--json", "--all"], env=env).stdout)
    if len(json_rows) != 3:
        raise AssertionError(f"Expected 3 rows for --json --all, got {json_rows}")
    required_keys = {"id", "state", "updated", "repos", "local", "prs", "title"}
    for row in json_rows:
        if not required_keys.issubset(row.keys()):
            raise AssertionError(f"Missing keys in list --json row: {row}")

    active_json_ids = {row["id"] for row in json.loads(run([str(workspace_cli), "list", "--json"], env=env).stdout)}
    if active_json_ids != {"LIST-ALPHA", "LIST-BETA"}:
        raise AssertionError(f"Expected closed workspace excluded from default --json scope, got {active_json_ids}")

    closed_json = json.loads(run([str(workspace_cli), "list", "--json", "--state", "closed"], env=env).stdout)
    if len(closed_json) != 1 or closed_json[0]["id"] != "LIST-GAMMA":
        raise AssertionError(f"Expected only the closed workspace in --json --state closed, got {closed_json}")

    empty_json = json.loads(run([str(workspace_cli), "list", "--json", "--state", "nonexistent"], env=env).stdout)
    if empty_json != []:
        raise AssertionError(f"Expected empty list for nonexistent state, got {empty_json}")

    error_proc = run([str(workspace_cli), "list", "--json", "--ids-only"], env=env, check=False)
    if error_proc.returncode == 0:
        raise AssertionError("Expected --json/--ids-only combination to fail")
    assert_contains(error_proc.stderr, "mutually exclusive")


def test_init_config_prompts_and_force_overwrite(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = {**os.environ, "WORKSPACES_CONFIG": str(config_path)}

    prompt_result = run([str(workspace_cli), "init-config"], env=env, input="\n\n")
    assert_contains(prompt_result.stdout, "[~/git]")
    assert_contains(prompt_result.stdout, "[~/workspaces]")
    data = read_yaml(config_path)
    if data["source_root"] != "~/git" or data["workspace_root"] != "~/workspaces":
        raise AssertionError(f"Expected template defaults accepted via blank input, got {data}")
    if data["ledger_root"] != "~/workspaces/_ledger":
        raise AssertionError(f"Expected ledger_root derived from workspace_root, got {data}")
    for key in ("base_remote", "base_branch", "push_remote", "jira_base_url", "github_host", "editor_command"):
        if key not in data:
            raise AssertionError(f"Expected template key {key!r} preserved, got {data}")

    config_path.unlink()
    custom_source = tmp / "custom-src"
    custom_source.mkdir()
    custom_workspace = tmp / "custom-ws"
    run(
        [str(workspace_cli), "init-config"],
        env=env,
        input=f"{custom_source}\n{custom_workspace}\n",
    )
    data = read_yaml(config_path)
    if data["source_root"] != str(custom_source):
        raise AssertionError(f"Expected custom source_root honored, got {data}")
    if data["workspace_root"] != str(custom_workspace):
        raise AssertionError(f"Expected custom workspace_root honored, got {data}")
    if data["ledger_root"] != f"{custom_workspace}/_ledger":
        raise AssertionError(f"Expected ledger_root derived from custom workspace_root, got {data}")

    config_path.unlink()
    missing_source = tmp / "does-not-exist"
    warn_result = run(
        [str(workspace_cli), "init-config"],
        env=env,
        input=f"{missing_source}\n\n",
    )
    assert_contains(warn_result.stderr, "Warning")
    assert_contains(warn_result.stderr, str(missing_source))
    assert_path(config_path)

    config_path.write_text("existing: true\n", encoding="utf-8")
    refused = run([str(workspace_cli), "init-config", "--non-interactive"], env=env)
    assert_contains(refused.stdout, "already exists")
    data = read_yaml(config_path)
    if data != {"existing": True}:
        raise AssertionError(f"Expected init-config without --force to leave existing config untouched, got {data}")

    run([str(workspace_cli), "init-config", "--non-interactive", "--force"], env=env)
    data = read_yaml(config_path)
    if "existing" in data or data.get("source_root") != "~/git":
        raise AssertionError(f"Expected --force to overwrite with template defaults, got {data}")


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
    if sandcastle_issue_meta(issue1)["type"] != "AFK":
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
    if issue["status"] != "merged" or sandcastle_issue_meta(issue)["reviewStatus"] != "approved":
        raise AssertionError(f"Expected reconciled issue, got {issue}")


def test_sandcastle_plan_multi_repo_cross_dependency(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    plan = parse_final_stdout_json(
        setup_cross_repo_execute_workspace(tmp, env, workspace_cli, name="CROSS").stdout
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


def write_fake_sandcastle_command(tmp, log_path, *, fail_repos=None):
    """A `--command` runner that reports every issue merged/approved, except repos
    in `fail_repos`, which exit 1 with no result file. A strict special case of
    `write_scripted_sandcastle_command`'s "normal"/"fail" behaviors.
    """
    behaviors = {repo: "fail" for repo in (fail_repos or [])}
    return write_scripted_sandcastle_command(
        tmp, log_path, behaviors, script_name="fake_sandcastle_runner.py"
    )


def write_scripted_sandcastle_command(tmp, log_path, behaviors=None, *, script_name="scripted_sandcastle_runner.py"):
    """A fake `--command` runner with per-repo scripted behaviors for edge-case testing.

    `behaviors` maps repo name -> one of:
      - "normal" (default): report every issue in the plan as merged/approved
      - "fail": exit 1 with no result file
      - "exit0_no_result": exit 0 with no result file at all
      - "corrupt_result": write a non-JSON result file, exit 0
      - "unknown_id_result": write a valid-JSON result referencing an issue id not in the plan
      - "partial_result": only report the first issue in the plan, omitting the rest
      - "bad_then_good_result": write a result whose FIRST entry references an unknown
        issue id (bad), followed by a SECOND, otherwise-valid entry reporting the plan's
        second issue as merged -- the plan's first issue is deliberately never mentioned
        at all, isolating "a bad entry poisoning a later good entry in the same list"
        from "an issue simply never covered by the result"
    """
    behaviors = behaviors or {}
    script = tmp / script_name
    script.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                f"LOG = Path({str(log_path)!r})",
                f"BEHAVIORS = {behaviors!r}",
                "",
                "plan_path = Path(os.environ['WORKSPACE_SANDCASTLE_PLAN'])",
                "result_path = Path(os.environ['WORKSPACE_SANDCASTLE_RESULT'])",
                "repo = os.environ['WORKSPACE_REPO_NAME']",
                "plan = json.loads(plan_path.read_text(encoding='utf-8'))",
                "issue_ids = [item['id'] for item in plan['issues']]",
                "with LOG.open('a', encoding='utf-8') as handle:",
                "    handle.write(f\"{repo}:{','.join(issue_ids)}\\n\")",
                "behavior = BEHAVIORS.get(repo, 'normal')",
                "",
                "if behavior == 'fail':",
                "    sys.exit(1)",
                "if behavior == 'exit0_no_result':",
                "    sys.exit(0)",
                "if behavior == 'corrupt_result':",
                "    result_path.write_text('{not valid json', encoding='utf-8')",
                "    sys.exit(0)",
                "if behavior == 'unknown_id_result':",
                "    payload = {",
                "        'version': 1,",
                "        'issues': [",
                "            {'id': 'does-not-exist', 'status': 'merged', 'reviewStatus': 'approved'}",
                "        ],",
                "    }",
                "    result_path.write_text(json.dumps(payload) + '\\n', encoding='utf-8')",
                "    sys.exit(0)",
                "if behavior == 'bad_then_good_result':",
                "    second = plan['issues'][1]",
                "    payload = {",
                "        'version': 1,",
                "        'issues': [",
                "            {'id': 'does-not-exist', 'status': 'merged', 'reviewStatus': 'approved'},",
                "            {",
                "                'id': second['id'],",
                "                'status': 'merged',",
                "                'reviewStatus': 'approved',",
                "                'branch': second['branch'],",
                "            },",
                "        ],",
                "    }",
                "    result_path.write_text(json.dumps(payload) + '\\n', encoding='utf-8')",
                "    sys.exit(0)",
                "if behavior == 'partial_result':",
                "    covered = plan['issues'][:1]",
                "else:",
                "    covered = plan['issues']",
                "issues = [",
                "    {",
                "        'id': item['id'],",
                "        'status': 'merged',",
                "        'reviewStatus': 'approved',",
                "        'branch': item['branch'],",
                "    }",
                "    for item in covered",
                "]",
                "result_path.write_text(json.dumps({'version': 1, 'issues': issues}) + '\\n', encoding='utf-8')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return f"python3 {script}"


def setup_cross_repo_execute_workspace(tmp, env, workspace_cli, *, name="EXEC"):
    make_source_repo(tmp, "frontend")
    make_source_repo(tmp, "backend")
    run(
        [str(workspace_cli), "create", name, "--title", "Execute", "--repo", "frontend"],
        env=env,
    )
    run([str(workspace_cli), "repo", "add", name, "backend"], env=env)
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            name,
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
            name,
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
    return run([str(workspace_cli), "sandcastle", "plan", name, "--json"], env=env)


def current_plan_pointer(tmp, workspace_id):
    workspace_yaml = read_yaml(tmp / "ledger" / "workspaces" / workspace_id / "workspace.yaml")
    return (workspace_yaml.get("sandcastle") or {}).get("currentPlan")


def test_sandcastle_execute_resolves_plan_pointer_and_override(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    setup_cross_repo_execute_workspace(tmp, env, workspace_cli)
    pointer_path = current_plan_pointer(tmp, "EXEC")["path"]
    run([str(workspace_cli), "sandcastle", "plan", "EXEC", "--json"], env=env)
    newer_pointer = current_plan_pointer(tmp, "EXEC")["path"]
    log_path = tmp / "invoke.log"
    fake = write_fake_sandcastle_command(tmp, log_path)
    run(
        [
            str(workspace_cli),
            "sandcastle",
            "execute",
            "EXEC",
            "--plan",
            pointer_path,
            "--command",
            fake,
        ],
        env=env,
    )
    if current_plan_pointer(tmp, "EXEC") is not None:
        raise AssertionError("Expected pointer cleared after explicit --plan execute")
    if not log_path.read_text(encoding="utf-8").strip():
        raise AssertionError("Expected explicit --plan execute to run")
    if newer_pointer == pointer_path:
        raise AssertionError("Expected distinct plan artifacts for override test")

    make_source_repo(tmp, "repo-b")
    run([str(workspace_cli), "create", "PTR", "--title", "Pointer", "--repo", "repo-b"], env=env)
    run([str(workspace_cli), "issue", "create", "PTR", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "PTR", "--json"], env=env)
    if current_plan_pointer(tmp, "PTR") is None:
        raise AssertionError("Expected current-plan pointer after plan")
    log_path2 = tmp / "invoke-ptr.log"
    fake2 = write_fake_sandcastle_command(tmp, log_path2)
    run(
        [
            str(workspace_cli),
            "sandcastle",
            "execute",
            "PTR",
            "--command",
            fake2,
        ],
        env=env,
    )
    if current_plan_pointer(tmp, "PTR") is not None:
        raise AssertionError("Expected pointer cleared after default execute")
    if not log_path2.read_text(encoding="utf-8").strip():
        raise AssertionError("Expected execute via current-plan pointer to run")


def test_sandcastle_execute_fallback_newest_valid_plan(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "FALLBACK", "--title", "Fallback", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "FALLBACK", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "FALLBACK", "--json"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "FALLBACK", "--json"], env=env)
    workspace_yaml_path = tmp / "ledger" / "workspaces" / "FALLBACK" / "workspace.yaml"
    data = read_yaml(workspace_yaml_path)
    if "sandcastle" in data:
        data["sandcastle"].pop("currentPlan", None)
        if not data["sandcastle"]:
            data.pop("sandcastle")
    workspace_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    log_path = tmp / "fallback.log"
    fake = write_fake_sandcastle_command(tmp, log_path)
    run(
        [
            str(workspace_cli),
            "sandcastle",
            "execute",
            "FALLBACK",
            "--command",
            fake,
        ],
        env=env,
    )
    if not log_path.read_text(encoding="utf-8").strip():
        raise AssertionError("Expected fallback execute without pointer to run")
    plans = sorted(
        (tmp / "ledger" / "workspaces" / "FALLBACK" / "runs").glob("sandcastle-plan-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if len(plans) < 2:
        raise AssertionError("Expected two plan artifacts")


def test_sandcastle_execute_refuses_stale_plan(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    setup_cross_repo_execute_workspace(tmp, env, workspace_cli)
    log_path = tmp / "invoke.log"
    fake = write_fake_sandcastle_command(tmp, log_path)
    issue_path = tmp / "ledger" / "workspaces" / "EXEC" / "issues" / "be-1.md"
    text = issue_path.read_text(encoding="utf-8")
    meta = read_frontmatter(issue_path)
    meta["blockedBy"] = []
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=False).strip()
    body = text[text.find("\n---\n", 4) + len("\n---\n") :]
    issue_path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    stale = run(
        [
            str(workspace_cli),
            "sandcastle",
            "execute",
            "EXEC",
            "--command",
            fake,
        ],
        env=env,
        check=False,
    )
    if stale.returncode == 0:
        raise AssertionError("Expected stale plan refusal")
    assert_contains(stale.stderr + stale.stdout, "stale")
    if log_path.exists() and log_path.read_text(encoding="utf-8").strip():
        raise AssertionError("Expected no execute invocations for stale plan")
    if current_plan_pointer(tmp, "EXEC") is None:
        raise AssertionError("Stale refusal must not clear current-plan pointer")


def test_sandcastle_execute_multi_wave_multi_repo(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    setup_cross_repo_execute_workspace(tmp, env, workspace_cli)
    log_path = tmp / "invoke.log"
    fake = write_fake_sandcastle_command(tmp, log_path)
    run(
        [
            str(workspace_cli),
            "sandcastle",
            "execute",
            "EXEC",
            "--command",
            fake,
        ],
        env=env,
    )
    log = log_path.read_text(encoding="utf-8").splitlines()
    if log != ["frontend:fe-1", "backend:be-1"]:
        raise AssertionError(f"Expected one invocation per repo per wave, got {log}")
    notes = (tmp / "ledger" / "workspaces" / "EXEC" / "notes.md").read_text(encoding="utf-8")
    assert_contains(notes, "Sandcastle execute finished")


def test_sandcastle_execute_propagates_blocked_hitl(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    setup_cross_repo_execute_workspace(tmp, env, workspace_cli)
    log_path = tmp / "invoke.log"
    fake = write_fake_sandcastle_command(tmp, log_path, fail_repos=["frontend"])
    failed = run(
        [
            str(workspace_cli),
            "sandcastle",
            "execute",
            "EXEC",
            "--command",
            fake,
        ],
        env=env,
        check=False,
    )
    if failed.returncode == 0:
        raise AssertionError("Expected non-zero exit when frontend command fails")
    log = log_path.read_text(encoding="utf-8").splitlines()
    if log != ["frontend:fe-1"]:
        raise AssertionError(f"Backend command must not run after frontend failure, got {log}")
    fe = read_frontmatter(tmp / "ledger" / "workspaces" / "EXEC" / "issues" / "fe-1.md")
    be = read_frontmatter(tmp / "ledger" / "workspaces" / "EXEC" / "issues" / "be-1.md")
    if fe["status"] != "failed":
        raise AssertionError(f"Expected fe-1 failed, got {fe}")
    if be["status"] != "blocked-hitl":
        raise AssertionError(f"Expected be-1 blocked-hitl, got {be}")


def test_sandcastle_execute_clears_pointer_on_failure(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "FAIL", "--title", "Fail path", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "FAIL", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "FAIL", "--json"], env=env)
    if current_plan_pointer(tmp, "FAIL") is None:
        raise AssertionError("Expected current-plan pointer before execute")
    fake = write_fake_sandcastle_command(tmp, tmp / "invoke.log", fail_repos=["repo"])
    run(
        [
            str(workspace_cli),
            "sandcastle",
            "execute",
            "FAIL",
            "--command",
            fake,
        ],
        env=env,
        check=False,
    )
    if current_plan_pointer(tmp, "FAIL") is not None:
        raise AssertionError("Expected pointer cleared after failed execute")


def test_sandcastle_execute_refuses_active_lock(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "LOCK", "--title", "Lock", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "LOCK", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "LOCK", "--json"], env=env)
    lock_path = tmp / "ledger" / "workspaces" / "LOCK" / "runs" / "active-sandcastle-run.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "createdAt": "now", "planPath": "x"}),
        encoding="utf-8",
    )
    fake = write_fake_sandcastle_command(tmp, tmp / "invoke.log")
    blocked = run(
        [
            str(workspace_cli),
            "sandcastle",
            "execute",
            "LOCK",
            "--command",
            fake,
        ],
        env=env,
        check=False,
    )
    if blocked.returncode == 0:
        raise AssertionError("Expected active lock refusal")
    assert_contains(blocked.stderr + blocked.stdout, "already active")


def test_sandcastle_execute_eager_invalidation(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "INV", "--title", "Invalidate", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "INV", "1", "--title", "Target"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "INV", "--json"], env=env)
    if current_plan_pointer(tmp, "INV") is None:
        raise AssertionError("Expected current-plan pointer after plan")
    run(
        [
            str(workspace_cli),
            "issue",
            "set-status",
            "INV",
            "1",
            "ready",
            "--branch",
            "feature/new-branch",
        ],
        env=env,
    )
    if current_plan_pointer(tmp, "INV") is not None:
        raise AssertionError("Expected branch edit to invalidate current-plan pointer")
    notes = (tmp / "ledger" / "workspaces" / "INV" / "notes.md").read_text(encoding="utf-8")
    assert_contains(notes, "invalidated")
    assert_contains(notes, "1")

    run([str(workspace_cli), "sandcastle", "plan", "INV", "--json"], env=env)
    run(
        [
            str(workspace_cli),
            "issue",
            "set-status",
            "INV",
            "1",
            "ready",
            "--review-status",
            "approved",
        ],
        env=env,
    )
    if current_plan_pointer(tmp, "INV") is not None:
        raise AssertionError("Expected review-status edit to invalidate current-plan pointer")

    run([str(workspace_cli), "sandcastle", "plan", "INV", "--json"], env=env)
    run([str(workspace_cli), "repo", "remove", "INV", "repo"], env=env)
    if current_plan_pointer(tmp, "INV") is not None:
        raise AssertionError("Expected repo remove to invalidate current-plan pointer")
    notes = (tmp / "ledger" / "workspaces" / "INV" / "notes.md").read_text(encoding="utf-8")
    assert_contains(notes, "repo set")


def test_sandcastle_execute_eager_invalidation_via_base_cli(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE_SANDCASTLE)
    make_source_repo(tmp, "repo")
    run([str(WORKSPACE_SANDCASTLE), "create", "BASE-INV", "--title", "Base invalidate", "--repo", "repo"], env=env)
    run([str(WORKSPACE_SANDCASTLE), "issue", "create", "BASE-INV", "1", "--title", "Target"], env=env)
    run([str(WORKSPACE_SANDCASTLE), "sandcastle", "plan", "BASE-INV", "--json"], env=env)
    if current_plan_pointer(tmp, "BASE-INV") is None:
        raise AssertionError("Expected current-plan pointer after plan")
    run(
        [
            str(WORKSPACE),
            "issue",
            "set-status",
            "BASE-INV",
            "1",
            "ready",
            "--branch",
            "feature/base-branch",
        ],
        env=env,
    )
    if current_plan_pointer(tmp, "BASE-INV") is not None:
        raise AssertionError("Expected base CLI branch edit to invalidate current-plan pointer")
    notes = (tmp / "ledger" / "workspaces" / "BASE-INV" / "notes.md").read_text(encoding="utf-8")
    assert_contains(notes, "invalidated")

    run([str(WORKSPACE_SANDCASTLE), "sandcastle", "plan", "BASE-INV", "--json"], env=env)
    make_source_repo(tmp, "extra")
    run([str(WORKSPACE), "repo", "add", "BASE-INV", "extra"], env=env)
    if current_plan_pointer(tmp, "BASE-INV") is not None:
        raise AssertionError("Expected base CLI repo add to invalidate current-plan pointer")


def test_sandcastle_execute_stale_on_repo_removal_and_rename(tmp, workspace_cli):
    """Regression for bug 1: frozen plan-payload repos must not blind the staleness check."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo1")
    run([str(workspace_cli), "create", "REPOCHG", "--title", "Repo change", "--repo", "repo1"], env=env)
    run([str(workspace_cli), "issue", "create", "REPOCHG", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "REPOCHG", "--json"], env=env)
    plan_path = current_plan_pointer(tmp, "REPOCHG")["path"]

    make_source_repo(tmp, "repo2")
    run([str(workspace_cli), "repo", "remove", "REPOCHG", "repo1"], env=env)
    run([str(workspace_cli), "repo", "add", "REPOCHG", "repo2"], env=env)

    log_path = tmp / "invoke.log"
    fake = write_fake_sandcastle_command(tmp, log_path)

    # Explicit --plan override: the plan artifact's own frozen `repos` snapshot must not
    # be trusted as "current" -- staleness must be checked against LIVE workspace repos.
    stale_explicit = run(
        [str(workspace_cli), "sandcastle", "execute", "REPOCHG", "--plan", plan_path, "--command", fake],
        env=env,
        check=False,
    )
    if stale_explicit.returncode == 0:
        raise AssertionError("Expected stale refusal after repo set changed (explicit --plan)")
    assert_contains(stale_explicit.stderr + stale_explicit.stdout, "stale")

    # Default resolution: the repo-change eager hook already cleared the pointer, so this
    # exercises the newest-valid-artifact fallback scan, which must also refuse.
    stale_default = run(
        [str(workspace_cli), "sandcastle", "execute", "REPOCHG", "--command", fake],
        env=env,
        check=False,
    )
    if stale_default.returncode == 0:
        raise AssertionError("Expected refusal via fallback resolution after repo set changed")

    if log_path.exists() and log_path.read_text(encoding="utf-8").strip():
        raise AssertionError("Expected no execute invocations against the orphaned repo scope")


def test_sandcastle_execute_status_only_transition_not_stale(tmp, workspace_cli):
    """Regression for bug 2: ordinary status progression must not falsely refuse as stale."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "STATUSOK", "--title", "Status only", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "STATUSOK", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "STATUSOK", "--json"], env=env)
    run([str(workspace_cli), "issue", "set-status", "STATUSOK", "1", "ready"], env=env)
    if current_plan_pointer(tmp, "STATUSOK") is None:
        raise AssertionError("A status-only edit must not eagerly invalidate the current plan")

    log_path = tmp / "invoke.log"
    fake = write_fake_sandcastle_command(tmp, log_path)
    result = run(
        [str(workspace_cli), "sandcastle", "execute", "STATUSOK", "--command", fake],
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Expected status-only transition to not trigger stale refusal:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    if not log_path.read_text(encoding="utf-8").strip():
        raise AssertionError("Expected execute to actually run after a status-only transition")


def test_sandcastle_execute_recovers_stale_lock_from_dead_pid(tmp, workspace_cli):
    """Regression for bug 3: a lock left by a dead PID must not permanently deadlock execute."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "DEADLOCK", "--title", "Dead lock", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "DEADLOCK", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "DEADLOCK", "--json"], env=env)

    proc = subprocess.Popen(["true"])
    proc.wait()
    dead_pid = proc.pid

    lock_path = tmp / "ledger" / "workspaces" / "DEADLOCK" / "runs" / "active-sandcastle-run.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": dead_pid, "createdAt": "long-ago", "planPath": "stale"}),
        encoding="utf-8",
    )

    log_path = tmp / "invoke.log"
    fake = write_fake_sandcastle_command(tmp, log_path)
    result = run(
        [str(workspace_cli), "sandcastle", "execute", "DEADLOCK", "--command", fake],
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Expected stale dead-PID lock to be recovered and execute to proceed:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    if not log_path.read_text(encoding="utf-8").strip():
        raise AssertionError("Expected execute to proceed after recovering a stale lock")
    if lock_path.exists():
        raise AssertionError("Expected the lock file removed after execute completed")


def test_sandcastle_execute_concurrent_lock_recovery_is_exclusive(tmp, workspace_cli):
    """Regression: two processes racing to recover the same dead lock must not both win.

    This targets the TOCTOU introduced by the bug-3 fix: process A observes a dead
    lock and decides to recover it; before A unlinks, process B does the same,
    deletes the dead lock, and creates its own live lock; A then unconditionally
    unlinks *B's live lock* (not the dead one A originally observed) and creates its
    own -- both A and B believe they hold the lock. We force the two competing
    threads to overlap inside the vulnerable "is it still dead? -> unlink" window
    (via an instrumented, artificially-slowed `sandcastle_run_active`) and assert
    that at most one of them is ever inside that window at a time, and that
    exactly one of the two actually acquires the lock.
    """
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "LOCKRACE", "--title", "Lock race", "--repo", "repo"], env=env)

    from workspaces.ledger import WorkspaceLedger

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["source_root"] = str(tmp / "src")
    config["workspace_root"] = str(tmp / "workspaces")
    config["ledger_root"] = str(tmp / "ledger")
    ledger = WorkspaceLedger(config)

    proc = subprocess.Popen(["true"])
    proc.wait()
    dead_pid = proc.pid
    lock_path = ledger.sandcastle_lock_path("LOCKRACE")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": dead_pid, "createdAt": "long-ago", "planPath": "stale"}),
        encoding="utf-8",
    )

    concurrent_entrants = []
    entrants_lock = threading.Lock()
    overlap_errors = []
    original_run_active = ledger.sandcastle_run_active

    def instrumented_run_active(workspace_id):
        with entrants_lock:
            concurrent_entrants.append(1)
            active_now = sum(concurrent_entrants)
        try:
            if active_now > 1:
                overlap_errors.append(
                    "Two threads were concurrently inside the lock-recovery "
                    "liveness-recheck window"
                )
            time.sleep(0.2)
            return original_run_active(workspace_id)
        finally:
            with entrants_lock:
                concurrent_entrants.pop()

    ledger.sandcastle_run_active = instrumented_run_active

    results = []
    results_lock = threading.Lock()

    def attempt():
        try:
            ledger.acquire_sandcastle_lock("LOCKRACE", {"planPath": "x", "targetedIssueIds": []})
            outcome = "acquired"
        except SystemExit:
            outcome = "refused"
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        if thread.is_alive():
            raise AssertionError("Lock acquisition thread did not finish in time")

    if overlap_errors:
        raise AssertionError(f"Concurrent lock recovery was not mutually exclusive: {overlap_errors}")
    if sorted(results) != ["acquired", "refused"]:
        raise AssertionError(f"Expected exactly one thread to win the race, got {results}")
    if not lock_path.exists():
        raise AssertionError("Expected the winning thread's lock file to remain on disk")
    recovery_lock_path = lock_path.with_name(lock_path.name + ".recovery-lock")
    if recovery_lock_path.exists():
        try:
            fd = os.open(recovery_lock_path, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        except BlockingIOError:
            raise AssertionError("Expected the recovery flock released after the race resolved")


def test_sandcastle_execute_recovers_past_lock_killed_mid_recovery(tmp, workspace_cli):
    """Regression: a process SIGKILL'd while holding the recovery lock must not
    permanently deadlock future recovery attempts.

    A plain `O_CREAT|O_EXCL` marker file has no ownership/liveness info, so a
    process that crashes while holding it leaves an orphaned marker that can never
    be told apart from "someone is legitimately recovering right now" -- permanently
    deadlocking every future recovery attempt. The `flock()`-based recovery lock is
    supposed to avoid this entirely: the kernel releases it unconditionally when the
    holding process's file descriptor is closed for any reason, including a hard
    kill. This spawns a real subprocess, confirms it genuinely holds the recovery
    lock, `SIGKILL`s it, and confirms a subsequent `sandcastle execute` still
    recovers the (separately dead-PID) main lock and proceeds rather than refusing.
    """
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "KILLRECOVER", "--title", "Kill mid recovery", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "KILLRECOVER", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "KILLRECOVER", "--json"], env=env)

    from workspaces.ledger import WorkspaceLedger

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["source_root"] = str(tmp / "src")
    config["workspace_root"] = str(tmp / "workspaces")
    config["ledger_root"] = str(tmp / "ledger")
    ledger = WorkspaceLedger(config)

    # Seed a dead-PID main lock so a subsequent acquire attempt must go through
    # `_recover_dead_sandcastle_lock` (and thus contend for the recovery lock).
    dead_proc = subprocess.Popen(["true"])
    dead_proc.wait()
    lock_path = ledger.sandcastle_lock_path("KILLRECOVER")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": dead_proc.pid, "createdAt": "long-ago", "planPath": "stale"}),
        encoding="utf-8",
    )

    recovery_lock_path = lock_path.with_name(lock_path.name + ".recovery-lock")
    ready_marker = tmp / "worker-ready"
    worker_script = tmp / "hold_recovery_flock.py"
    worker_script.write_text(
        "\n".join(
            [
                "import fcntl, os, time",
                f"path = {str(recovery_lock_path)!r}",
                f"marker = {str(ready_marker)!r}",
                "fd = os.open(path, os.O_CREAT | os.O_RDWR)",
                "fcntl.flock(fd, fcntl.LOCK_EX)",
                "with open(marker, 'w', encoding='utf-8') as handle:",
                "    handle.write('ready')",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    worker = subprocess.Popen([sys.executable, str(worker_script)])
    try:
        deadline = time.time() + 10
        while not ready_marker.exists():
            if time.time() > deadline:
                raise AssertionError("Worker never signaled it held the recovery lock")
            time.sleep(0.05)
        # The worker now genuinely holds the recovery lock (confirmed via the marker
        # it only writes AFTER acquiring the flock); kill it hard, simulating a
        # crash/OOM-kill/`kill -9` occurring precisely mid-recovery.
        worker.kill()
        worker.wait(timeout=10)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)

    if not recovery_lock_path.exists():
        raise AssertionError("Expected the recovery-lock file itself to remain on disk after the kill")

    log_path = tmp / "invoke.log"
    fake = write_fake_sandcastle_command(tmp, log_path)
    result = run(
        [str(workspace_cli), "sandcastle", "execute", "KILLRECOVER", "--command", fake],
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Expected execute to recover past the killed mid-recovery process, not deadlock:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    if not log_path.read_text(encoding="utf-8").strip():
        raise AssertionError("Expected execute to proceed after recovering past the killed process")
    if lock_path.exists():
        raise AssertionError("Expected the main lock file removed after execute completed")


def setup_independent_two_repo_execute_workspace(tmp, env, workspace_cli, *, name="TWOREPO"):
    """Two repos, one issue each, with NO cross-dependency -- both land in wave 0."""
    make_source_repo(tmp, "alpha")
    make_source_repo(tmp, "beta")
    run([str(workspace_cli), "create", name, "--title", "Two repo", "--repo", "alpha"], env=env)
    run([str(workspace_cli), "repo", "add", name, "beta"], env=env)
    run(
        [str(workspace_cli), "issue", "create", name, "a-1", "--title", "Alpha issue", "--repo", "alpha"],
        env=env,
    )
    run(
        [str(workspace_cli), "issue", "create", name, "b-1", "--title", "Beta issue", "--repo", "beta"],
        env=env,
    )
    run([str(workspace_cli), "sandcastle", "plan", name, "--json"], env=env)


def test_sandcastle_execute_corrupt_result_does_not_orphan_sibling_wave_process(tmp, workspace_cli):
    """Regression for bug 4a: one repo's corrupt result must not block reconciling siblings."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    setup_independent_two_repo_execute_workspace(tmp, env, workspace_cli, name="CORRUPT")
    log_path = tmp / "invoke.log"
    fake = write_scripted_sandcastle_command(
        tmp, log_path, behaviors={"alpha": "corrupt_result", "beta": "normal"}
    )
    result = run(
        [str(workspace_cli), "sandcastle", "execute", "CORRUPT", "--command", fake],
        env=env,
        check=False,
    )
    if result.returncode == 0:
        raise AssertionError("Expected non-zero exit due to alpha's corrupt result")
    log = sorted(log_path.read_text(encoding="utf-8").splitlines())
    if log != ["alpha:a-1", "beta:b-1"]:
        raise AssertionError(f"Expected both repos invoked/waited despite alpha's failure, got {log}")
    alpha = read_frontmatter(tmp / "ledger" / "workspaces" / "CORRUPT" / "issues" / "a-1.md")
    beta = read_frontmatter(tmp / "ledger" / "workspaces" / "CORRUPT" / "issues" / "b-1.md")
    if alpha["status"] != "failed":
        raise AssertionError(f"Expected alpha's issue failed after corrupt result, got {alpha}")
    if beta["status"] != "merged":
        raise AssertionError(f"Expected beta's issue reconciled normally, got {beta}")
    notes = (tmp / "ledger" / "workspaces" / "CORRUPT" / "notes.md").read_text(encoding="utf-8")
    assert_contains(notes, "Failed to reconcile")


def test_sandcastle_execute_launch_failure_still_reaps_already_launched_sibling(tmp, workspace_cli):
    """Regression: a later repo's launch-time failure must not orphan an already-`Popen`'d sibling.

    Bugs 4/5's fixes only protected the post-launch reconciliation loop. If a later
    repo's launch step raises BEFORE `subprocess.Popen` (e.g. its worktree vanished
    from disk between planning and execute), an earlier sibling already launched in
    the same wave's launch loop must still be `.wait()`-ed on and reconciled/backstopped
    normally, not left orphaned with its issue stuck `running` forever.
    """
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    setup_independent_two_repo_execute_workspace(tmp, env, workspace_cli, name="LAUNCHFAIL")

    # "alpha" sorts before "beta" in the per-wave launch loop, so alpha is already
    # launched by the time beta's launch step discovers its worktree is gone.
    beta_worktree = tmp / "workspaces" / "LAUNCHFAIL" / "beta"
    shutil.rmtree(beta_worktree)

    log_path = tmp / "invoke.log"
    fake = write_fake_sandcastle_command(tmp, log_path)
    result = run(
        [str(workspace_cli), "sandcastle", "execute", "LAUNCHFAIL", "--command", fake],
        env=env,
        check=False,
    )
    if result.returncode == 0:
        raise AssertionError("Expected non-zero exit due to beta's missing worktree")
    if "Traceback" in result.stderr:
        raise AssertionError(f"Expected a clean failure, not a crash:\n{result.stderr}")
    alpha = read_frontmatter(tmp / "ledger" / "workspaces" / "LAUNCHFAIL" / "issues" / "a-1.md")
    beta = read_frontmatter(tmp / "ledger" / "workspaces" / "LAUNCHFAIL" / "issues" / "b-1.md")
    if alpha["status"] != "merged":
        raise AssertionError(
            f"Expected alpha's already-launched sibling to be reaped and reconciled normally, got {alpha}"
        )
    if beta["status"] != "failed":
        raise AssertionError(f"Expected beta's issue marked failed due to its missing worktree, got {beta}")
    notes = (tmp / "ledger" / "workspaces" / "LAUNCHFAIL" / "notes.md").read_text(encoding="utf-8")
    assert_contains(notes, "Failed to launch")


def test_sandcastle_execute_result_with_unknown_issue_id_does_not_crash(tmp, workspace_cli):
    """Regression for bug 4b: a result referencing an unplanned issue id must not crash the run."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "UNKNOWNID", "--title", "Unknown id", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "UNKNOWNID", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "UNKNOWNID", "--json"], env=env)
    log_path = tmp / "invoke.log"
    fake = write_scripted_sandcastle_command(tmp, log_path, behaviors={"repo": "unknown_id_result"})
    result = run(
        [str(workspace_cli), "sandcastle", "execute", "UNKNOWNID", "--command", fake],
        env=env,
        check=False,
    )
    if result.returncode == 0:
        raise AssertionError("Expected a failing outcome since the real issue was never reconciled")
    if "Traceback" in result.stderr:
        raise AssertionError(f"Expected a clean failure, not a crash:\n{result.stderr}")
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "UNKNOWNID" / "issues" / "1.md")
    if issue["status"] != "failed":
        raise AssertionError(f"Expected the real targeted issue marked failed, got {issue}")


def test_sandcastle_execute_bad_entry_does_not_discard_later_good_entry(tmp, workspace_cli):
    """Regression for bug 4c: one bad entry in a result list must not discard a later valid one.

    `apply_sandcastle_result` used to raise immediately on the first bad entry in a
    result's `issues` list, silently discarding any legitimately-successful entries
    listed after it in the same list. This plans two issues in one repo; the fake
    runner's result lists an unknown-id (bad) entry FIRST, then a genuinely valid
    entry for the SECOND issue -- issue 1 is deliberately never mentioned at all, so
    the "never covered" and "poisoned by an earlier bad entry" cases are distinguishable.
    """
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "BADENTRY", "--title", "Bad entry", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "BADENTRY", "1", "--title", "First"], env=env)
    run([str(workspace_cli), "issue", "create", "BADENTRY", "2", "--title", "Second"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "BADENTRY", "--json"], env=env)
    log_path = tmp / "invoke.log"
    fake = write_scripted_sandcastle_command(tmp, log_path, behaviors={"repo": "bad_then_good_result"})
    result = run(
        [str(workspace_cli), "sandcastle", "execute", "BADENTRY", "--command", fake],
        env=env,
        check=False,
    )
    if result.returncode == 0:
        raise AssertionError("Expected non-zero overall exit since issue 1 was never covered")
    if "Traceback" in result.stderr:
        raise AssertionError(f"Expected a clean failure, not a crash:\n{result.stderr}")
    issue1 = read_frontmatter(tmp / "ledger" / "workspaces" / "BADENTRY" / "issues" / "1.md")
    issue2 = read_frontmatter(tmp / "ledger" / "workspaces" / "BADENTRY" / "issues" / "2.md")
    if issue1["status"] != "failed":
        raise AssertionError(f"Expected the never-covered issue marked failed, got {issue1}")
    if issue2["status"] != "merged":
        raise AssertionError(
            f"Expected the validly-reported issue listed AFTER the bad entry to still be "
            f"reconciled as merged, not discarded/marked failed, got {issue2}"
        )


def test_sandcastle_execute_exit_zero_no_result_marks_failed(tmp, workspace_cli):
    """Regression for bug 5a: exit 0 with no result file must not leave an issue stuck running."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "NORESULT", "--title", "No result", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "NORESULT", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "NORESULT", "--json"], env=env)
    log_path = tmp / "invoke.log"
    fake = write_scripted_sandcastle_command(tmp, log_path, behaviors={"repo": "exit0_no_result"})
    result = run(
        [str(workspace_cli), "sandcastle", "execute", "NORESULT", "--command", fake],
        env=env,
        check=False,
    )
    if result.returncode == 0:
        raise AssertionError("Expected non-zero overall exit when an issue never got a result")
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "NORESULT" / "issues" / "1.md")
    if issue["status"] != "failed":
        raise AssertionError(f"Expected issue marked failed, not left stuck running, got {issue}")


def test_sandcastle_execute_partial_result_marks_uncovered_failed(tmp, workspace_cli):
    """Regression for bug 5b: a result covering only some of a repo's issues must fail the rest."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "PARTIAL", "--title", "Partial result", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "PARTIAL", "1", "--title", "First"], env=env)
    run([str(workspace_cli), "issue", "create", "PARTIAL", "2", "--title", "Second"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "PARTIAL", "--json"], env=env)
    log_path = tmp / "invoke.log"
    fake = write_scripted_sandcastle_command(tmp, log_path, behaviors={"repo": "partial_result"})
    result = run(
        [str(workspace_cli), "sandcastle", "execute", "PARTIAL", "--command", fake],
        env=env,
        check=False,
    )
    if result.returncode == 0:
        raise AssertionError("Expected non-zero overall exit when an issue was left uncovered")
    issue1 = read_frontmatter(tmp / "ledger" / "workspaces" / "PARTIAL" / "issues" / "1.md")
    issue2 = read_frontmatter(tmp / "ledger" / "workspaces" / "PARTIAL" / "issues" / "2.md")
    if issue1["status"] != "merged":
        raise AssertionError(f"Expected the covered issue reconciled normally, got {issue1}")
    if issue2["status"] != "failed":
        raise AssertionError(f"Expected the uncovered issue marked failed, not left running, got {issue2}")


def test_sandcastle_execute_own_reconciliation_does_not_self_invalidate(tmp, workspace_cli):
    """Regression for bug 6: execute's own result reconciliation must not self-invalidate."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "SELFINV", "--title", "Self invalidate", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "SELFINV", "1", "--title", "Only"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "SELFINV", "--json"], env=env)
    log_path = tmp / "invoke.log"
    fake = write_fake_sandcastle_command(tmp, log_path)
    run(
        [str(workspace_cli), "sandcastle", "execute", "SELFINV", "--command", fake],
        env=env,
    )
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "SELFINV" / "issues" / "1.md")
    if issue["status"] != "merged":
        raise AssertionError(f"Expected execute's own reconciliation to succeed, got {issue}")
    notes_after_execute = (tmp / "ledger" / "workspaces" / "SELFINV" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert_not_contains(notes_after_execute, "invalidated")

    # Contrast: a genuinely external edit after a fresh plan must still invalidate.
    # Issue "1" is already `merged` (done) so it's no longer targeted by a fresh plan;
    # use a new, still-targetable issue to exercise the invalidation path.
    run([str(workspace_cli), "issue", "create", "SELFINV", "2", "--title", "Second"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "SELFINV", "--json"], env=env)
    if "2" not in (current_plan_pointer(tmp, "SELFINV") or {}).get("targetedIssueIds", []):
        raise AssertionError("Expected issue 2 to be targeted by the fresh plan")
    run(
        [
            str(workspace_cli),
            "issue",
            "set-status",
            "SELFINV",
            "2",
            "ready",
            "--branch",
            "feature/external-edit",
        ],
        env=env,
    )
    if current_plan_pointer(tmp, "SELFINV") is not None:
        raise AssertionError("Expected a genuinely external branch edit to still invalidate the plan")
    notes_after_edit = (tmp / "ledger" / "workspaces" / "SELFINV" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert_contains(notes_after_edit, "invalidated")


def runner_plan_for(tmp, workspace_id, *, wave, repo):
    runs_dir = tmp / "ledger" / "workspaces" / workspace_id / "runs"
    prefix = f"sandcastle-run-plan-w{wave}-{repo}-"
    matches = sorted(runs_dir.glob(f"{prefix}*"))
    if not matches:
        raise AssertionError(f"No runner plan for wave {wave} repo {repo} under {runs_dir}")
    return json.loads(matches[-1].read_text(encoding="utf-8"))


def cross_repo_dep_repos(runner_plan):
    """Repos that would receive read-only mounts for issues in this runner plan."""
    cross = set()
    for issue in runner_plan["issues"]:
        for mount in dependency_mounts_for_issue(runner_plan, issue):
            cross.add(mount["sandboxPath"].removeprefix("related-repos/"))
    return sorted(cross)


def rewrite_issue_blocked_by(issue_path, blocked_by):
    text = issue_path.read_text(encoding="utf-8")
    meta = read_frontmatter(issue_path)
    meta["blockedBy"] = [str(item) for item in blocked_by]
    body = text[text.find("\n---\n", 4) + len("\n---\n") :]
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=False).strip()
    issue_path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


def sync_plan_blocked_by_for_issue(plan_path, issue_id, blocked_by):
    """Keep the locked plan artifact's blockedBy in sync after a direct issue edit."""
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    blocked_by = [str(item) for item in blocked_by]
    for bucket in (payload.get("issuesByRepo") or {}).values():
        for entry in bucket.get("targeted") or []:
            if entry.get("id") == issue_id:
                entry["blockedBy"] = list(blocked_by)
        for entry in bucket.get("readyButNotTargeted") or []:
            if entry.get("id") == issue_id:
                entry["blockedBy"] = list(blocked_by)
        for item in bucket.get("blocked") or []:
            entry = item.get("issue") or {}
            if entry.get("id") == issue_id:
                entry["blockedBy"] = list(blocked_by)
    plan_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_sandcastle_runner_plan_cross_repo_dependency(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    setup_cross_repo_execute_workspace(tmp, env, workspace_cli)
    fake = write_fake_sandcastle_command(tmp, tmp / "invoke.log")
    run(
        [str(workspace_cli), "sandcastle", "execute", "EXEC", "--command", fake],
        env=env,
    )

    frontend_plan = runner_plan_for(tmp, "EXEC", wave=0, repo="frontend")
    backend_plan = runner_plan_for(tmp, "EXEC", wave=1, repo="backend")

    for plan in (frontend_plan, backend_plan):
        repo_names = sorted(item["name"] for item in plan["repos"])
        if repo_names != ["backend", "frontend"]:
            raise AssertionError(f"Expected full workspace repo list, got {plan['repos']}")
        for item in plan["repos"]:
            if not item.get("worktreePath"):
                raise AssertionError(f"Expected worktreePath on repo entry, got {item}")
        if "issueRepos" in plan:
            raise AssertionError("Runner plan must not include issueRepos; repo is embedded in dependsOn")

    if frontend_plan["issues"][0]["id"] != "fe-1" or frontend_plan["issues"][0]["dependsOn"] != []:
        raise AssertionError(f"Expected fe-1 with empty dependsOn, got {frontend_plan['issues'][0]}")

    backend_issue = backend_plan["issues"][0]
    expected_depends_on = [{"id": "fe-1", "repo": "frontend"}]
    if backend_issue["id"] != "be-1" or backend_issue["dependsOn"] != expected_depends_on:
        raise AssertionError(f"Expected be-1 cross-repo dependsOn, got {backend_issue}")

    if cross_repo_dep_repos(frontend_plan) != []:
        raise AssertionError(
            f"Same-wave frontend issue should have no cross-repo deps, got {cross_repo_dep_repos(frontend_plan)}"
        )
    if cross_repo_dep_repos(backend_plan) != ["frontend"]:
        raise AssertionError(
            f"Backend issue should mount frontend only, got {cross_repo_dep_repos(backend_plan)}"
        )

    mounts = dependency_mounts_for_issue(backend_plan, backend_issue)
    if len(mounts) != 1:
        raise AssertionError(f"Expected one readonly mount, got {mounts}")
    mount = mounts[0]
    if mount["readonly"] is not True:
        raise AssertionError(f"Dependency mount must be readonly, got {mount}")
    if mount["sandboxPath"] != "related-repos/frontend":
        raise AssertionError(f"Unexpected sandboxPath: {mount['sandboxPath']}")
    frontend_entry = next(item for item in backend_plan["repos"] if item["name"] == "frontend")
    if mount["hostPath"] != frontend_entry["worktreePath"]:
        raise AssertionError("Mount hostPath must be the dependency repo worktreePath")


def test_sandcastle_runner_plan_same_repo_dependency_no_cross_repos(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "SAMEREPO", "--title", "Same repo deps", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "SAMEREPO", "1", "--title", "First"], env=env)
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "SAMEREPO",
            "2",
            "--title",
            "Second",
            "--blocked-by",
            "1",
        ],
        env=env,
    )
    run([str(workspace_cli), "sandcastle", "plan", "SAMEREPO", "--json"], env=env)
    fake = write_fake_sandcastle_command(tmp, tmp / "invoke.log")
    run(
        [str(workspace_cli), "sandcastle", "execute", "SAMEREPO", "--command", fake],
        env=env,
    )

    first_plan = runner_plan_for(tmp, "SAMEREPO", wave=0, repo="repo")
    second_plan = runner_plan_for(tmp, "SAMEREPO", wave=1, repo="repo")

    if first_plan["issues"][0]["dependsOn"] != []:
        raise AssertionError(f"Expected issue 1 with no dependsOn, got {first_plan['issues'][0]}")
    if second_plan["issues"][0]["dependsOn"] != []:
        raise AssertionError(
            f"Same-repo blocker must not appear in dependsOn, got {second_plan['issues'][0]}"
        )

    if cross_repo_dep_repos(first_plan) != []:
        raise AssertionError(f"Issue 1 should have no cross-repo deps, got {cross_repo_dep_repos(first_plan)}")
    if cross_repo_dep_repos(second_plan) != []:
        raise AssertionError(
            f"Same-repo dependency must not yield cross-repo mounts, got {cross_repo_dep_repos(second_plan)}"
        )


def test_sandcastle_runner_plan_three_repo_mount_selection(tmp, workspace_cli):
    """Issue A (repo1) blocked by B (repo2, cross) and C (repo1, same) mounts repo2 only."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo1")
    make_source_repo(tmp, "repo2")
    make_source_repo(tmp, "repo3")
    run([str(workspace_cli), "create", "THREE", "--title", "Three repos", "--repo", "repo1"], env=env)
    run([str(workspace_cli), "repo", "add", "THREE", "repo2"], env=env)
    run([str(workspace_cli), "repo", "add", "THREE", "repo3"], env=env)
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "THREE",
            "issue-b",
            "--title",
            "B in repo2",
            "--repo",
            "repo2",
        ],
        env=env,
    )
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "THREE",
            "issue-c",
            "--title",
            "C in repo1",
            "--repo",
            "repo1",
        ],
        env=env,
    )
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "THREE",
            "issue-a",
            "--title",
            "A cross and same repo deps",
            "--repo",
            "repo1",
            "--blocked-by",
            "issue-b",
            "--blocked-by",
            "issue-c",
        ],
        env=env,
    )
    run([str(workspace_cli), "sandcastle", "plan", "THREE", "--json"], env=env)
    fake = write_fake_sandcastle_command(tmp, tmp / "invoke.log")
    run(
        [str(workspace_cli), "sandcastle", "execute", "THREE", "--command", fake],
        env=env,
    )

    # Wave 0: repo2 issue-b, repo1 issue-c (parallel). Wave 1: repo1 issue-a.
    repo2_plan = runner_plan_for(tmp, "THREE", wave=0, repo="repo2")
    repo1_wave0 = runner_plan_for(tmp, "THREE", wave=0, repo="repo1")
    repo1_wave1 = runner_plan_for(tmp, "THREE", wave=1, repo="repo1")

    issue_a = next(item for item in repo1_wave1["issues"] if item["id"] == "issue-a")
    if issue_a["dependsOn"] != [{"id": "issue-b", "repo": "repo2"}]:
        raise AssertionError(f"Expected cross-repo dependsOn only, got {issue_a['dependsOn']}")

    mounts = dependency_mounts_for_issue(repo1_wave1, issue_a)
    if len(mounts) != 1 or mounts[0]["sandboxPath"] != "related-repos/repo2":
        raise AssertionError(f"Expected single repo2 mount, got {mounts}")
    if mounts[0]["readonly"] is not True:
        raise AssertionError(f"Mount must be readonly, got {mounts[0]}")
    if cross_repo_dep_repos(repo1_wave1) != ["repo2"]:
        raise AssertionError(f"Must not mount repo1 (self) or repo3 (unrelated), got {cross_repo_dep_repos(repo1_wave1)}")

    if repo2_plan["issues"][0]["dependsOn"] != []:
        raise AssertionError(f"repo2 blocker should have no cross-repo deps, got {repo2_plan['issues'][0]}")
    if repo1_wave0["issues"][0]["dependsOn"] != []:
        raise AssertionError(f"repo1 wave-0 issue should have no cross-repo deps, got {repo1_wave0['issues'][0]}")


def test_sandcastle_runner_plan_depends_on_full_blocked_by_after_merge(tmp, workspace_cli):
    """dependsOn uses full blockedBy from issue metadata, not just the current wave graph."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    setup_cross_repo_execute_workspace(tmp, env, workspace_cli)
    run([str(workspace_cli), "issue", "set-status", "EXEC", "fe-1", "merged"], env=env)
    run([str(workspace_cli), "sandcastle", "plan", "EXEC", "--json"], env=env)
    fake = write_fake_sandcastle_command(tmp, tmp / "invoke.log")
    run(
        [str(workspace_cli), "sandcastle", "execute", "EXEC", "--command", fake],
        env=env,
    )

    backend_plan = runner_plan_for(tmp, "EXEC", wave=0, repo="backend")
    backend_issue = backend_plan["issues"][0]
    if backend_issue["dependsOn"] != [{"id": "fe-1", "repo": "frontend"}]:
        raise AssertionError(
            f"Merged cross-repo blocker must still appear in dependsOn, got {backend_issue}"
        )
    mounts = dependency_mounts_for_issue(backend_plan, backend_issue)
    if len(mounts) != 1 or mounts[0]["readonly"] is not True:
        raise AssertionError(f"Expected readonly mount for merged blocker repo, got {mounts}")


def test_sandcastle_runner_plan_omits_unresolvable_blockers_from_depends_on(tmp, workspace_cli):
    """Missing blocker IDs and unresolvable-repo blockers are skipped in dependsOn."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo1")
    make_source_repo(tmp, "repo2")
    run([str(workspace_cli), "create", "UNRES", "--title", "Unresolvable blockers", "--repo", "repo1"], env=env)
    run([str(workspace_cli), "repo", "add", "UNRES", "repo2"], env=env)
    run(
        [str(workspace_cli), "issue", "create", "UNRES", "issue-ambiguous", "--title", "No repo set"],
        env=env,
    )
    run([str(workspace_cli), "issue", "set-status", "UNRES", "issue-ambiguous", "merged"], env=env)
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "UNRES",
            "issue-b",
            "--title",
            "Resolvable cross-repo blocker",
            "--repo",
            "repo2",
        ],
        env=env,
    )
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "UNRES",
            "issue-a",
            "--title",
            "Depends on resolvable and unresolvable blockers",
            "--repo",
            "repo1",
            "--blocked-by",
            "issue-b",
            "--blocked-by",
            "issue-ambiguous",
        ],
        env=env,
    )
    run([str(workspace_cli), "sandcastle", "plan", "UNRES", "--json"], env=env)

    issue_a_path = tmp / "ledger" / "workspaces" / "UNRES" / "issues" / "issue-a.md"
    blocked_by = ["issue-b", "issue-ambiguous", "missing-ghost"]
    rewrite_issue_blocked_by(issue_a_path, blocked_by)
    plan_path = Path(current_plan_pointer(tmp, "UNRES")["path"])
    sync_plan_blocked_by_for_issue(plan_path, "issue-a", blocked_by)

    fake = write_fake_sandcastle_command(tmp, tmp / "invoke.log")
    run(
        [str(workspace_cli), "sandcastle", "execute", "UNRES", "--command", fake],
        env=env,
    )

    repo1_wave1 = runner_plan_for(tmp, "UNRES", wave=1, repo="repo1")
    issue_a = next(item for item in repo1_wave1["issues"] if item["id"] == "issue-a")
    expected_depends_on = [{"id": "issue-b", "repo": "repo2"}]
    if issue_a["dependsOn"] != expected_depends_on:
        raise AssertionError(
            f"Expected only resolvable cross-repo blocker in dependsOn, got {issue_a['dependsOn']}"
        )
    if any(dep.get("id") in {"issue-ambiguous", "missing-ghost"} for dep in issue_a["dependsOn"]):
        raise AssertionError(f"Unresolvable blockers must be omitted, got {issue_a['dependsOn']}")
    mounts = dependency_mounts_for_issue(repo1_wave1, issue_a)
    if len(mounts) != 1 or mounts[0]["sandboxPath"] != "related-repos/repo2":
        raise AssertionError(f"Expected single repo2 mount, got {mounts}")


def test_sandcastle_runner_plan_dedups_same_cross_repo_blockers_in_depends_on(tmp, workspace_cli):
    """Two different blockers in the same cross-repo dedupe to one dependsOn entry."""
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo1")
    make_source_repo(tmp, "repo2")
    run([str(workspace_cli), "create", "DEDUP", "--title", "Same-repo dedup", "--repo", "repo1"], env=env)
    run([str(workspace_cli), "repo", "add", "DEDUP", "repo2"], env=env)
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "DEDUP",
            "issue-b1",
            "--title",
            "First repo2 blocker",
            "--repo",
            "repo2",
        ],
        env=env,
    )
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "DEDUP",
            "issue-b2",
            "--title",
            "Second repo2 blocker",
            "--repo",
            "repo2",
        ],
        env=env,
    )
    run(
        [
            str(workspace_cli),
            "issue",
            "create",
            "DEDUP",
            "issue-a",
            "--title",
            "Two blockers same cross-repo",
            "--repo",
            "repo1",
            "--blocked-by",
            "issue-b1",
            "--blocked-by",
            "issue-b2",
        ],
        env=env,
    )
    run([str(workspace_cli), "sandcastle", "plan", "DEDUP", "--json"], env=env)
    fake = write_fake_sandcastle_command(tmp, tmp / "invoke.log")
    run(
        [str(workspace_cli), "sandcastle", "execute", "DEDUP", "--command", fake],
        env=env,
    )

    repo1_wave1 = runner_plan_for(tmp, "DEDUP", wave=1, repo="repo1")
    issue_a = next(item for item in repo1_wave1["issues"] if item["id"] == "issue-a")
    expected_depends_on = [{"id": "issue-b1", "repo": "repo2"}]
    if issue_a["dependsOn"] != expected_depends_on:
        raise AssertionError(
            f"Expected first cross-repo blocker only (deduped by repo), got {issue_a['dependsOn']}"
        )
    if len(issue_a["dependsOn"]) != 1:
        raise AssertionError(f"Expected exactly one dependsOn entry, got {issue_a['dependsOn']}")
    mounts = dependency_mounts_for_issue(repo1_wave1, issue_a)
    if len(mounts) != 1 or mounts[0]["sandboxPath"] != "related-repos/repo2":
        raise AssertionError(f"Expected single repo2 mount after dedup, got {mounts}")
    if cross_repo_dep_repos(repo1_wave1) != ["repo2"]:
        raise AssertionError(f"Expected one cross-repo mount target, got {cross_repo_dep_repos(repo1_wave1)}")


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


def test_makefile_install_produces_sibling_skill_layout(tmp):
    fake_home = tmp / "home"
    fake_home.mkdir()
    run(["make", "install"], cwd=str(ROOT), env={**os.environ, "HOME": str(fake_home)})

    skills = fake_home / ".agents" / "skills"
    base_skill_md = skills / "workspaces" / "SKILL.md"
    assert_path(base_skill_md)

    sandcastle_dir = skills / "workspaces-with-sandcastle"
    if not sandcastle_dir.is_symlink():
        raise AssertionError(
            f"Expected {sandcastle_dir} to be a symlink to a sibling of the workspaces "
            f"install's scripts, so it is independently loadable while ../scripts/ still resolves"
        )

    sandcastle_skill_md = sandcastle_dir / "SKILL.md"
    assert_path(sandcastle_skill_md)
    expected_skill_md = skills / "workspaces" / "workspaces-with-sandcastle" / "SKILL.md"
    if sandcastle_skill_md.resolve() != expected_skill_md.resolve():
        raise AssertionError(
            f"Sandcastle SKILL.md should resolve into the workspaces install, "
            f"got {sandcastle_skill_md.resolve()} expected {expected_skill_md.resolve()}"
        )

    sandcastle_script = sandcastle_dir / ".." / "scripts" / "workspace_with_sandcastle.py"
    assert_path(sandcastle_script)
    expected_script = skills / "workspaces" / "scripts" / "workspace_with_sandcastle.py"
    if sandcastle_script.resolve() != expected_script.resolve():
        raise AssertionError(
            f"../scripts/ from inside the sandcastle skill dir should resolve to the "
            f"workspaces install's scripts, got {sandcastle_script.resolve()} "
            f"expected {expected_script.resolve()}"
        )


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
    if sandcastle_issue_meta(issue2)["type"] != "HITL":
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
    sc = sandcastle_issue_meta(issue)
    if sc["type"] != "HITL" or sc["reviewStatus"] != "pending":
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
    sc1 = sandcastle_issue_meta(issue1)
    if sc1["type"] != "AFK" or sc1["reviewStatus"] != "approved":
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
    sc = sandcastle_issue_meta(issue)
    if sc["type"] != "HITL" or sc["reviewStatus"] != "pending":
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
    sc = sandcastle_issue_meta(issue)
    if sc["type"] != "AFK" or sc["reviewStatus"] != "approved":
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
    if sandcastle_issue_meta(conflict_issue)["reviewStatus"] != "approved":
        raise AssertionError(
            f"Expected existing nested block value to win over stray legacy value, got {conflict_issue}"
        )


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
    test_repo_refresh_repo_updates_git_metadata,
    test_github_sync_apply_pr_event_maps_state_and_records_pr,
    test_github_sync_sync_prs_raises_when_gh_missing,
]


BASE_TESTS = [
    test_config_and_ledger_workspace,
    test_worktree_adopt_status_note_close,
    test_adopt_external_worktree_pointer_and_doctor,
    test_list_json_and_ids_only_flags,
    test_init_config_prompts_and_force_overwrite,
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
    test_sandcastle_execute_resolves_plan_pointer_and_override,
    test_sandcastle_execute_fallback_newest_valid_plan,
    test_sandcastle_execute_refuses_stale_plan,
    test_sandcastle_execute_multi_wave_multi_repo,
    test_sandcastle_execute_propagates_blocked_hitl,
    test_sandcastle_execute_clears_pointer_on_failure,
    test_sandcastle_execute_refuses_active_lock,
    test_sandcastle_execute_eager_invalidation,
    test_sandcastle_execute_stale_on_repo_removal_and_rename,
    test_sandcastle_execute_status_only_transition_not_stale,
    test_sandcastle_execute_recovers_stale_lock_from_dead_pid,
    test_sandcastle_execute_concurrent_lock_recovery_is_exclusive,
    test_sandcastle_execute_recovers_past_lock_killed_mid_recovery,
    test_sandcastle_execute_corrupt_result_does_not_orphan_sibling_wave_process,
    test_sandcastle_execute_launch_failure_still_reaps_already_launched_sibling,
    test_sandcastle_execute_result_with_unknown_issue_id_does_not_crash,
    test_sandcastle_execute_bad_entry_does_not_discard_later_good_entry,
    test_sandcastle_execute_exit_zero_no_result_marks_failed,
    test_sandcastle_execute_partial_result_marks_uncovered_failed,
    test_sandcastle_execute_own_reconciliation_does_not_self_invalidate,
    test_sandcastle_runner_plan_cross_repo_dependency,
    test_sandcastle_runner_plan_same_repo_dependency_no_cross_repos,
    test_sandcastle_runner_plan_three_repo_mount_selection,
    test_sandcastle_runner_plan_depends_on_full_blocked_by_after_merge,
    test_sandcastle_runner_plan_omits_unresolvable_blockers_from_depends_on,
    test_sandcastle_runner_plan_dedups_same_cross_repo_blockers_in_depends_on,
    test_cleanup_plan_and_execution,
]

CLI_SPLIT_TESTS = [
    test_cli_split_help_and_restrictions,
    test_makefile_install_produces_sibling_skill_layout,
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
    test_sandcastle_execute_eager_invalidation_via_base_cli,
]


def main():
    passed = 0
    for test in WORKSPACE_MODEL_TESTS:
        test()
        print(f"ok {test.__name__}")
        passed += 1

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
