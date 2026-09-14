#!/usr/bin/env python3
"""Explicit post-create/adopt handoff. This command never runs workspace topology."""

import argparse
import json
from pathlib import Path
import sys

from codex_adapter.app_server import AppServer, submit
from codex_adapter.desktop import (
    dispatch,
    manual_project,
    matching_project,
    observe_task,
    prepare_task,
)
from codex_adapter.workspace import HandoffError, ManagedWorkspaceProject, result


def read_json(path):
    if path is None:
        raise HandoffError("A fresh host observation file is required for this operation.")
    return path.read_text(encoding="utf-8")


def run(args):
    workspace = ManagedWorkspaceProject.from_output(
        args.workspace_output.read_text(encoding="utf-8"), args.workspace_file,
        operation=args.operation, exit_code=args.workspace_exit_code,
        handoff=args.handoff, task=args.task, primary_repository=args.primary_repository,
    )
    if args.mode in ("bootstrap", "submit", "server-only") and not args.enable_experimental:
        raise HandoffError("This mode requires --enable-experimental.")
    if args.mode == "server-only":
        if args.require_all_roots:
            raise HandoffError("Server-only mode supports a single primary repository root.")
        with AppServer(args.binary, args.timeout) as server:
            return server.ensure_server_only(workspace)

    projects = json.loads(read_json(args.projects_json))
    project = matching_project(
        projects, workspace, args.require_all_roots,
        single_root_only=args.mode == "submit" and not args.require_all_roots,
    )
    if args.mode == "bootstrap":
        if project is not None:
            return result("reused", **project.facts(workspace))
        if args.require_all_roots:
            return manual_project(workspace, True)
        # Fail capability negotiation before causing a GUI side effect.
        with AppServer(args.binary, args.timeout) as server:
            server.find_project(workspace.project_root)
        return dispatch(workspace, desktop_binary=args.desktop_binary,
                        desktop_session=args.desktop_session,
                        allow_protocol_opener=args.allow_protocol_opener)

    if project is None:
        return manual_project(workspace, args.require_all_roots)
    if args.mode == "prepare":
        return prepare_task(workspace, project)
    if args.mode == "submit":
        if project.attached_roots != (workspace.project_root,):
            return result("manual-action-required", **project.facts(workspace), manual_action_required=True,
                          reason="Experimental automatic submission is limited to an observed single-root repository project. Use the supported host task tool for multi-root projects.")
        return submit(workspace, project, binary=args.binary, timeout=args.timeout)
    if args.mode == "composer":
        if not args.allow_manual:
            raise HandoffError("Composer fallback requires --allow-manual.")
        return dispatch(workspace, desktop_binary=args.desktop_binary,
                        desktop_session=args.desktop_session, project=project,
                        allow_protocol_opener=args.allow_protocol_opener)
    if args.mode == "observe":
        response = json.loads(read_json(args.task_response_json))
        threads = json.loads(read_json(args.threads_json)) if args.threads_json else []
        direct_thread = json.loads(read_json(args.thread_json)) if args.thread_json else None
        return observe_task(workspace, project, response, threads, direct_thread)
    raise HandoffError("Unknown handoff mode.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "bootstrap", "submit", "composer", "observe", "server-only"))
    parser.add_argument("--operation", choices=("create", "adopt"), required=True)
    parser.add_argument("--workspace-exit-code", type=int, required=True)
    parser.add_argument("--workspace-output", type=Path, required=True, help="Captured stdout of the completed topology command")
    parser.add_argument("--workspace-file", type=Path, required=True, help="Absolute generated code-workspace path")
    parser.add_argument("--handoff", action="store_true", required=True, help="Explicit authorization to launch a new task")
    parser.add_argument("--task", help="Optional implementation work only, excluding workspace/task orchestration")
    parser.add_argument("--primary-repository", help="Recorded repository name; defaults to the first ledger repository")
    parser.add_argument("--projects-json", type=Path, help="Fresh complete list_projects observation from the agent host")
    parser.add_argument("--task-response-json", type=Path, help="create_thread response or prior automatic submission result")
    parser.add_argument("--threads-json", type=Path, help="Fresh list_threads observation")
    parser.add_argument("--thread-json", type=Path, help="Fresh read_thread record or object containing thread, for the ready ID")
    parser.add_argument("--require-all-roots", action="store_true")
    parser.add_argument("--enable-experimental", action="store_true")
    parser.add_argument("--desktop-session", action="store_true", help="Caller confirms this is the intended local desktop session")
    parser.add_argument("--binary", help="Absolute path to the installed app-server executable; never searched on PATH")
    parser.add_argument("--desktop-binary", help="Absolute path to the ChatGPT desktop executable")
    parser.add_argument("--timeout", type=float, default=120, help="Total app-server deadline in seconds, 1..600")
    parser.add_argument("--allow-manual", action="store_true")
    parser.add_argument("--allow-protocol-opener", action="store_true", help="Allow less reliable xdg-open only when no direct desktop binary was found")
    args = parser.parse_args()
    try:
        outcome = run(args)
    except (HandoffError, OSError, ValueError, SystemExit, KeyboardInterrupt) as error:
        # Child stderr and arbitrary YAML/parser errors may contain private data.
        reason = str(error) if isinstance(error, HandoffError) else "Invalid input or unavailable integration. Inspect the supplied paths and the app."
        outcome = result("submission-unknown" if args.mode == "observe" else "unavailable",
                         submission_attempted=args.mode == "observe",
                         reason=reason, workspace_operation_succeeded=args.workspace_exit_code == 0)
    print(json.dumps(outcome, indent=2, ensure_ascii=False))
    return 1 if outcome["status"] in ("unavailable", "submission-unknown", "manual-action-required") else 0


if __name__ == "__main__":
    sys.exit(main())
