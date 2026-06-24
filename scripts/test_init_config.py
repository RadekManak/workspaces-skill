#!/usr/bin/env python3
"""Tests for the init-config command."""
import argparse
import os
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workspace


@pytest.fixture
def tmp_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    with mock.patch.object(workspace, "CONFIG_PATH", config_path):
        yield config_path


@pytest.fixture
def args():
    return argparse.Namespace(force=False, non_interactive=False)


class TestNonInteractive:
    def test_writes_template_defaults(self, tmp_config, args):
        args.non_interactive = True
        workspace.cmd_init_config(args)
        assert tmp_config.exists()
        data = yaml.safe_load(tmp_config.read_text())
        assert data["source_root"] == "~/git"
        assert data["workspace_root"] == "~/workspaces"
        assert data["ledger_root"] == "~/workspaces/_ledger"
        assert data["base_remote"] == "origin"

    def test_preserves_all_template_keys(self, tmp_config, args):
        args.non_interactive = True
        workspace.cmd_init_config(args)
        data = yaml.safe_load(tmp_config.read_text())
        for key in ("base_remote", "base_branch", "push_remote", "jira_base_url",
                     "github_host", "editor_command"):
            assert key in data


class TestInteractive:
    def test_accept_defaults_with_enter(self, tmp_config, args):
        with mock.patch("builtins.input", side_effect=["", ""]):
            workspace.cmd_init_config(args)
        data = yaml.safe_load(tmp_config.read_text())
        assert data["source_root"] == "~/git"
        assert data["workspace_root"] == "~/workspaces"
        assert data["ledger_root"] == "~/workspaces/_ledger"

    def test_custom_source_root(self, tmp_config, args):
        with mock.patch("builtins.input", side_effect=["~/src", ""]):
            workspace.cmd_init_config(args)
        data = yaml.safe_load(tmp_config.read_text())
        assert data["source_root"] == "~/src"
        assert data["workspace_root"] == "~/workspaces"

    def test_custom_workspace_root(self, tmp_config, args):
        with mock.patch("builtins.input", side_effect=["", "~/ws"]):
            workspace.cmd_init_config(args)
        data = yaml.safe_load(tmp_config.read_text())
        assert data["workspace_root"] == "~/ws"
        assert data["ledger_root"] == "~/ws/_ledger"

    def test_custom_both(self, tmp_config, args):
        with mock.patch("builtins.input", side_effect=["/opt/repos", "/opt/ws"]):
            workspace.cmd_init_config(args)
        data = yaml.safe_load(tmp_config.read_text())
        assert data["source_root"] == "/opt/repos"
        assert data["workspace_root"] == "/opt/ws"
        assert data["ledger_root"] == "/opt/ws/_ledger"

    def test_ledger_root_derived_not_prompted(self, tmp_config, args):
        calls = []
        def fake_input(prompt):
            calls.append(prompt)
            return ""
        with mock.patch("builtins.input", side_effect=fake_input):
            workspace.cmd_init_config(args)
        assert len(calls) == 2
        assert "source_root" in calls[0]
        assert "workspace_root" in calls[1]
        assert not any("ledger_root" in c for c in calls)

    def test_preserves_non_prompted_keys(self, tmp_config, args):
        with mock.patch("builtins.input", side_effect=["/custom/src", "/custom/ws"]):
            workspace.cmd_init_config(args)
        data = yaml.safe_load(tmp_config.read_text())
        assert data["base_remote"] == "origin"
        assert data["base_branch"] == "main"
        assert data["push_remote"] == "fork"

    def test_prompt_shows_default_in_brackets(self, tmp_config, args):
        prompts = []
        def fake_input(prompt):
            prompts.append(prompt)
            return ""
        with mock.patch("builtins.input", side_effect=fake_input):
            workspace.cmd_init_config(args)
        assert "[~/git]" in prompts[0]
        assert "[~/workspaces]" in prompts[1]


class TestSourceRootWarning:
    def test_warns_if_source_root_missing(self, tmp_config, args, capsys):
        with mock.patch("builtins.input", side_effect=["/nonexistent/path/xyz", ""]):
            workspace.cmd_init_config(args)
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "/nonexistent/path/xyz" in captured.err
        assert tmp_config.exists()

    def test_no_warning_if_source_root_exists(self, tmp_config, args, tmp_path, capsys):
        real_dir = tmp_path / "repos"
        real_dir.mkdir()
        with mock.patch("builtins.input", side_effect=[str(real_dir), ""]):
            workspace.cmd_init_config(args)
        captured = capsys.readouterr()
        assert "Warning" not in captured.err


class TestForceFlag:
    def test_refuses_overwrite_without_force(self, tmp_config, args, capsys):
        tmp_config.write_text("existing: true\n")
        workspace.cmd_init_config(args)
        captured = capsys.readouterr()
        assert "already exists" in captured.out
        assert yaml.safe_load(tmp_config.read_text()) == {"existing": True}

    def test_overwrites_with_force(self, tmp_config, args):
        tmp_config.write_text("existing: true\n")
        args.force = True
        args.non_interactive = True
        workspace.cmd_init_config(args)
        data = yaml.safe_load(tmp_config.read_text())
        assert "existing" not in data
        assert data["source_root"] == "~/git"
