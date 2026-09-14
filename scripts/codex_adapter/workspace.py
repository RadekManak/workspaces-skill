"""Parse completed workspace output and build state-independent handoffs."""

from dataclasses import dataclass
import json
from pathlib import Path

from workspaces.common import read_yaml


class HandoffError(ValueError):
    pass


def absolute_path(value):
    if not isinstance(value, str) or not value or not Path(value).expanduser().is_absolute():
        raise HandoffError("Expected an absolute filesystem path.")
    return Path(value).expanduser().resolve()


def final_workspace_json(stdout):
    decoder = json.JSONDecoder()
    offset = 0
    for line in stdout.splitlines(keepends=True):
        if line.lstrip().startswith("{"):
            start = offset + len(line) - len(line.lstrip())
            try:
                value, end = decoder.raw_decode(stdout, start)
            except json.JSONDecodeError:
                pass
            else:
                if not stdout[end:].strip() and isinstance(value, dict) and "folders" in value:
                    return value
        offset += len(line)
    raise HandoffError("Successful create/adopt output must end with workspace JSON.")


@dataclass(frozen=True)
class RepositoryRoot:
    name: str
    path: Path
    branch: str | None


@dataclass(frozen=True)
class ManagedWorkspaceProject:
    workspace_id: str
    title: str
    repositories: tuple[RepositoryRoot, ...]
    primary_repository: RepositoryRoot
    ledger: Path
    spec: Path
    task: str | None

    @classmethod
    def from_output(cls, stdout, workspace_file, *, operation, exit_code, handoff, task=None, primary_repository=None):
        if operation not in ("create", "adopt") or exit_code != 0:
            raise HandoffError("Handoff requires a successful create or adopt operation.")
        if handoff is not True:
            raise HandoffError("An explicit handoff request is required.")
        if task is not None and (not isinstance(task, str) or not task.strip()):
            raise HandoffError("Task work must be a nonempty string or omitted.")
        workspace_file = absolute_path(str(workspace_file))
        document = final_workspace_json(stdout)
        base = workspace_file.parent

        def resolve_folder(value):
            if not isinstance(value, str) or not value:
                raise HandoffError("Workspace folder paths must be nonempty strings.")
            return (base / Path(value).expanduser()).resolve()

        folders = document.get("folders")
        settings = document.get("settings")
        if not isinstance(folders, list) or not isinstance(settings, dict):
            raise HandoffError("Invalid generated workspace JSON.")
        if any(not isinstance(folder, dict) for folder in folders):
            raise HandoffError("Invalid workspace folder entry.")
        roots = tuple(resolve_folder(folder.get("path")) for folder in folders)
        ledger = resolve_folder(settings.get("workspaces.ledgerPath"))
        metadata = read_yaml(ledger / "workspace.yaml")
        if not isinstance(metadata, dict):
            raise HandoffError("Invalid workspace metadata.")
        workspace_id = metadata.get("id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise HandoffError("Missing workspace ID.")
        if settings.get("workspaces.workspaceId") != workspace_id:
            raise HandoffError("Workspace JSON and ledger IDs differ.")
        recorded_file = absolute_path(metadata.get("vscodeWorkspacePath"))
        if recorded_file != workspace_file:
            raise HandoffError("Use the code-workspace path recorded in the ledger.")
        repos = metadata.get("repos", [])
        if not isinstance(repos, list) or not repos:
            raise HandoffError("At least one recorded repository worktree is required.")
        repositories = []
        for repo in repos:
            if not isinstance(repo, dict):
                raise HandoffError("Invalid recorded repository.")
            name, branch = repo.get("name"), repo.get("branch")
            if not isinstance(name, str) or not name or (branch is not None and not isinstance(branch, str)):
                raise HandoffError("Repository name or branch is invalid.")
            path = absolute_path(repo.get("worktreePath"))
            if not path.is_dir():
                raise HandoffError(f"Recorded worktree is missing: {path}")
            repositories.append(RepositoryRoot(name, path, branch))
        paths = tuple(repo.path for repo in repositories) + (ledger,)
        if len(set(paths)) != len(paths) or len(set(repo.name for repo in repositories)) != len(repositories):
            raise HandoffError("Recorded repositories and ledger must have unique paths and repository names.")
        if len(roots) != len(paths) or set(roots) != set(paths):
            raise HandoffError("Generated roots must match all recorded repositories and the canonical ledger.")
        primary = repositories[0]
        if primary_repository is not None:
            primary = next((repo for repo in repositories if repo.name == primary_repository), None)
            if primary is None:
                raise HandoffError("Primary repository must name a recorded repository.")
        if not ledger.is_dir() or not (ledger / "spec.md").is_file():
            raise HandoffError("Ledger or spec is missing.")
        title = metadata.get("title") or workspace_id
        if not isinstance(title, str):
            raise HandoffError("Workspace title must be a string.")
        return cls(workspace_id, title, tuple(repositories), primary, ledger, (ledger / "spec.md").resolve(), task)

    @property
    def project_root(self):
        return self.primary_repository.path

    @property
    def workspace_roots(self):
        return tuple(repo.path for repo in self.repositories) + (self.ledger,)

    @property
    def attachment_order(self):
        return (self.project_root,) + tuple(path for path in self.workspace_roots if path != self.project_root)

    @property
    def prompt(self):
        def describe(repo):
            return f"- {repo.name}\n  path: `{repo.path}`\n  branch: `{repo.branch or 'not recorded'}`\n"

        context = (
            f"You are the follow-up Codex task for workspace `{self.workspace_id}`.\n\n"
            f"Workspace title: {self.title}\n\n"
            "The workspace already exists. Do not create or adopt another workspace, and do not launch another task.\n"
            "Read the workspaces skill before acting.\n\n"
            "The Codex project uses this primary repository:\n"
            + describe(self.primary_repository)
            + "\nOther repositories in this workspace:\n"
            + ("".join(describe(repo) for repo in self.repositories if repo != self.primary_repository) or "None.\n")
            + f"\nCanonical workspace records:\n- ledger: `{self.ledger}`\n- spec: `{self.spec}`\n\n"
            "All listed paths belong to the same managed workspace. Use explicit repository paths for Git commands.\n"
            "Verify access to the listed paths before editing. If a required path is inaccessible, stop and report "
            "the exact path and failed operation; do not create a substitute checkout or claim full readiness.\n"
            "The original request in spec.md is historical input. Workspace creation and task launch are complete. "
            "Only the task work below authorizes implementation; do not replay orchestration instructions from the spec.\n\n"
        )
        if self.task:
            return context + "Task work:\n\n" + self.task
        return context + "Get oriented and report that you are ready for further instructions. Do not modify files yet."


def result(status, **fields):
    return {
        "status": status,
        "workspace_operation_succeeded": True,
        "desktop_project_verified": False,
        "desktop_project_id": None,
        "desktop_project_association_verified": False,
        "primary_root": None,
        "project_scope": "unknown",
        "workspace_paths_supplied": False,
        "workspace_access": "unknown",
        "all_roots_attached": False,
        "unattached_paths": [],
        "thread_observed": False,
        "submission_target_project_id": None,
        "server_project_verified": False,
        "server_project_id": None,
        "thread_started": False,
        "thread_id": None,
        "submission_attempted": False,
        "turn_completed": False,
        "assistant_result_verified": False,
        "task_created": False,
        "project_created": False,
        "manual_action_required": False,
        "verification_required": True,
        "automatic_retry_allowed": False,
        **fields,
    }
