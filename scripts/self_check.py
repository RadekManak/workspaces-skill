#!/usr/bin/env python3
import json
import os
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
    ledger = Path(result.stdout.strip())
    assert_path(ledger / "workspace.yaml")
    assert_path(ledger / "spec.md")
    assert_path(ledger / "notes.md")
    assert_path(ledger / "issues")
    workspace_file = tmp / "workspaces" / "TASK-1" / "TASK-1.code-workspace"
    assert_path(workspace_file)
    workspace_payload = json.loads(workspace_file.read_text(encoding="utf-8"))
    folder_names = [folder["name"] for folder in workspace_payload["folders"]]
    if folder_names != ["TASK-1 ledger"]:
        raise AssertionError(f"Unexpected VS Code workspace folders: {workspace_payload}")
    open_path = run([str(workspace_cli), "open", "TASK-1", "--print"], env=env).stdout.strip()
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
    workspace_payload = json.loads(workspace_file.read_text(encoding="utf-8"))
    folder_names = [folder["name"] for folder in workspace_payload["folders"]]
    if folder_names != ["TASK-2 ledger", "repo"]:
        raise AssertionError(f"Unexpected VS Code workspace folders: {workspace_payload}")
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


def test_sandcastle_plan_execute_reconcile_and_runner(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")

    run([str(workspace_cli), "create", "TASK-4", "--title", "Sandcastle", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "TASK-4", "1", "--title", "First"], env=env)
    run(
        [str(workspace_cli), "issue", "create", "TASK-4", "2", "--title", "Human", "--type", "hitl"],
        env=env,
    )

    plan = json.loads(run([str(workspace_cli), "sandcastle", "TASK-4", "--json"], env=env).stdout)
    if [issue["id"] for issue in plan["issues"]] != ["1"]:
        raise AssertionError(f"Unexpected plan issues: {plan}")
    if [issue["id"] for issue in plan["hitl"]] != ["2"]:
        raise AssertionError(f"Unexpected HITL issues: {plan}")
    assert_path(plan["planPath"])
    assert_path(Path(plan["resultPath"]).parent)

    run([str(workspace_cli), "sandcastle", "TASK-4", "--init-runner"], env=env)
    scaffold = tmp / "workspaces" / "TASK-4" / "repo" / ".sandcastle"
    assert_path(scaffold / "main.mts")
    assert_path(scaffold / "implement-prompt.md")
    assert_path(scaffold / "review-prompt.md")
    repeat = run(
        [str(workspace_cli), "sandcastle", "TASK-4", "--init-runner"],
        env=env,
        check=False,
    )
    if repeat.returncode == 0:
        raise AssertionError("Expected init-runner to refuse overwrite")
    run([str(workspace_cli), "sandcastle", "TASK-4", "--init-runner", "--force"], env=env)

    command = (
        "python3 - <<'PY'\n"
        "import json, os\n"
        "with open(os.environ['WORKSPACE_SANDCASTLE_RESULT'], 'w', encoding='utf-8') as f:\n"
        "    json.dump({'issues':[{'id':'1','status':'merged','reviewStatus':'approved','cleanupStatus':'pending','commits':[{'sha':'abc123'}]}]}, f)\n"
        "PY"
    )
    run([str(workspace_cli), "sandcastle", "TASK-4", "--execute", "--command", command], env=env)
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "TASK-4" / "issues" / "1.md")
    if issue["status"] != "merged" or issue["reviewStatus"] != "approved":
        raise AssertionError(f"Expected reconciled issue, got {issue}")
    assert_contains(run([str(workspace_cli), "status", "TASK-4"], env=env).stdout, "ready HITL: 1")

    result_path = tmp / "manual-result.json"
    result_path.write_text(
        json.dumps({"issues": [{"id": "2", "status": "done", "reviewStatus": "approved"}]}),
        encoding="utf-8",
    )
    run([str(workspace_cli), "sandcastle", "TASK-4", "--reconcile-result", str(result_path)], env=env)
    assert_contains(run([str(workspace_cli), "status", "TASK-4"], env=env).stdout, "State: user-review")


