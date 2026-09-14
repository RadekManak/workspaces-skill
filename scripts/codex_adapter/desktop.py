"""Desktop observations come from the agent host, never from subprocess MCP calls."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import NewType
from urllib.parse import urlencode

from .workspace import HandoffError, absolute_path, result


DesktopProjectId = NewType("DesktopProjectId", str)


@dataclass(frozen=True)
class DesktopProject:
    id: DesktopProjectId
    attached_roots: tuple[Path, ...]

    def facts(self, workspace):
        return {
            "desktop_project_id": self.id,
            "desktop_project_verified": True,
            "primary_root": str(workspace.project_root),
            "attached_roots": [str(path) for path in self.attached_roots],
            "project_scope": "all-roots" if set(workspace.workspace_roots) <= set(self.attached_roots) else "primary-only",
            "all_roots_attached": set(workspace.workspace_roots) <= set(self.attached_roots),
            "unattached_paths": [str(path) for path in workspace.workspace_roots if path not in self.attached_roots],
            "project_status": "reused",
        }


def project_roots(record):
    values = record.get("roots", [record.get("path")])
    if not isinstance(values, list) or not values:
        raise HandoffError("Project roots must be a nonempty array.")
    return tuple(absolute_path(value.get("path") if isinstance(value, dict) else value) for value in values)


def matching_project(payload, workspace, require_all_roots=False, *, single_root_only=False):
    records = payload.get("projects") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise HandoffError("Expected the host's complete desktop projects array.")
    matches = []
    for record in records:
        if not isinstance(record, dict):
            raise HandoffError("Invalid desktop project record.")
        if record.get("projectKind") != "local":
            continue
        if record.get("namespace", "desktop") != "desktop":
            raise HandoffError("A server project cannot enter the desktop adapter.")
        project_id = record.get("projectId")
        if not isinstance(project_id, str) or not project_id:
            raise HandoffError("Desktop project observations require projectId.")
        roots = project_roots(record)
        if require_all_roots:
            matches_root_policy = roots == workspace.attachment_order
        elif single_root_only:
            matches_root_policy = roots == (workspace.project_root,)
        else:
            matches_root_policy = roots[0] == workspace.project_root and set(roots) <= set(workspace.workspace_roots)
        if matches_root_policy:
            matches.append(DesktopProject(DesktopProjectId(project_id), roots))
    matches.sort(key=lambda project: len(project.attached_roots))
    if len(matches) > 1 and len(matches[0].attached_roots) == len(matches[1].attached_roots):
        raise HandoffError("Multiple desktop projects match; resolve the ambiguity in the app.")
    return matches[0] if matches else None


def manual_project(workspace, require_all_roots=False):
    roots = workspace.attachment_order if require_all_roots else (workspace.project_root,)
    ordered = "\n".join(f"{index}. {root}" for index, root in enumerate(roots, 1))
    return result(
        "manual-action-required",
        manual_action_required=True,
        reason="No desktop project with the required ordered roots was observed.",
        instructions=(
            "Open the Codex desktop project menu and inspect existing projects. "
            f"Edit the matching project, or choose New project and name it '{workspace.workspace_id}'. "
            f"Set these folders in order, with the first folder primary:\n{ordered}\n"
            "Save, then obtain a fresh list_projects observation before starting a task."
        ),
    )


def prepare_task(workspace, project):
    return result(
        "host-action-required",
        **project.facts(workspace),
        tool="mcp__codex_app__create_thread",
        arguments={
            "target": {
                "type": "project",
                "projectId": project.id,
                "environment": {"type": "local"},
            },
            "title": workspace.title,
            "prompt": workspace.prompt,
        },
        reason="The agent host must call the exposed tool once. No task has been submitted by this helper.",
    )


def observed_thread_id(record):
    value = record.get("threadId") or record.get("id")
    return value if isinstance(value, str) and value else None


def observe_task(workspace, project, response, threads, direct_thread=None):
    if not isinstance(response, dict):
        raise HandoffError("Expected the create_thread response object.")
    facts = {**project.facts(workspace), "submission_attempted": True,
             "workspace_paths_supplied": True, "submission_target_project_id": project.id}
    automatic = response.get("status") in (
        "auto-submitted", "auto-submitted-desktop-association-unknown",
    )
    if automatic:
        if response.get("desktop_project_id") != project.id or not all(
            response.get(field) is True
            for field in ("server_project_verified", "turn_completed", "assistant_result_verified")
        ):
            raise HandoffError("Automatic result does not match the observed desktop project or completed execution.")
    thread_id = response.get("thread_id") if automatic else response.get("threadId")
    client_id = response.get("clientThreadId")
    if not thread_id and isinstance(client_id, str) and client_id:
        return result("handoff-pending", **facts, client_thread_id=client_id,
                      reason="Wait for a ready threadId. Do not pass clientThreadId to thread tools.")
    if not isinstance(thread_id, str) or not thread_id:
        return result("submission-unknown", **facts,
                      reason="Task creation did not return a usable ID. Inspect the app; do not automatically retry.")
    records = threads.get("threads") if isinstance(threads, dict) else threads
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise HandoffError("Expected the host's threads array.")
    records = list(records)
    if direct_thread is not None:
        if not isinstance(direct_thread, dict):
            raise HandoffError("Expected a direct read_thread record.")
        record = direct_thread.get("thread", direct_thread)
        if not isinstance(record, dict) or observed_thread_id(record) != thread_id:
            raise HandoffError("Direct thread observation does not match the ready thread ID.")
        records.append(record)
    observed = any(observed_thread_id(record) == thread_id for record in records)
    associated = any(observed_thread_id(record) == thread_id and record.get("projectId") == project.id for record in records)
    facts["thread_observed"] = observed
    if automatic:
        return {
            **response,
            **facts,
            "status": "auto-submitted" if associated else "auto-submitted-desktop-association-unknown",
            "desktop_project_association_verified": associated,
            "verification_required": not associated,
            "reason": None if associated else "Execution completed, but desktop project association remains unobserved. Do not resubmit.",
        }
    return result(
        "handoff-created",
        **facts,
        thread_id=thread_id,
        thread_started=True,
        task_created=True,
        desktop_project_association_verified=associated,
        verification_required=not associated,
        reason=None if associated else "A ready task ID exists, but desktop project association is not yet observed. Do not resubmit.",
    )


def executable(explicit, candidates):
    if explicit is not None:
        candidates = [absolute_path(explicit)]
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    raise HandoffError("No compatible installed executable found. Supply its absolute path explicitly.")


def linux_only():
    if not sys.platform.startswith("linux"):
        raise HandoffError("The experimental desktop integration supports Linux only.")


def dispatch(workspace, *, desktop_binary, desktop_session, project=None, allow_protocol_opener=False):
    linux_only()
    if not desktop_session:
        raise HandoffError("Explicit confirmation of a local desktop session is required for GUI dispatch.")
    params = {"path": str(workspace.project_root)}
    if project is not None:
        params = {"projectId": str(project.id), **params, "prompt": workspace.prompt}
    uri = "codex://threads/new?" + urlencode(params)
    method = "desktop-binary"
    try:
        binary = executable(desktop_binary, (
            Path(root) / "ChatGPT" for root in
            ("/usr/lib/chatgpt", "/usr/lib/ChatGPT", "/opt/chatgpt", "/opt/ChatGPT")
        ))
    except HandoffError:
        if desktop_binary is not None or not allow_protocol_opener:
            raise
        binary = shutil.which("xdg-open")
        if binary is None:
            raise HandoffError("No direct desktop executable or xdg-open is available.")
        method = "protocol-opener"
    # The desktop outlives this helper. Its exit status cannot verify dispatch.
    subprocess.Popen([str(binary), uri], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return result(
        "dispatch-only" if project is not None else "desktop-observation-required",
        **(project.facts(workspace) if project is not None else {}),
        dispatch_method=method,
        dispatch_reliability="less-reliable" if method == "protocol-opener" else "unsupported",
        manual_action_required=project is not None,
        instructions=(
            "Review the prefilled composer and submit it yourself. Then observe its task and project association."
            if project is not None else
            "Bootstrap dispatched without a prompt. Call desktop list_projects again, then submit with that fresh observation."
        ),
    )
