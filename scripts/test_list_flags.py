"""Tests for workspace list --json and --ids-only flags."""

import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workspace


def make_ledger(tmp, workspaces):
    ledger = Path(tmp) / "_ledger" / "workspaces"
    ledger.mkdir(parents=True)
    for ws in workspaces:
        ws_dir = ledger / ws["id"]
        ws_dir.mkdir(parents=True)
        with (ws_dir / "workspace.yaml").open("w") as f:
            yaml.safe_dump(ws, f, sort_keys=False)
    return str(Path(tmp) / "_ledger")


def make_config(tmp):
    return {
        "source_root": str(tmp),
        "workspace_root": str(Path(tmp) / "roots"),
        "ledger_root": str(Path(tmp) / "_ledger"),
        "base_remote": "origin",
        "base_branch": "main",
        "push_remote": "fork",
        "jira_base_url": "",
        "github_host": "github.com",
        "editor_command": "code",
    }


SAMPLE_WORKSPACES = [
    {
        "id": "alpha",
        "title": "Alpha workspace",
        "state": "in-progress",
        "updatedAt": "2026-01-01T00:00:00+00:00",
        "repos": [],
        "links": {"githubPrs": []},
    },
    {
        "id": "beta",
        "title": "Beta workspace",
        "state": "in-progress",
        "updatedAt": "2026-01-02T00:00:00+00:00",
        "repos": [],
        "links": {"githubPrs": []},
    },
    {
        "id": "gamma",
        "title": "Gamma workspace",
        "state": "closed",
        "updatedAt": "2026-01-03T00:00:00+00:00",
        "repos": [],
        "links": {"githubPrs": []},
    },
]


def run_cmd_list(tmp, extra_args=None):
    config = make_config(tmp)
    make_ledger(tmp, SAMPLE_WORKSPACES)

    original_load_config = workspace.load_config
    workspace.load_config = lambda: config
    try:
        parser = workspace.build_parser()
        argv = ["list"] + (extra_args or [])
        args = parser.parse_args(argv)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            args.func(args)
        return buf.getvalue()
    finally:
        workspace.load_config = original_load_config


def test_ids_only():
    with tempfile.TemporaryDirectory() as tmp:
        output = run_cmd_list(tmp, ["--ids-only", "--all"])
    lines = output.strip().split("\n")
    assert set(lines) == {"alpha", "beta", "gamma"}, f"got {lines}"
    for line in lines:
        assert line == line.strip()


def test_ids_only_active_filter():
    with tempfile.TemporaryDirectory() as tmp:
        output = run_cmd_list(tmp, ["--ids-only"])
    lines = output.strip().split("\n")
    assert "gamma" not in lines, "closed workspace should be filtered out"
    assert set(lines) == {"alpha", "beta"}


def test_ids_only_state_filter():
    with tempfile.TemporaryDirectory() as tmp:
        output = run_cmd_list(tmp, ["--ids-only", "--state", "closed"])
    lines = output.strip().split("\n")
    assert lines == ["gamma"]


def test_ids_only_no_header():
    with tempfile.TemporaryDirectory() as tmp:
        output = run_cmd_list(tmp, ["--ids-only", "--all"])
    assert "ID" not in output
    assert "Workspaces" not in output
    assert "STATE" not in output


def test_json_output():
    with tempfile.TemporaryDirectory() as tmp:
        output = run_cmd_list(tmp, ["--json", "--all"])
    data = json.loads(output)
    assert isinstance(data, list)
    assert len(data) == 3
    required_keys = {"id", "state", "updated", "repos", "local", "prs", "title"}
    for item in data:
        assert required_keys.issubset(item.keys()), f"missing keys in {item}"


def test_json_active_filter():
    with tempfile.TemporaryDirectory() as tmp:
        output = run_cmd_list(tmp, ["--json"])
    data = json.loads(output)
    ids = [item["id"] for item in data]
    assert "gamma" not in ids
    assert set(ids) == {"alpha", "beta"}


def test_json_state_filter():
    with tempfile.TemporaryDirectory() as tmp:
        output = run_cmd_list(tmp, ["--json", "--state", "closed"])
    data = json.loads(output)
    assert len(data) == 1
    assert data[0]["id"] == "gamma"


def test_json_empty():
    with tempfile.TemporaryDirectory() as tmp:
        output = run_cmd_list(tmp, ["--json", "--state", "nonexistent"])
    data = json.loads(output)
    assert data == []


def test_both_flags_error():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run_cmd_list(tmp, ["--json", "--ids-only"])
            assert False, "should have raised SystemExit"
        except SystemExit as e:
            assert "mutually exclusive" in str(e)


def test_ids_only_empty():
    with tempfile.TemporaryDirectory() as tmp:
        output = run_cmd_list(tmp, ["--ids-only", "--state", "nonexistent"])
    assert output == ""


if __name__ == "__main__":
    tests = [
        test_ids_only,
        test_ids_only_active_filter,
        test_ids_only_state_filter,
        test_ids_only_no_header,
        test_json_output,
        test_json_active_filter,
        test_json_state_filter,
        test_json_empty,
        test_both_flags_error,
        test_ids_only_empty,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS {test.__name__}")
        except Exception as e:
            print(f"  FAIL {test.__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
