"""Offline handoff smoke tests. Host observations are fixtures, not live proof."""

import json
from pathlib import Path
import sys

import yaml

from codex_adapter.desktop import matching_project, observe_task, prepare_task
from codex_adapter.workspace import HandoffError, ManagedWorkspaceProject
from tests.support import ROOT, WORKSPACE, make_source_repo, run, write_config


def fixture(tmp):
    repositories = [make_source_repo(tmp, name) for name in ("cluster-api", "machine-api-operator")]
    ledger = tmp / "ledger"
    ledger.mkdir()
    (ledger / "spec.md").write_text("Create another workspace and launch another task.")
    workspace_file = tmp / "workspace.code-workspace"
    metadata = {
        "id": "two-repos", "title": "Two repositories",
        "vscodeWorkspacePath": str(workspace_file),
        "repos": [{"name": path.name, "worktreePath": str(path), "branch": "main"} for path in repositories],
    }
    (ledger / "workspace.yaml").write_text(yaml.safe_dump(metadata))
    document = {
        "folders": [{"path": str(path.relative_to(tmp))} for path in [ledger, *repositories]],
        "settings": {"workspaces.ledgerPath": str(ledger), "workspaces.workspaceId": "two-repos"},
    }
    workspace_file.write_text(json.dumps(document))
    return workspace_file, document, metadata


def load(workspace_file, document, **kwargs):
    return ManagedWorkspaceProject.from_output(
        "Created workspace\n" + json.dumps(document), workspace_file,
        operation="create", exit_code=0, handoff=True, **kwargs,
    )


def project_records(paths):
    return {"projects": [{"projectId": "desktop", "projectKind": "local", "roots": [str(path) for path in paths]}]}


def test_handoff_topology_and_prompt(tmp):
    file, document, _ = fixture(tmp)
    workspace = load(file, document)
    assert [repo.name for repo in workspace.repositories] == ["cluster-api", "machine-api-operator"]
    assert workspace.project_root == workspace.repositories[0].path
    for path in (*workspace.workspace_roots, workspace.spec):
        assert str(path) in workspace.prompt
    assert "branch: `main`" in workspace.prompt
    assert "workspace already exists" in workspace.prompt
    assert "Do not modify files yet" in workspace.prompt
    assert "Create another workspace" not in workspace.prompt
    assert load(file, document, task="Implement X").prompt.endswith("Task work:\n\nImplement X")
    override = load(file, document, primary_repository="machine-api-operator")
    assert override.project_root == workspace.repositories[1].path
    assert override.attachment_order == (override.project_root, workspace.project_root, workspace.ledger)


def test_handoff_validation(tmp):
    file, document, metadata = fixture(tmp)
    ledger_file = tmp / "ledger" / "workspace.yaml"
    for repos in ([], metadata["repos"] * 2, [{**metadata["repos"][0], "worktreePath": str(tmp / "missing")} ]):
        ledger_file.write_text(yaml.safe_dump({**metadata, "repos": repos}))
        try:
            load(file, document)
        except HandoffError:
            pass
        else:
            raise AssertionError("Invalid repository topology accepted")
    ledger_file.write_text(yaml.safe_dump(metadata))
    for malformed in ({**document, "folders": document["folders"][:-1]},
                      {**document, "folders": document["folders"] + document["folders"][:1]}):
        try:
            load(file, malformed)
        except HandoffError:
            pass
        else:
            raise AssertionError("Mismatched generated topology accepted")
    try:
        load(file, document, primary_repository="unknown")
    except HandoffError:
        pass
    else:
        raise AssertionError("Unknown primary accepted")