def test_sandcastle_failure_marks_failed(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "TASK-5", "--title", "Failure", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "TASK-5", "1", "--title", "First"], env=env)
    proc = run(
        [str(workspace_cli), "sandcastle", "TASK-5", "--execute", "--command", "exit 7"],
        env=env,
        check=False,
    )
    if proc.returncode != 7:
        raise AssertionError(f"Expected exit 7, got {proc.returncode}")
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "TASK-5" / "issues" / "1.md")
    if issue["status"] != "failed":
        raise AssertionError(f"Expected failed issue, got {issue}")


def test_sandcastle_lock_refuses_concurrent_execute(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run([str(workspace_cli), "create", "TASK-LOCK", "--title", "Lock", "--repo", "repo"], env=env)
    run([str(workspace_cli), "issue", "create", "TASK-LOCK", "1", "--title", "First"], env=env)
    lock = tmp / "ledger" / "workspaces" / "TASK-LOCK" / "runs" / "active-sandcastle-run.json"
    lock.write_text(json.dumps({"pid": 123, "createdAt": "test"}), encoding="utf-8")
    proc = run(
        [str(workspace_cli), "sandcastle", "TASK-LOCK", "--execute", "--command", "true"],
        env=env,
        check=False,
    )
    if proc.returncode == 0:
        raise AssertionError("Expected active Sandcastle lock to refuse execution")
    issue = read_frontmatter(tmp / "ledger" / "workspaces" / "TASK-LOCK" / "issues" / "1.md")
    if issue["status"] != "planned":
        raise AssertionError(f"Expected locked issue to remain planned, got {issue}")


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
    run([str(workspace_cli), "sandcastle", "TASK-6", "--reconcile-result", str(result_path)], env=env)

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

    worktree = tmp / "workspaces" / "TASK-DOC" / "repo"
    (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    dirty_text = run([str(workspace_cli), "doctor", "TASK-DOC", "--fix"], env=env).stdout
    assert_contains(dirty_text, "- warning repo-dirty:")


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

    no_args = run([str(workspace_cli), "doctor"], env=env, check=False)
    if no_args.returncode == 0:
        raise AssertionError("Expected doctor with no args and no CWD workspace to fail")


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
    if "type" in issue or "reviewStatus" in issue:
        raise AssertionError(f"Base issue create should omit Sandcastle fields, got {issue}")


def test_open_identical_between_entrypoints(tmp):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, WORKSPACE)
    run([str(WORKSPACE), "create", "OPEN-CMP", "--title", "Open compare"], env=env)
    base_open = run([str(WORKSPACE), "open", "OPEN-CMP", "--print"], env=env).stdout.strip()
    sandcastle_open = run(
        [str(WORKSPACE_SANDCASTLE), "open", "OPEN-CMP", "--print"], env=env
    ).stdout.strip()
    if base_open != sandcastle_open:
        raise AssertionError(
            f"Expected identical open output, base={base_open!r} sandcastle={sandcastle_open!r}"
        )


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
    # (carries `type`/`reviewStatus`) must be updatable via the base entrypoint
    # without losing that pre-existing Sandcastle metadata.
    run(
        [str(WORKSPACE_SANDCASTLE), "issue", "create", "XENT", "2", "--title", "Sandcastle-authored", "--type", "HITL"],
        env=env,
    )
    run([str(WORKSPACE), "issue", "set-status", "XENT", "2", "in-progress"], env=env)
    issue2 = read_frontmatter(tmp / "ledger" / "workspaces" / "XENT" / "issues" / "2.md")
    if issue2.get("type") != "HITL":
        raise AssertionError(f"Expected base-entrypoint update to preserve type=HITL, got {issue2}")


BASE_TESTS = [
    test_config_and_ledger_workspace,
    test_worktree_adopt_status_note_close,
    test_issue_graph_and_user_review,
    test_repo_crud_and_pr_lifecycle,
    test_workspace_cleanup_plan_and_execution,
    test_workspace_force_cleanup,
    test_doctor_fix_output,
    test_doctor_multi_id_and_all,
]

SANDCASTLE_TESTS = [
    test_sandcastle_plan_execute_reconcile_and_runner,
    test_sandcastle_failure_marks_failed,
    test_sandcastle_lock_refuses_concurrent_execute,
    test_cleanup_plan_and_execution,
]

CLI_SPLIT_TESTS = [
    test_cli_split_help_and_restrictions,
    test_base_issue_create_omits_sandcastle_fields,
    test_open_identical_between_entrypoints,
    test_cross_entrypoint_issue_compat,
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
