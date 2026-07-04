#!/usr/bin/env python3
"""Shared test harness for the workspaces self-check suite."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "scripts" / "workspace.py"
WORKSPACE_SANDCASTLE = ROOT / "scripts" / "workspace_with_sandcastle.py"
sys.path.insert(0, str(ROOT / "scripts"))


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


def run_plain(tests):
    passed = 0
    for test in tests:
        test()
        print(f"ok {test.__name__}")
        passed += 1
    return passed


def run_dual_entrypoint(tests):
    passed = 0
    for test in tests:
        for workspace_cli in (WORKSPACE, WORKSPACE_SANDCASTLE):
            with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
                test(Path(tmp_dir), workspace_cli)
            print(f"ok {test.__name__} [{workspace_cli.name}]")
            passed += 1
    return passed


def run_sandcastle_only(tests):
    passed = 0
    for test in tests:
        with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
            test(Path(tmp_dir), WORKSPACE_SANDCASTLE)
        print(f"ok {test.__name__} [{WORKSPACE_SANDCASTLE.name}]")
        passed += 1
    return passed


def run_cli_split(tests):
    passed = 0
    for test in tests:
        with tempfile.TemporaryDirectory(prefix="workspaces-self-check-") as tmp_dir:
            test(Path(tmp_dir))
        print(f"ok {test.__name__}")
        passed += 1
    return passed