def test_handoff_matching_and_observation(tmp):
    file, document, _ = fixture(tmp)
    workspace = load(file, document)
    single = project_records([workspace.project_root])
    project = matching_project(single, workspace)
    assert project is not None
    prepared = prepare_task(workspace, project)
    assert prepared["status"] == "host-action-required"
    assert not prepared["manual_action_required"]
    assert matching_project(single, workspace, True) is None
    assert matching_project(project_records(workspace.attachment_order), workspace, True)
    assert matching_project(project_records(reversed(workspace.attachment_order)), workspace, True) is None
    assert matching_project(project_records([workspace.project_root, tmp / "unrelated"]), workspace) is None
    for key in ("id", "threadId"):
        observed = observe_task(workspace, project, {"threadId": "ready"},
                                [{key: "ready", "projectId": project.id}])
        assert observed["thread_observed"] and observed["desktop_project_association_verified"]
    direct = observe_task(workspace, project, {"threadId": "ready"}, [], {"thread": {"id": "ready"}})
    assert direct["thread_started"] and direct["thread_observed"]
    assert not direct["desktop_project_association_verified"]
    assert direct["submission_target_project_id"] == project.id
    assert direct["project_scope"] == "primary-only"
    assert direct["workspace_paths_supplied"]
    assert direct["workspace_access"] == "unknown"
    assert not direct["all_roots_attached"] and not direct["manual_action_required"]
    assert direct["unattached_paths"] == [str(workspace.repositories[1].path), str(workspace.ledger)]
    assert not direct["automatic_retry_allowed"]
    assert observe_task(workspace, project, {}, [])["status"] == "submission-unknown"
    pending = observe_task(workspace, project, {"clientThreadId": "pending"}, [])
    assert not pending["thread_started"] and pending["status"] == "handoff-pending"


def test_two_repository_handoff_cli_smoke(tmp):
    env = write_config(tmp / "config.yaml", tmp, WORKSPACE)
    for name in ("cluster-api", "machine-api-operator"):
        make_source_repo(tmp, name)
    created = run([str(WORKSPACE), "create", "two-repos", "--repo", "cluster-api", "--repo", "machine-api-operator",
                   "--input", "Create a workspace and launch a thread"], env=env)
    file = tmp / "workspaces" / "two-repos" / "two-repos.code-workspace"
    stdout = tmp / "stdout"
    stdout.write_text(created.stdout)
    projects = tmp / "projects.json"
    primary = file.parent / "cluster-api"
    projects.write_text(json.dumps(project_records([primary])))
    common = [sys.executable, str(ROOT / "scripts" / "codex_handoff.py")]
    args = ["--operation", "create", "--workspace-exit-code", "0", "--workspace-output", str(stdout),
            "--workspace-file", str(file), "--handoff", "--projects-json", str(projects)]
    prepared = json.loads(run([*common, "prepare", *args]).stdout)
    assert prepared["status"] == "host-action-required" and not prepared["manual_action_required"]
    assert prepared["arguments"]["target"]["projectId"] == "desktop"
    assert str(file.parent / "machine-api-operator") in prepared["arguments"]["prompt"]
    assert "Create a workspace and launch a thread" not in prepared["arguments"]["prompt"]
    response, direct = tmp / "response.json", tmp / "direct.json"
    response.write_text(json.dumps({"threadId": "ready"}))
    direct.write_text(json.dumps({"id": "ready"}))
    observed = json.loads(run([*common, "observe", *args, "--task-response-json", str(response), "--thread-json", str(direct)]).stdout)
    assert observed["thread_started"] and observed["thread_observed"]
    assert not observed["manual_action_required"] and not observed["all_roots_attached"]
    strict = run([*common, "prepare", *args, "--require-all-roots"], check=False)
    assert strict.returncode == 1
    assert json.loads(strict.stdout)["status"] == "manual-action-required"
    legacy = run([*common, "prepare", *args, "Create another workspace"], check=False)
    assert legacy.returncode == 2


CODEX_HANDOFF_TESTS = [test_handoff_topology_and_prompt, test_handoff_validation,
                      test_handoff_matching_and_observation, test_two_repository_handoff_cli_smoke]
