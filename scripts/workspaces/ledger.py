import datetime as dt
import json
import os
from pathlib import Path

from .common import now, read_yaml, relative_or_absolute, slug, template, write_yaml


class WorkspaceLedger:
    def __init__(self, config):
        self.config = config

    def ledger_dir(self, workspace_id):
        return Path(self.config["ledger_root"]) / "workspaces" / workspace_id

    def workspace_yaml_path(self, workspace_id):
        return self.ledger_dir(workspace_id) / "workspace.yaml"

    def workspace_root_path(self, workspace_id):
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
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        pid = lock.get("pid")
        if not isinstance(pid, int):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def cleanup_runs_dir(self):
        return Path(self.config["ledger_root"]) / "runs"

    def load(self, workspace_id):
        path = self.workspace_yaml_path(workspace_id)
        if not path.exists():
            raise SystemExit(f"Workspace not found: {workspace_id}")
        return read_yaml(path)

    def iter_workspaces(self):
        workspaces = Path(self.config["ledger_root"]) / "workspaces"
        if not workspaces.exists():
            return
        for meta in sorted(workspaces.glob("*/workspace.yaml")):
            data = read_yaml(meta)
            if data.get("id"):
                yield data

    def save(self, data):
        data["updatedAt"] = now()
        if data.get("cleanupStatus") == "done":
            old_path = data.get("vscodeWorkspacePath")
            if old_path and Path(old_path).exists():
                Path(old_path).unlink()
            data.pop("vscodeWorkspacePath", None)
        else:
            data["vscodeWorkspacePath"] = str(self.write_vscode_workspace(data))
        write_yaml(self.workspace_yaml_path(data["id"]), data)

    def build_vscode_workspace(self, data):
        workspace_id = data["id"]
        path = self.vscode_workspace_path(workspace_id)
        workspace_root = path.parent

        folders = [
            {
                "name": f"{workspace_id} ledger",
                "path": relative_or_absolute(self.ledger_dir(workspace_id), workspace_root),
            }
        ]
        seen_paths = {str(Path(self.ledger_dir(workspace_id)).resolve())}
        for repo in data.get("repos", []):
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

    def write_vscode_workspace(self, data):
        path, payload = self.build_vscode_workspace(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
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
            return read_yaml(meta)

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
        self.save(data)
        self.append_note(workspace_id, f"Created workspace `{workspace_id}`.")
        return data
