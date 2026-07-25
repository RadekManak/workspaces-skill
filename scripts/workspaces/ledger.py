import datetime as dt
import fcntl
import json
import os
from pathlib import Path

from .common import (
    now,
    read_json,
    read_yaml,
    relative_or_absolute,
    slug,
    template,
    validate_workspace_id,
    warn,
    write_json,
    write_yaml,
)
from .sandcastle_state import maybe_invalidate_plan_for_repo_change
from .workspace_model import Workspace


class WorkspaceLedger:
    def __init__(self, config):
        self.config = config

    def ledger_dir(self, workspace_id):
        workspace_id = validate_workspace_id(workspace_id)
        return Path(self.config["ledger_root"]) / "workspaces" / workspace_id

    def workspace_yaml_path(self, workspace_id):
        return self.ledger_dir(workspace_id) / "workspace.yaml"

    def workspace_root_path(self, workspace_id):
        workspace_id = validate_workspace_id(workspace_id)
        return Path(self.config["workspace_root"]) / workspace_id

    def vscode_workspace_path(self, workspace_id):
        return self.workspace_root_path(workspace_id) / f"{workspace_id}.code-workspace"

    def issues_dir(self, workspace_id):
        return self.ledger_dir(workspace_id) / "issues"

    def issue_path(self, workspace_id, issue_id):
        return self.issues_dir(workspace_id) / f"{slug(issue_id)}.md"

    def runs_dir(self, workspace_id):
        return self.ledger_dir(workspace_id) / "runs"

    def sandcastle_lock_path(self, workspace_id):
        return self.runs_dir(workspace_id) / "active-sandcastle-run.json"

    def sandcastle_run_active(self, workspace_id):
        lock_path = self.sandcastle_lock_path(workspace_id)
        if not lock_path.exists():
            return False
        # A lock we cannot read tells us nothing about a running process, so it
        # is reported as inactive (otherwise the workspace would be permanently
        # deadlocked) -- but never silently, since it also means a genuinely
        # running Sandcastle execution would no longer be detected.
        try:
            lock = read_json(lock_path)
        except json.JSONDecodeError as error:
            warn(f"Ignoring unreadable Sandcastle lock {lock_path}: {error}")
            return False
        pid = lock.get("pid")
        if not isinstance(pid, int):
            warn(f"Ignoring Sandcastle lock without a valid pid: {lock_path}")
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def acquire_sandcastle_lock(self, workspace_id, payload):
        path = self.sandcastle_lock_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = {
            "workspace": workspace_id,
            "createdAt": now(),
            "pid": os.getpid(),
            "planPath": payload.get("planPath"),
            "targetedIssueIds": payload.get("targetedIssueIds"),
        }
        # A lock file surviving a crash/kill -9/OOM/reboot must not permanently
        # deadlock the workspace: if the PID inside it is dead, remove the stale
        # file and retry once (still via O_EXCL, so a genuinely concurrent
        # acquirer that wins the retry race still correctly wins the lock).
        for attempt in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as error:
                if attempt == 0 and self._recover_dead_sandcastle_lock(workspace_id, path):
                    continue
                raise SystemExit(f"Sandcastle run already active: {path}") from error
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(lock, handle, indent=2)
                handle.write("\n")
            return path
        raise SystemExit(f"Sandcastle run already active: {path}")

    def _recover_dead_sandcastle_lock(self, workspace_id, path):
        """Remove `path` iff it's still a dead-PID lock, but only after winning a
        secondary "recovery" lock that serializes concurrent recovery attempts.

        Without this, two processes that both observe the same dead lock could each
        decide independently to recover it: whichever unlinks second would actually be
        deleting the OTHER process's freshly-created, live lock (not the original dead
        one it itself observed), letting both believe they hold the workspace's run
        lock -- so "check dead, then unlink" must never happen concurrently for the
        same lock.

        The recovery lock itself must be *unconditionally* crash-recoverable: an
        earlier version of this used a second `O_CREAT|O_EXCL` marker file, but that
        file carried no PID/ownership info, so a process that crashed while holding it
        left an orphaned marker with no way to tell "someone is legitimately recovering
        right now" from "a dead process left this behind" -- permanently deadlocking
        every future recovery attempt (exactly the class of bug this whole mechanism
        exists to prevent, just moved one file over). Re-deriving a second PID-based
        check-then-act recovery layer for *that* marker would just reintroduce the same
        TOCTOU race one level up, recursively. Using `flock()` instead sidesteps the
        whole problem: it is a kernel-mediated mutex tied to the open file descriptor,
        so at most one process can ever hold it at a time (real mutual exclusion, not a
        userspace check-then-act pattern), AND the kernel unconditionally releases it
        when the holding descriptor is closed for any reason -- including the holding
        process crashing, being `kill -9`'d, OOM-killed, or the machine rebooting --
        so there is no file-content/ownership state that can ever be left "orphaned."
        """
        recovery_path = path.with_name(path.name + ".recovery-lock")
        fd = os.open(recovery_path, os.O_CREAT | os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Someone else is actively recovering this exact lock right now;
                # don't race them for it.
                return False
            # Re-check liveness now, inside the critical section: another process may
            # have already recovered and replaced `path` with its own live lock while
            # this process was waiting to acquire the flock above.
            if self.sandcastle_run_active(workspace_id):
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return True
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def release_sandcastle_lock(self, path):
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass

    def cleanup_runs_dir(self):
        return Path(self.config["ledger_root"]) / "runs"

    def load(self, workspace_id):
        path = self.workspace_yaml_path(workspace_id)
        if not path.exists():
            raise SystemExit(f"Workspace not found: {workspace_id}")
        return Workspace(read_yaml(path))

    def iter_workspaces(self):
        workspaces = Path(self.config["ledger_root"]) / "workspaces"
        if not workspaces.exists():
            return
        for meta in sorted(workspaces.glob("*/workspace.yaml")):
            data = read_yaml(meta)
            if not data.get("id"):
                warn(f"Skipping workspace metadata without an `id`: {meta}")
                continue
            yield Workspace(data)

    def save(self, workspace):
        workspace_id = workspace.id
        old_workspace = None
        path = self.workspace_yaml_path(workspace_id)
        if path.exists():
            old_workspace = Workspace(read_yaml(path))
        workspace.set_updated_at(now())
        if workspace.cleanup_status == "done":
            old_path = workspace.vscode_workspace_path
            if old_path and Path(old_path).exists():
                Path(old_path).unlink()
            workspace.clear_vscode_workspace_path()
        else:
            workspace.set_vscode_workspace_path(str(self.write_vscode_workspace(workspace)))
        write_yaml(path, workspace.to_dict())
        if old_workspace is not None:
            maybe_invalidate_plan_for_repo_change(self, workspace_id, old_workspace, workspace)

    def build_vscode_workspace(self, workspace):
        workspace_id = workspace.id
        path = self.vscode_workspace_path(workspace_id)
        workspace_root = path.parent

        folders = [
            {
                "name": f"{workspace_id} ledger",
                "path": relative_or_absolute(self.ledger_dir(workspace_id), workspace_root),
            }
        ]
        seen_paths = {str(Path(self.ledger_dir(workspace_id)).resolve())}
        for repo in workspace.repos:
            repo_path = repo.get("worktreePath")
            if not repo_path:
                continue
            resolved = str(Path(repo_path).expanduser().resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            folders.append(
                {
                    "name": repo.get("name") or Path(repo_path).name,
                    "path": relative_or_absolute(repo_path, workspace_root),
                }
            )

        payload = {
            "folders": folders,
            "settings": {
                "workspaces.workspaceId": workspace_id,
                "workspaces.ledgerPath": relative_or_absolute(
                    self.ledger_dir(workspace_id),
                    workspace_root,
                ),
            },
        }
        return path, payload

    def write_vscode_workspace(self, workspace):
        path, payload = self.build_vscode_workspace(workspace)
        write_json(path, payload)
        return path

    def append_note(self, workspace_id, text):
        path = self.ledger_dir(workspace_id) / "notes.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(template("notes.md"), encoding="utf-8")
        stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {stamp}\n{text.strip()}\n")

    def ensure(self, workspace_id, title=None, state="captured", input_text=None):
        root = self.ledger_dir(workspace_id)
        meta = root / "workspace.yaml"
        if meta.exists():
            return Workspace(read_yaml(meta))

        created = now()
        data = {
            "id": workspace_id,
            "title": title or workspace_id,
            "state": state,
            "createdAt": created,
            "updatedAt": created,
            "repos": [],
            "links": {"jira": [], "githubPrs": []},
        }
        root.mkdir(parents=True, exist_ok=True)
        (root / "runs").mkdir(exist_ok=True)
        (root / "issues").mkdir(exist_ok=True)
        spec_input = input_text or "TBD."
        (root / "spec.md").write_text(
            template("spec.md").format(id=workspace_id, title=data["title"], input=spec_input),
            encoding="utf-8",
        )
        (root / "notes.md").write_text(template("notes.md"), encoding="utf-8")
        workspace = Workspace(data)
        self.save(workspace)
        self.append_note(workspace_id, f"Created workspace `{workspace_id}`.")
        return workspace
