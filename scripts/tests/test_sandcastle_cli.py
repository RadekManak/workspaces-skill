#!/usr/bin/env python3
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import support
from tests.support import (
    assert_contains,
    assert_not_contains,
    assert_path,
    make_source_repo,
    parse_final_stdout_json,
    read_frontmatter,
    read_yaml,
    run,
    write_config,
)

from workspaces.issues import sandcastle_issue_meta
from workspaces.sandcastle_execute import dependency_mounts_for_issue


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


def main():
    passed = support.run_sandcastle_only(SANDCASTLE_TESTS)
    print(f"{passed} self-check(s) passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"self-check failed: {error}", file=sys.stderr)
        sys.exit(1)
