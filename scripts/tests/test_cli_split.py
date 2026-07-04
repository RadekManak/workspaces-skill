#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import support
from tests.support import (
    ROOT,
    WORKSPACE,
    WORKSPACE_SANDCASTLE,
    assert_contains,
    assert_final_vscode_json,
    assert_not_contains,
    assert_path,
    first_stdout_line,
    make_source_repo,
    parse_final_stdout_json,
    parse_trailing_stdout_json_blocks,
    read_frontmatter,
    read_yaml,
    run,
    write_config,
)
from tests.test_sandcastle_cli import current_plan_pointer

from workspaces.issues import sandcastle_issue_meta


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
    passed = support.run_cli_split(CLI_SPLIT_TESTS)
    print(f"{passed} self-check(s) passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"self-check failed: {error}", file=sys.stderr)
        sys.exit(1)
