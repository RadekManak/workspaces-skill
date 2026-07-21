#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import support
from tests.support import (
    assert_contains,
    assert_final_vscode_json,
    assert_not_contains,
    assert_path,
    first_stdout_line,
    make_source_repo,
    parse_trailing_stdout_json_blocks,
    read_yaml,
    run,
    write_config,
)

from workspaces.git import branch_exists


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
    assert_not_contains(status, "Config:")
    status_with_config = run(
        [str(workspace_cli), "status", "TASK-2", "--show-config"], env=env
    ).stdout
    assert_contains(status_with_config, "Config:")
    assert_contains(status_with_config, f"  ledger_root: {tmp / 'ledger'}")
    assert_contains(status_with_config, "  base_branch: main")

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


def test_cleanup_plan_refreshes_stale_open_pr_via_gh(tmp, workspace_cli):
    config_path = tmp / "config.yaml"
    env = write_config(config_path, tmp, workspace_cli)
    make_source_repo(tmp, "repo")
    run(
        [str(workspace_cli), "create", "TASK-PR-REFRESH", "--title", "PR refresh cleanup", "--repo", "repo"],
        env=env,
    )
    run([str(workspace_cli), "close", "TASK-PR-REFRESH"], env=env)

    workspace_yaml = tmp / "ledger" / "workspaces" / "TASK-PR-REFRESH" / "workspace.yaml"
    data = read_yaml(workspace_yaml)
    data["links"] = {
        "githubPrs": [
            {
                "url": "https://github.com/openshift/example/pull/97",
                "number": 97,
                "repo": "example",
                "branch": "feature",
                "state": "OPEN",
                "merged": False,
            }
        ]
    }
    workspace_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if 'view' not in sys.argv:\n"
        "    sys.exit(2)\n"
        "print(json.dumps({\n"
        "  'number': 97,\n"
        "  'url': 'https://github.com/openshift/example/pull/97',\n"
        "  'state': 'MERGED',\n"
        "  'mergedAt': '2026-07-21T14:11:20Z',\n"
        "  'title': 'done',\n"
        "  'headRefName': 'feature',\n"
        "}))\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = {**env, "PATH": f"{bin_dir}:{env.get('PATH', '')}"}

    plan = json.loads(
        run([str(workspace_cli), "cleanup-plan", "TASK-PR-REFRESH", "--json"], env=env).stdout
    )
    candidate = plan["candidates"][0]
    if candidate["recommendedAction"] != "safe-to-remove":
        raise AssertionError(
            f"Expected stale OPEN PR refreshed via gh to allow safe-to-remove, got {candidate}"
        )
    if candidate.get("openPrCount") != 0:
        raise AssertionError(f"Expected openPrCount 0 after gh refresh, got {candidate}")

    refreshed = read_yaml(workspace_yaml)
    pr = refreshed["links"]["githubPrs"][0]
    if pr.get("state") != "MERGED" or not pr.get("merged"):
        raise AssertionError(f"Expected ledger PR snapshot persisted as MERGED, got {pr}")


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
    test_cleanup_plan_refreshes_stale_open_pr_via_gh,
    test_doctor_fix_output,
    test_doctor_multi_id_and_all,
    test_doctor_fix_skips_vscode_json_for_cleanup_done_workspace,
]


def main():
    passed = support.run_dual_entrypoint(BASE_TESTS)
    print(f"{passed} self-check(s) passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"self-check failed: {error}", file=sys.stderr)
        sys.exit(1)
