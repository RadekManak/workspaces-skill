"""Tests for workspace adopt writing .workspace-id."""

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workspace


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


def init_git_repo(path):
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def test_adopt_writes_workspace_id():
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(tmp)
        repo_path = Path(tmp) / "my-repo"
        repo_path.mkdir()
        init_git_repo(repo_path)

        with mock.patch.object(workspace, "load_config", return_value=config):
            parser = workspace.build_parser()
            args = parser.parse_args(["adopt", str(repo_path), "--id", "my-ws"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                args.func(args)

        ws_root = Path(config["workspace_root"]) / "my-ws"
        pointer = ws_root / ".workspace-id"
        assert pointer.exists(), f".workspace-id should exist at {pointer}"
        assert pointer.read_text(encoding="utf-8").strip() == "my-ws"


def test_adopt_creates_workspace_root_dir():
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(tmp)
        repo_path = Path(tmp) / "my-repo"
        repo_path.mkdir()
        init_git_repo(repo_path)

        ws_root = Path(config["workspace_root"]) / "test-ws"
        assert not ws_root.exists()

        with mock.patch.object(workspace, "load_config", return_value=config):
            parser = workspace.build_parser()
            args = parser.parse_args(["adopt", str(repo_path), "--id", "test-ws"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                args.func(args)

        assert ws_root.exists(), f"workspace root dir should be created at {ws_root}"


def test_adopt_does_not_write_workspace_id_into_external_worktree():
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(tmp)
        repo_path = Path(tmp) / "external-repo"
        repo_path.mkdir()
        init_git_repo(repo_path)

        with mock.patch.object(workspace, "load_config", return_value=config):
            parser = workspace.build_parser()
            args = parser.parse_args(["adopt", str(repo_path), "--id", "ext-ws"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                args.func(args)

        pointer_in_repo = repo_path / ".workspace-id"
        assert not pointer_in_repo.exists(), (
            ".workspace-id should NOT be written into the adopted repo path "
            "when it lives outside workspace_root/<id>/"
        )


def test_adopt_doctor_no_workspace_pointer_warning():
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(tmp)
        repo_path = Path(tmp) / "my-repo"
        repo_path.mkdir()
        init_git_repo(repo_path)

        with mock.patch.object(workspace, "load_config", return_value=config):
            parser = workspace.build_parser()
            args = parser.parse_args(["adopt", str(repo_path), "--id", "doc-ws"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                args.func(args)

        issues = workspace.doctor_workspace(config, "doc-ws")
        codes = [i["code"] for i in issues]
        assert "workspace-pointer" not in codes, (
            f"doctor should not report workspace-pointer warning after adopt, got: {issues}"
        )


if __name__ == "__main__":
    tests = [
        test_adopt_writes_workspace_id,
        test_adopt_creates_workspace_root_dir,
        test_adopt_does_not_write_workspace_id_into_external_worktree,
        test_adopt_doctor_no_workspace_pointer_warning,
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
