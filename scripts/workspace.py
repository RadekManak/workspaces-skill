#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    print("PyYAML is required: python3 -m pip install pyyaml", file=sys.stderr)
    sys.exit(2)


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = Path("~/workspaces/config.yaml").expanduser()
CONFIG_PATH = Path(os.environ.get("WORKSPACES_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()

DEFAULT_CONFIG = {
    "source_root": "~/git",
    "workspace_root": "~/workspaces",
    "ledger_root": "~/workspaces/_ledger",
    "base_remote": "origin",
    "base_branch": "main",
    "push_remote": "fork",
    "jira_base_url": "",
    "github_host": "github.com",
    "editor_command": "code",
}

DONE_STATES = {"closed", "dev-complete"}
STATE_ORDER = {
    "review-feedback": 0,
    "blocked": 1,
    "needs-clarification": 2,
    "user-review": 3,
    "in-progress": 4,
    "pr-review": 5,
    "scoped": 6,
    "captured": 7,
    "dev-complete": 8,
    "closed": 9,
}

ISSUE_TYPES = {"AFK", "HITL"}
ISSUE_DONE_STATUSES = {"merged", "done", "skipped"}
ISSUE_ACTIVE_STATUSES = {"planned", "ready", "needs-fix"}
ISSUE_STATUS_ORDER = {
    "blocked-hitl": 0,
    "failed": 1,
    "needs-fix": 2,
    "reviewing": 3,
    "running": 4,
    "ready": 5,
    "planned": 6,
    "merged": 7,
    "done": 8,
    "skipped": 9,
}


def now():
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def expand(value):
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value))
    return value


def load_config():
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        config.update(loaded)
    for key in ("source_root", "workspace_root", "ledger_root"):
        config[key] = expand(config[key])
    return config


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=False)


def read_yaml(path):
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def slug(value):
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = text.strip("-")
    return text or "issue"


def run(cmd, cwd=None, check=True, capture=True):
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise SystemExit(f"Command failed: {' '.join(cmd)}\n{stderr}")
    return (proc.stdout or "").strip()


def ledger_dir(config, workspace_id):
    return Path(config["ledger_root"]) / "workspaces" / workspace_id


def workspace_yaml_path(config, workspace_id):
    return ledger_dir(config, workspace_id) / "workspace.yaml"


def workspace_root_path(config, workspace_id):
    return Path(config["workspace_root"]) / workspace_id


def vscode_workspace_path(config, workspace_id):
    return workspace_root_path(config, workspace_id) / f"{workspace_id}.code-workspace"


def issues_dir(config, workspace_id):
    return ledger_dir(config, workspace_id) / "issues"


def issue_path(config, workspace_id, issue_id):
    return issues_dir(config, workspace_id) / f"{slug(issue_id)}.md"


def runs_dir(config, workspace_id):
    return ledger_dir(config, workspace_id) / "runs"


def cleanup_runs_dir(config):
    return Path(config["ledger_root"]) / "runs"


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


def load_workspace(config, workspace_id):
    return WorkspaceLedger(config).load(workspace_id)


def iter_workspaces(config):
    yield from WorkspaceLedger(config).iter_workspaces()


def save_workspace(config, data):
    WorkspaceLedger(config).save(data)


def relative_or_absolute(path, base):
    path = Path(path).expanduser()
    base = Path(base).expanduser()
    try:
        return os.path.relpath(path.resolve(), base.resolve())
    except FileNotFoundError:
        return os.path.relpath(path.absolute(), base.absolute())


def write_vscode_workspace(config, data):
    return WorkspaceLedger(config).write_vscode_workspace(data)


def build_vscode_workspace(config, data):
    return WorkspaceLedger(config).build_vscode_workspace(data)


def template(name):
    return (SKILL_ROOT / "templates" / name).read_text(encoding="utf-8")


def append_note(config, workspace_id, text):
    WorkspaceLedger(config).append_note(workspace_id, text)


def ensure_workspace(config, workspace_id, title=None, state="captured", input_text=None):
    return WorkspaceLedger(config).ensure(workspace_id, title, state, input_text)


def sandcastle_branch(workspace_id, issue_id):
    return f"sandcastle/{slug(workspace_id)}/{slug(issue_id)}"


def split_frontmatter(text):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw_meta = text[4:end]
    body = text[end + len("\n---\n") :]
    return yaml.safe_load(raw_meta) or {}, body


def issue_body_from_args(args):
    parts = []
    if getattr(args, "body", None):
        parts.append(args.body.strip())
    else:
        parts.append("## What to build\n\nTBD.\n")
    acceptance = getattr(args, "acceptance", None) or []
    if acceptance:
        lines = ["## Acceptance criteria", ""]
        lines.extend(f"- [ ] {item}" for item in acceptance)
        parts.append("\n".join(lines))
    return "\n\n".join(part for part in parts if part).strip() + "\n"


def normalize_issue_meta(config, workspace_id, issue_id, meta):
    issue_id = str(meta.get("id") or issue_id)
    issue_type = str(meta.get("type") or "AFK").upper()
    if issue_type not in ISSUE_TYPES:
        raise SystemExit(f"Invalid issue type for {issue_id}: {issue_type}")
    blocked_by = meta.get("blockedBy") or []
    if isinstance(blocked_by, str):
        blocked_by = [blocked_by]
    status = str(meta.get("status") or "planned")
    created = meta.get("createdAt") or now()
    normalized = {
        "id": issue_id,
        "title": meta.get("title") or issue_id,
        "type": issue_type,
        "status": status,
        "blockedBy": [str(item) for item in blocked_by],
        "branch": meta.get("branch") or sandcastle_branch(workspace_id, issue_id),
        "reviewStatus": meta.get("reviewStatus") or "pending",
        "createdAt": created,
        "updatedAt": now(),
    }
    for key, value in meta.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def write_issue(config, workspace_id, issue_id, meta, body):
    meta = normalize_issue_meta(config, workspace_id, issue_id, meta)
    path = issue_path(config, workspace_id, meta["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=False).strip()
    path.write_text(f"---\n{frontmatter}\n---\n{body.strip()}\n", encoding="utf-8")
    return path, meta


def read_issue(path):
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    issue_id = meta.get("id") or path.stem
    return {"path": path, "meta": meta, "body": body, "id": str(issue_id)}


def iter_issues(config, workspace_id):
    root = issues_dir(config, workspace_id)
    if not root.exists():
        return []
    issues = [read_issue(path) for path in sorted(root.glob("*.md"))]
    issues.sort(
        key=lambda item: (
            ISSUE_STATUS_ORDER.get(item["meta"].get("status", ""), 99),
            str(item["meta"].get("id", "")),
        )
    )
    return issues


def issue_index(config, workspace_id):
    return {issue["id"]: issue for issue in iter_issues(config, workspace_id)}


def blockers_complete(index, issue):
    for blocker_id in issue["meta"].get("blockedBy") or []:
        blocker = index.get(str(blocker_id))
        if not blocker:
            return False
        if blocker["meta"].get("status") not in ISSUE_DONE_STATUSES:
            return False
    return True


def ready_issues(config, workspace_id):
    index = issue_index(config, workspace_id)
    ready = []
    blocked_hitl = []
    for issue in index.values():
        meta = issue["meta"]
        status = meta.get("status")
        issue_type = str(meta.get("type") or "AFK").upper()
        if status not in ISSUE_ACTIVE_STATUSES:
            continue
        if not blockers_complete(index, issue):
            continue
        if issue_type == "AFK":
            ready.append(issue)
        elif issue_type == "HITL":
            blocked_hitl.append(issue)
    ready.sort(key=lambda item: str(item["meta"].get("id", "")))
    blocked_hitl.sort(key=lambda item: str(item["meta"].get("id", "")))
    return ready, blocked_hitl


def select_repo(data, repo_name=None):
    repos = data.get("repos", [])
    if repo_name:
        for repo in repos:
            if repo.get("name") == repo_name:
                return repo
        raise SystemExit(f"Repo not found in workspace: {repo_name}")
    if not repos:
        raise SystemExit("Workspace has no repos. Add or adopt one before running Sandcastle.")
    if len(repos) > 1:
        names = ", ".join(repo.get("name") or "?" for repo in repos)
        raise SystemExit(f"Workspace has multiple repos ({names}). Pass --repo.")
    return repos[0]


def issue_payload(issue):
    meta = issue["meta"]
    return {
        "id": meta.get("id"),
        "title": meta.get("title"),
        "type": meta.get("type"),
        "status": meta.get("status"),
        "blockedBy": meta.get("blockedBy") or [],
        "branch": meta.get("branch"),
        "reviewStatus": meta.get("reviewStatus"),
        "path": str(issue["path"]),
        "body": issue["body"].strip(),
    }


def write_sandcastle_plan(config, data, repo, ready, hitl, limit=None):
    selected = ready[:limit] if limit else ready
    workspace_id = data["id"]
    created = now()
    payload = {
        "version": 1,
        "createdAt": created,
        "workspace": {
            "id": workspace_id,
            "title": data.get("title"),
            "state": data.get("state"),
            "ledgerDir": str(ledger_dir(config, workspace_id)),
            "specPath": str(ledger_dir(config, workspace_id) / "spec.md"),
        },
        "repo": {
            "name": repo.get("name"),
            "worktreePath": repo.get("worktreePath"),
            "branch": repo.get("branch"),
            "remote": repo.get("remote"),
        },
        "issues": [issue_payload(issue) for issue in selected],
        "hitl": [issue_payload(issue) for issue in hitl],
    }
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    path = runs_dir(config, workspace_id) / f"sandcastle-plan-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path, payload


def sandcastle_lock_path(config, workspace_id):
    return runs_dir(config, workspace_id) / "active-sandcastle-run.json"


def acquire_sandcastle_lock(config, workspace_id, payload):
    path = sandcastle_lock_path(config, workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = {
        "workspace": workspace_id,
        "createdAt": now(),
        "pid": os.getpid(),
        "planPath": payload.get("planPath"),
    }
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise SystemExit(f"Sandcastle run already active: {path}") from error
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(lock, f, indent=2)
        f.write("\n")
    return path


def release_sandcastle_lock(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def set_issue_status(
    config,
    workspace_id,
    issue_id,
    status,
    review_status=None,
    branch=None,
    extra=None,
):
    path = issue_path(config, workspace_id, issue_id)
    if not path.exists():
        raise SystemExit(f"Issue not found: {issue_id}")
    issue = read_issue(path)
    meta = dict(issue["meta"])
    old_status = meta.get("status")
    meta["status"] = status
    if review_status:
        meta["reviewStatus"] = review_status
    if branch:
        meta["branch"] = branch
    for key, value in (extra or {}).items():
        if key not in {"id", "note"}:
            meta[key] = value
    path, meta = write_issue(config, workspace_id, issue_id, meta, issue["body"])
    return path, meta, old_status


def maybe_mark_user_review(config, workspace_id):
    issues = iter_issues(config, workspace_id)
    if not issues:
        return False
    if any(issue["meta"].get("status") not in ISSUE_DONE_STATUSES for issue in issues):
        return False
    data = load_workspace(config, workspace_id)
    if data.get("state") in {"closed", "dev-complete", "user-review"}:
        return False
    old_state = data.get("state")
    data["state"] = "user-review"
    save_workspace(config, data)
    append_note(
        config,
        workspace_id,
        f"State changed from `{old_state}` to `user-review` because all local issues are complete.",
    )
    return True


def apply_sandcastle_result(config, workspace_id, result_path):
    path = Path(result_path).expanduser()
    if not path.exists():
        raise SystemExit(f"Sandcastle result not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    updates = payload.get("issues") or payload.get("updates") or []
    if not isinstance(updates, list):
        raise SystemExit("Sandcastle result field `issues` must be a list")

    applied = []
    for update in updates:
        if not isinstance(update, dict):
            raise SystemExit("Each Sandcastle issue result must be an object")
        issue_id = update.get("id")
        status = update.get("status")
        if not issue_id or not status:
            raise SystemExit("Each Sandcastle issue result needs `id` and `status`")
        extra = {
            key: value
            for key, value in update.items()
            if key not in {"id", "status", "reviewStatus", "branch", "note"}
        }
        issue_path_, meta, old_status = set_issue_status(
            config,
            workspace_id,
            str(issue_id),
            str(status),
            review_status=update.get("reviewStatus"),
            branch=update.get("branch"),
            extra=extra,
        )
        applied.append((meta, old_status, update.get("note"), issue_path_))

    data = load_workspace(config, workspace_id)
    save_workspace(config, data)
    append_note(
        config,
        workspace_id,
        f"Reconciled Sandcastle result `{path}` with {len(applied)} issue update(s).",
    )
    for meta, old_status, note, _ in applied:
        detail = note or f"Issue `{meta['id']}` status changed from `{old_status}` to `{meta['status']}`."
        append_note(config, workspace_id, detail)
    maybe_mark_user_review(config, workspace_id)
    return applied


def init_sandcastle_runner(repo, force=False):
    worktree = repo.get("worktreePath")
    if not worktree or not Path(worktree).exists():
        raise SystemExit(f"Repo worktree missing on disk: {worktree}")
    source = SKILL_ROOT / "templates" / "sandcastle"
    target = Path(worktree) / ".sandcastle"
    target.mkdir(parents=True, exist_ok=True)

    written = []
    for item in sorted(source.iterdir()):
        if not item.is_file():
            continue
        dest = target / item.name
        if dest.exists() and not force:
            raise SystemExit(f"Refusing to overwrite existing file: {dest}. Pass --force.")
        shutil.copyfile(item, dest)
        written.append(dest)
    return written


def path_is_under(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


class CleanupSafety:
    def __init__(self, config):
        self.config = config
        self.ledger = WorkspaceLedger(config)

    def workspace_root_extra_paths(self, data):
        root = self.ledger.workspace_root_path(data["id"])
        if not root.exists():
            return []
        expected = {".workspace-id", f"{data['id']}.code-workspace"}
        for repo in data.get("repos", []):
            path = repo.get("worktreePath")
            if path and path_is_under(path, root):
                rel = Path(path).resolve().relative_to(root.resolve())
                if rel.parts:
                    expected.add(rel.parts[0])
        return [
            str(item)
            for item in sorted(root.iterdir())
            if item.name not in expected
        ]

    def workspace_candidates(self, workspace_ids):
        return [self.workspace_candidate(workspace_id) for workspace_id in workspace_ids]

    def workspace_candidate(self, workspace_id):
        data = self.ledger.load(workspace_id)
        root = self.ledger.workspace_root_path(workspace_id)
        repo_rows = []
        dirty_repos = []
        missing_repos = []
        non_linked_repos = []
        external_repos = []
        total_ahead = 0
        for repo in data.get("repos", []):
            path = repo.get("worktreePath")
            exists = bool(path and Path(path).exists())
            row = {
                "name": repo.get("name"),
                "path": path,
                "branch": repo.get("branch"),
                "exists": exists,
                "dirty": None,
                "aheadOfBase": None,
                "linkedWorktree": None,
                "underWorkspaceRoot": None,
            }
            if exists:
                info = git_info(path, self.config)
                row["branch"] = info.get("branch")
                row["dirty"] = info["dirty"]
                row["aheadOfBase"] = info["aheadOfBase"]
                row["linkedWorktree"] = is_linked_worktree(path)
                row["underWorkspaceRoot"] = path_is_under(path, root)
                if row["dirty"]:
                    dirty_repos.append(repo.get("name") or path)
                if row["aheadOfBase"]:
                    total_ahead += row["aheadOfBase"]
                if not row["linkedWorktree"]:
                    non_linked_repos.append(repo.get("name") or path)
                if not row["underWorkspaceRoot"]:
                    external_repos.append(repo.get("name") or path)
            else:
                missing_repos.append(repo.get("name") or path or "?")
            repo_rows.append(row)

        extra_paths = self.workspace_root_extra_paths(data)
        prs = data.get("links", {}).get("githubPrs", [])
        open_prs = [
            pr
            for pr in prs
            if str(pr.get("state") or "").upper() not in {"CLOSED", "MERGED"}
            and not pr.get("merged")
        ]
        candidate = {
            "workspace": workspace_id,
            "title": data.get("title"),
            "state": data.get("state"),
            "workspaceRoot": str(root),
            "workspaceRootExists": root.exists(),
            "repos": repo_rows,
            "repoCount": len(repo_rows),
            "existingRepoCount": len([repo for repo in repo_rows if repo["exists"]]),
            "dirtyRepos": dirty_repos,
            "missingRepos": missing_repos,
            "nonLinkedRepos": non_linked_repos,
            "externalRepos": external_repos,
            "extraPaths": extra_paths,
            "aheadOfBase": total_ahead,
            "prCount": len(prs),
            "openPrCount": len(open_prs),
            "recommendedAction": "needs-review",
            "reason": "",
        }

        reasons = []
        if data.get("state") not in DONE_STATES:
            reasons.append("workspace is not closed or dev-complete")
        if dirty_repos:
            reasons.append("repo worktree has uncommitted changes")
        if non_linked_repos:
            reasons.append("repo is not a linked git worktree")
        if external_repos:
            reasons.append("repo worktree is outside the workspace root")
        if extra_paths:
            reasons.append("workspace root contains extra files")
        if open_prs:
            reasons.append("workspace has open PR snapshots")

        if reasons:
            candidate["reason"] = "; ".join(reasons)
        elif not candidate["workspaceRootExists"] and candidate["existingRepoCount"] == 0:
            candidate["recommendedAction"] = "mark-done"
            candidate["reason"] = "workspace worktrees are already gone"
        else:
            candidate["recommendedAction"] = "safe-to-remove"
            candidate["reason"] = "workspace is done and worktrees are clean linked worktrees"
        return candidate

    def print_workspace_plan(self, candidates):
        if not candidates:
            print("No workspace cleanup candidates found.")
            return
        print(
            f"{'WORKSPACE':24} {'STATE':14} {'ACTION':14} {'REPOS':5} "
            f"{'DIRTY':5} {'AHEAD':5} {'PRS':3} REASON"
        )
        for item in candidates:
            print(
                f"{item['workspace'][:24]:24} "
                f"{str(item['state'] or '-')[:14]:14} "
                f"{item['recommendedAction'][:14]:14} "
                f"{str(item['existingRepoCount'])[:5]:5} "
                f"{str(len(item['dirtyRepos']))[:5]:5} "
                f"{str(item['aheadOfBase'])[:5]:5} "
                f"{str(item['openPrCount'])[:3]:3} "
                f"{item['reason']}"
            )

    def snapshot(self, candidate):
        return {
            "workspace": candidate.get("workspace"),
            "state": candidate.get("state"),
            "workspaceRoot": candidate.get("workspaceRoot"),
            "workspaceRootExists": candidate.get("workspaceRootExists"),
            "repoCount": candidate.get("repoCount"),
            "existingRepoCount": candidate.get("existingRepoCount"),
            "dirtyRepos": candidate.get("dirtyRepos") or [],
            "missingRepos": candidate.get("missingRepos") or [],
            "nonLinkedRepos": candidate.get("nonLinkedRepos") or [],
            "externalRepos": candidate.get("externalRepos") or [],
            "extraPaths": candidate.get("extraPaths") or [],
            "aheadOfBase": candidate.get("aheadOfBase"),
            "openPrCount": candidate.get("openPrCount"),
            "recommendedAction": candidate.get("recommendedAction"),
            "reason": candidate.get("reason"),
            "repos": [
                {
                    "name": repo.get("name"),
                    "path": repo.get("path"),
                    "branch": repo.get("branch"),
                    "exists": repo.get("exists"),
                    "dirty": repo.get("dirty"),
                    "aheadOfBase": repo.get("aheadOfBase"),
                    "linkedWorktree": repo.get("linkedWorktree"),
                    "underWorkspaceRoot": repo.get("underWorkspaceRoot"),
                }
                for repo in candidate.get("repos", [])
            ],
        }

    def write_plan(self, candidates):
        stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        path = self.ledger.cleanup_runs_dir() / f"cleanup-plan-{stamp}.json"
        payload = {
            "version": 1,
            "type": "workspace-cleanup",
            "createdAt": now(),
            "candidates": candidates,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path, payload

    def load_plan(self, path):
        plan_path = Path(path).expanduser()
        if not plan_path.exists():
            raise SystemExit(f"Cleanup plan not found: {plan_path}")
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SystemExit(f"Invalid cleanup plan JSON: {plan_path}\n{error}") from error
        if payload.get("type") != "workspace-cleanup":
            raise SystemExit(f"Cleanup plan is not a workspace cleanup plan: {plan_path}")
        return payload

    def validate_plan_candidate(self, plan, workspace_id):
        planned = {
            str(candidate.get("workspace")): candidate
            for candidate in plan.get("candidates", [])
        }.get(str(workspace_id))
        if not planned:
            raise SystemExit(f"Workspace `{workspace_id}` is not in cleanup plan.")
        if planned.get("recommendedAction") not in {"safe-to-remove", "mark-done"}:
            raise SystemExit(
                f"Workspace `{workspace_id}` was not approved as safe in the plan: "
                f"{planned.get('recommendedAction')}"
            )
        current = self.workspace_candidate(workspace_id)
        if self.snapshot(current) != self.snapshot(planned):
            raise SystemExit(
                f"Cleanup plan is stale for `{workspace_id}`. Run cleanup-plan again."
            )
        return current

    def cleanup_workspace(self, workspace_id):
        candidate = self.workspace_candidate(workspace_id)
        action = candidate["recommendedAction"]
        if action not in {"safe-to-remove", "mark-done"}:
            raise SystemExit(
                f"Refusing cleanup for {workspace_id}: {candidate['reason']} ({action})"
            )

        data = self.ledger.load(workspace_id)
        if action == "safe-to-remove":
            for repo in data.get("repos", []):
                path = repo.get("worktreePath")
                if not path or not Path(path).exists():
                    continue
                source_path = repo.get("sourcePath")
                git_cwd = source_path if source_path and Path(source_path).exists() else path
                run(["git", "-C", str(git_cwd), "worktree", "remove", str(path)])
            root = self.ledger.workspace_root_path(workspace_id)
            if root.exists():
                shutil.rmtree(root)

        data["state"] = "closed"
        data["cleanupStatus"] = "done"
        data["cleanupAt"] = now()
        data["cleanupAction"] = action
        self.ledger.save(data)
        self.ledger.append_note(workspace_id, f"Cleaned up workspace with action `{action}`.")
        return {**candidate, "cleanupStatus": data["cleanupStatus"]}

    def issue_candidates(self, workspace_ids, repo_name=None):
        candidates = []
        for workspace_id in workspace_ids:
            data = self.ledger.load(workspace_id)
            repo = select_repo(data, repo_name)
            candidates.extend(self.issue_candidates_for_workspace(workspace_id, data, repo))
        return candidates

    def issue_candidates_for_workspace(self, workspace_id, data, repo):
        repo_path = repo.get("worktreePath")
        task_branch = repo.get("branch")
        repo_exists = bool(repo_path and Path(repo_path).exists())
        candidates = []
        for issue in iter_issues(self.config, workspace_id):
            meta = issue["meta"]
            if meta.get("status") not in ISSUE_DONE_STATUSES:
                continue
            if meta.get("cleanupStatus") != "pending":
                continue
            branch = meta.get("branch")
            item = f"{workspace_id}:{meta.get('id')}"
            candidate = {
                "item": item,
                "workspace": workspace_id,
                "workspaceState": data.get("state"),
                "repo": repo.get("name"),
                "repoPath": repo_path,
                "taskBranch": task_branch,
                "issue": meta.get("id"),
                "issueTitle": meta.get("title"),
                "issueStatus": meta.get("status"),
                "cleanupStatus": meta.get("cleanupStatus"),
                "branch": branch,
                "branchExists": False,
                "mergedIntoTaskBranch": None,
                "worktreePath": None,
                "worktreeDirty": None,
                "recommendedAction": "needs-review",
                "reason": "",
            }
            if not repo_exists:
                candidate["reason"] = "repo worktree is missing"
                candidates.append(candidate)
                continue
            if not branch:
                candidate["reason"] = "issue has no child branch"
                candidates.append(candidate)
                continue
            if branch == task_branch:
                candidate["reason"] = "child branch equals workspace task branch"
                candidates.append(candidate)
                continue

            child_worktree = worktree_for_branch(repo_path, branch)
            candidate["worktreePath"] = child_worktree
            if child_worktree:
                candidate["worktreeDirty"] = worktree_dirty(child_worktree)
                if candidate["worktreeDirty"]:
                    candidate["reason"] = "child worktree has uncommitted changes"
                    candidates.append(candidate)
                    continue

            candidate["branchExists"] = branch_exists(repo_path, branch)
            candidate["mergedIntoTaskBranch"] = branch_merged(
                repo_path,
                branch,
                task_branch,
            )
            if not candidate["branchExists"] and not child_worktree:
                candidate["recommendedAction"] = "mark-done"
                candidate["reason"] = "child branch and worktree are already gone"
            elif candidate["mergedIntoTaskBranch"] is True:
                candidate["recommendedAction"] = "safe-to-delete"
                candidate["reason"] = "child branch is merged into task branch"
            else:
                candidate["reason"] = "child branch is not safely merged into task branch"
            candidates.append(candidate)
        return candidates

    def print_issue_plan(self, candidates):
        if not candidates:
            print("No cleanup candidates found.")
            return
        print(
            f"{'ITEM':24} {'ACTION':14} {'BRANCH':32} {'MERGED':6} {'DIRTY':5} REASON"
        )
        for item in candidates:
            merged = item["mergedIntoTaskBranch"]
            dirty = item["worktreeDirty"]
            print(
                f"{item['item'][:24]:24} "
                f"{item['recommendedAction'][:14]:14} "
                f"{str(item['branch'] or '-')[:32]:32} "
                f"{str(merged)[:6]:6} "
                f"{str(dirty)[:5]:5} "
                f"{item['reason']}"
            )

    def cleanup_item(self, item, repo_name=None):
        if ":" not in item:
            raise SystemExit(f"Cleanup item must be WORKSPACE:ISSUE, got: {item}")
        workspace_id, issue_id = item.split(":", 1)
        matches = [
            candidate
            for candidate in self.issue_candidates([workspace_id], repo_name=repo_name)
            if str(candidate.get("issue")) == issue_id
        ]
        if not matches:
            raise SystemExit(f"No cleanup candidate found for {item}")
        candidate = matches[0]
        action = candidate["recommendedAction"]
        if action not in {"safe-to-delete", "mark-done"}:
            raise SystemExit(
                f"Refusing cleanup for {item}: {candidate['reason']} ({action})"
            )

        repo_path = candidate["repoPath"]
        if action == "safe-to-delete":
            child_worktree = candidate.get("worktreePath")
            if child_worktree:
                run(["git", "-C", str(repo_path), "worktree", "remove", child_worktree])
            if candidate.get("branchExists"):
                run(["git", "-C", str(repo_path), "branch", "-d", candidate["branch"]])

        issue = issue_index(self.config, workspace_id).get(issue_id)
        extra = {
            "cleanupStatus": "done",
            "cleanupAt": now(),
            "cleanupAction": action,
        }
        _, meta, _ = set_issue_status(
            self.config,
            workspace_id,
            issue_id,
            issue["meta"]["status"],
            review_status=issue["meta"].get("reviewStatus"),
            branch=issue["meta"].get("branch"),
            extra=extra,
        )
        data = self.ledger.load(workspace_id)
        self.ledger.save(data)
        self.ledger.append_note(workspace_id, f"Cleaned up `{item}` with action `{action}`.")
        return {**candidate, "cleanupStatus": meta.get("cleanupStatus")}


def workspace_root_extra_paths(config, data):
    return CleanupSafety(config).workspace_root_extra_paths(data)


def workspace_cleanup_candidates(config, workspace_ids):
    return CleanupSafety(config).workspace_candidates(workspace_ids)


def print_workspace_cleanup_plan(candidates):
    CleanupSafety({}).print_workspace_plan(candidates)


def cleanup_plan_snapshot(candidate):
    return CleanupSafety({}).snapshot(candidate)


def write_cleanup_plan(config, candidates):
    return CleanupSafety(config).write_plan(candidates)


def load_cleanup_plan(path):
    return CleanupSafety({}).load_plan(path)


def validate_cleanup_plan_candidate(config, plan, workspace_id):
    return CleanupSafety(config).validate_plan_candidate(plan, workspace_id)


def cleanup_workspace(config, workspace_id):
    return CleanupSafety(config).cleanup_workspace(workspace_id)


def cleanup_candidates(config, workspace_ids, repo_name=None):
    return CleanupSafety(config).issue_candidates(workspace_ids, repo_name=repo_name)


def print_cleanup_plan(candidates):
    CleanupSafety({}).print_issue_plan(candidates)


def cleanup_item(config, item, repo_name=None):
    return CleanupSafety(config).cleanup_item(item, repo_name=repo_name)


def repo_name_from_remote(remote_url):
    remote_url = remote_url.strip()
    if remote_url.startswith("git@"):
        _, rest = remote_url.split(":", 1)
        return rest.removesuffix(".git")
    if remote_url.startswith("https://") or remote_url.startswith("ssh://"):
        parsed = urlparse(remote_url)
        return parsed.path.strip("/").removesuffix(".git")
    return remote_url.removesuffix(".git")


def github_repo_from_remote(remote_url):
    name = repo_name_from_remote(remote_url)
    if "/" in name:
        return name
    return None


def git_root(path):
    try:
        root = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
        return Path(root)
    except SystemExit:
        return None


def git_info(path, config):
    root = git_root(path)
    if root is None:
        raise SystemExit(f"Not inside a git repo: {path}")
    branch = run(["git", "-C", str(root), "branch", "--show-current"], check=False)
    origin = run(["git", "-C", str(root), "remote", "get-url", "origin"], check=False)
    fork = run(["git", "-C", str(root), "remote", "get-url", config["push_remote"]], check=False)
    status = run(["git", "-C", str(root), "status", "--short"], check=False)
    ahead = run(
        [
            "git",
            "-C",
            str(root),
            "rev-list",
            "--count",
            f"{config['base_remote']}/{config['base_branch']}..HEAD",
        ],
        check=False,
    )
    return {
        "root": str(root),
        "name": root.name,
        "branch": branch or None,
        "remote": origin or None,
        "forkRemote": fork or None,
        "dirty": bool(status),
        "aheadOfBase": int(ahead) if ahead.isdigit() else None,
    }


def git_success(path, args):
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 0


def git_resolved_path(path, arg):
    output = run(["git", "-C", str(path), "rev-parse", arg], check=False)
    if not output:
        return None
    candidate = Path(output)
    if not candidate.is_absolute():
        candidate = Path(path) / candidate
    return candidate.resolve()


def is_linked_worktree(path):
    git_dir = git_resolved_path(path, "--git-dir")
    common_dir = git_resolved_path(path, "--git-common-dir")
    if not git_dir or not common_dir:
        return False
    return git_dir != common_dir


def git_worktrees(path):
    output = run(["git", "-C", str(path), "worktree", "list", "--porcelain"], check=False)
    entries = []
    current = {}
    for line in output.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"path": line.removeprefix("worktree ").strip(), "branch": None}
        elif line.startswith("branch ") and current:
            ref = line.removeprefix("branch ").strip()
            current["branch"] = ref.removeprefix("refs/heads/")
    if current:
        entries.append(current)
    return entries


def worktree_for_branch(path, branch):
    for entry in git_worktrees(path):
        if entry.get("branch") == branch:
            return entry.get("path")
    return None


def branch_exists(path, branch):
    return git_success(path, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])


def branch_merged(path, branch, target_branch):
    if not branch_exists(path, branch):
        return None
    if not target_branch:
        return False
    if not branch_exists(path, target_branch):
        return False
    return git_success(path, ["merge-base", "--is-ancestor", branch, target_branch])


def worktree_dirty(path):
    return bool(run(["git", "-C", str(path), "status", "--porcelain"], check=False))


def upsert_repo(data, repo):
    repos = data.setdefault("repos", [])
    for index, existing in enumerate(repos):
        if existing.get("worktreePath") == repo.get("worktreePath") or existing.get("name") == repo.get("name"):
            repos[index] = {**existing, **repo}
            return
    repos.append(repo)


def infer_workspace_id(path, info):
    current = Path(path).resolve()
    for parent in [current] + list(current.parents):
        pointer = parent / ".workspace-id"
        if pointer.exists():
            return pointer.read_text(encoding="utf-8").strip()
    branch = info.get("branch") or ""
    jira = re.search(r"[A-Z][A-Z0-9]+-\d+", branch)
    if jira:
        return jira.group(0)
    if branch:
        return branch.replace("/", "-")
    raise SystemExit("Could not infer workspace id. Pass --id.")


def cmd_init_config(args):
    if CONFIG_PATH.exists() and not args.force:
        print(f"Config already exists: {CONFIG_PATH}")
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SKILL_ROOT / "templates" / "config.yaml", CONFIG_PATH)
    print(f"Wrote {CONFIG_PATH}")


def cmd_config(args):
    print(yaml.safe_dump(load_config(), sort_keys=False).strip())


def add_source_repo_to_workspace(config, data, repo_name, branch=None):
    workspace_id = data["id"]
    workspace_root = workspace_root_path(config, workspace_id)
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / ".workspace-id").write_text(workspace_id + "\n", encoding="utf-8")
    source = Path(config["source_root"]) / repo_name
    dest = workspace_root / repo_name
    if not source.exists():
        raise SystemExit(f"Source repo not found: {source}")
    if not dest.exists():
        run(
            [
                "git",
                "-C",
                str(source),
                "worktree",
                "add",
                "-b",
                branch or workspace_id,
                str(dest),
                f"{config['base_remote']}/{config['base_branch']}",
            ],
            capture=True,
        )
    info = git_info(dest, config)
    upsert_repo(
        data,
        {
            "name": repo_name,
            "sourcePath": str(source),
            "worktreePath": str(dest),
            "branch": info["branch"],
            "baseRemote": config["base_remote"],
            "baseBranch": config["base_branch"],
            "pushRemote": config["push_remote"],
            "remote": info["remote"],
            "forkRemote": info["forkRemote"],
        },
    )
    return dest


def cmd_create(args):
    config = load_config()
    data = ensure_workspace(config, args.id, args.title, args.state, args.input)
    for repo in args.repo or []:
        add_source_repo_to_workspace(config, data, repo, branch=args.branch)
    save_workspace(config, data)
    print(str(ledger_dir(config, args.id)))


def cmd_adopt(args):
    config = load_config()
    info = git_info(args.path, config)
    workspace_id = args.id or infer_workspace_id(args.path, info)
    data = ensure_workspace(config, workspace_id, args.title, "in-progress")
    upsert_repo(
        data,
        {
            "name": args.name or Path(info["root"]).name,
            "worktreePath": info["root"],
            "branch": info["branch"],
            "baseRemote": config["base_remote"],
            "baseBranch": config["base_branch"],
            "pushRemote": config["push_remote"],
            "remote": info["remote"],
            "forkRemote": info["forkRemote"],
        },
    )
    save_workspace(config, data)
    append_note(config, workspace_id, f"Adopted worktree `{info['root']}` on branch `{info['branch']}`.")
    print(workspace_id)


def find_repo(data, repo_name):
    for index, repo in enumerate(data.get("repos", [])):
        if repo.get("name") == repo_name:
            return index, repo
    raise SystemExit(f"Repo not found in workspace: {repo_name}")


def cmd_repo_list(args):
    config = load_config()
    data = load_workspace(config, args.id)
    repos = data.get("repos", [])
    if args.json:
        print(json.dumps({"repos": repos}, indent=2))
        return
    if not repos:
        print("No repos found.")
        return
    print(f"{'NAME':24} {'BRANCH':24} PATH")
    for repo in repos:
        print(f"{str(repo.get('name') or '')[:24]:24} {str(repo.get('branch') or '')[:24]:24} {repo.get('worktreePath') or ''}")


def cmd_repo_add(args):
    config = load_config()
    data = load_workspace(config, args.id)
    dest = add_source_repo_to_workspace(config, data, args.repo, branch=args.branch)
    save_workspace(config, data)
    append_note(config, args.id, f"Added repo `{args.repo}` at `{dest}`.")
    print(dest)


def cmd_repo_adopt(args):
    config = load_config()
    data = load_workspace(config, args.id)
    info = git_info(args.path, config)
    upsert_repo(
        data,
        {
            "name": args.name or Path(info["root"]).name,
            "worktreePath": info["root"],
            "branch": info["branch"],
            "baseRemote": config["base_remote"],
            "baseBranch": config["base_branch"],
            "pushRemote": config["push_remote"],
            "remote": info["remote"],
            "forkRemote": info["forkRemote"],
        },
    )
    save_workspace(config, data)
    append_note(config, args.id, f"Adopted repo `{info['root']}` on branch `{info['branch']}`.")
    print(info["root"])


def remove_repo_worktree(config, data, repo):
    path = repo.get("worktreePath")
    if not path or not Path(path).exists():
        return
    if not is_linked_worktree(path):
        raise SystemExit(f"Refusing to delete non-linked repo checkout: {path}")
    if not path_is_under(path, workspace_root_path(config, data["id"])):
        raise SystemExit(f"Refusing to delete repo outside workspace root: {path}")
    if worktree_dirty(path):
        raise SystemExit(f"Refusing to delete dirty repo worktree: {path}")
    source_path = repo.get("sourcePath")
    git_cwd = source_path if source_path and Path(source_path).exists() else path
    run(["git", "-C", str(git_cwd), "worktree", "remove", str(path)])


def cmd_repo_remove(args):
    config = load_config()
    data = load_workspace(config, args.id)
    index, repo = find_repo(data, args.repo)
    if args.delete_worktree:
        remove_repo_worktree(config, data, repo)
    data["repos"].pop(index)
    save_workspace(config, data)
    append_note(config, args.id, f"Removed repo `{args.repo}` from workspace metadata.")
    print(args.repo)


def refresh_repo(config, data, repo):
    path = repo.get("worktreePath")
    if not path or not Path(path).exists():
        return repo
    info = git_info(path, config)
    return {
        **repo,
        "name": repo.get("name") or info["name"],
        "worktreePath": info["root"],
        "branch": info["branch"],
        "baseRemote": repo.get("baseRemote") or config["base_remote"],
        "baseBranch": repo.get("baseBranch") or config["base_branch"],
        "pushRemote": repo.get("pushRemote") or config["push_remote"],
        "remote": info["remote"],
        "forkRemote": info["forkRemote"],
    }


def cmd_repo_refresh(args):
    config = load_config()
    data = load_workspace(config, args.id)
    if args.repo:
        index, repo = find_repo(data, args.repo)
        data["repos"][index] = refresh_repo(config, data, repo)
        refreshed = [args.repo]
    else:
        data["repos"] = [refresh_repo(config, data, repo) for repo in data.get("repos", [])]
        refreshed = [repo.get("name") or "?" for repo in data.get("repos", [])]
    save_workspace(config, data)
    append_note(config, args.id, f"Refreshed repo metadata: {', '.join(refreshed)}.")
    print("\n".join(refreshed))


def find_workspace_from_cwd(config):
    current = Path.cwd().resolve()
    for parent in [current] + list(current.parents):
        pointer = parent / ".workspace-id"
        if pointer.exists():
            return pointer.read_text(encoding="utf-8").strip()
    workspaces = Path(config["ledger_root"]) / "workspaces"
    if workspaces.exists():
        for meta in workspaces.glob("*/workspace.yaml"):
            data = read_yaml(meta)
            for repo in data.get("repos", []):
                path = repo.get("worktreePath")
                if path and current.is_relative_to(Path(path).resolve()):
                    return data["id"]
    raise SystemExit("Workspace id required.")


def cmd_status(args):
    config = load_config()
    workspace_id = args.id or find_workspace_from_cwd(config)
    data = load_workspace(config, workspace_id)
    print(f"Workspace: {data['id']}")
    print(f"Title: {data.get('title', '')}")
    print(f"State: {data.get('state', '')}")
    if data.get("vscodeWorkspacePath"):
        print(f"VS Code: {data.get('vscodeWorkspacePath')}")
    print("")
    for repo in data.get("repos", []):
        path = repo.get("worktreePath")
        print(f"- {repo.get('name')}: {path}")
        if path and Path(path).exists():
            info = git_info(path, config)
            print(f"  branch: {info['branch']}")
            print(f"  dirty: {'yes' if info['dirty'] else 'no'}")
            if info["aheadOfBase"] is not None:
                print(f"  ahead {config['base_remote']}/{config['base_branch']}: {info['aheadOfBase']}")
        else:
            print("  missing on disk")
    prs = data.get("links", {}).get("githubPrs", [])
    if prs:
        print("")
        print("GitHub PRs:")
        for pr in prs:
            label = pr.get("url") or pr.get("number")
            print(f"- {pr.get('repo')} {label} {pr.get('state', '')}")
    issues = iter_issues(config, workspace_id)
    if issues:
        ready, hitl = ready_issues(config, workspace_id)
        counts = {}
        for issue in issues:
            status = issue["meta"].get("status", "")
            counts[status] = counts.get(status, 0) + 1
        print("")
        print("Issues:")
        print(f"  total: {len(issues)}")
        print(f"  ready AFK: {len(ready)}")
        print(f"  ready HITL: {len(hitl)}")
        print("  statuses: " + " ".join(f"{k}:{v}" for k, v in sorted(counts.items())))


def cmd_open(args):
    config = load_config()
    data = load_workspace(config, args.id)
    save_workspace(config, data)
    path = Path(data.get("vscodeWorkspacePath") or vscode_workspace_path(config, args.id))
    if not path.exists():
        path = write_vscode_workspace(config, data)
    if args.print:
        print(path)
        return
    command = args.command or config.get("editor_command") or "code"
    parts = shlex.split(command)
    if not parts:
        raise SystemExit("Editor command is empty.")
    try:
        subprocess.Popen([*parts, str(path)])
    except FileNotFoundError as error:
        raise SystemExit(f"Editor command not found: {parts[0]}") from error
    append_note(config, args.id, f"Opened VS Code workspace `{path}`.")
    print(path)


def doctor_issue(severity, code, message, fixable=False):
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "fixable": fixable,
    }


def doctor_workspace(config, workspace_id, fix=False):
    data = load_workspace(config, workspace_id)
    issues = []
    root = ledger_dir(config, workspace_id)
    workspace_root = workspace_root_path(config, workspace_id)

    required_dirs = [root / "runs", root / "issues"]
    required_files = {
        root / "spec.md": template("spec.md").format(
            id=workspace_id,
            title=data.get("title") or workspace_id,
            input="TBD.",
        ),
        root / "notes.md": template("notes.md"),
    }
    for directory in required_dirs:
        if not directory.exists():
            issues.append(doctor_issue("warning", "missing-dir", f"Missing directory: {directory}", True))
            if fix:
                directory.mkdir(parents=True, exist_ok=True)
    for path, contents in required_files.items():
        if not path.exists():
            issues.append(doctor_issue("warning", "missing-file", f"Missing file: {path}", True))
            if fix:
                path.write_text(contents, encoding="utf-8")

    if data.get("cleanupStatus") != "done":
        pointer = workspace_root / ".workspace-id"
        if not pointer.exists() or pointer.read_text(encoding="utf-8").strip() != workspace_id:
            issues.append(
                doctor_issue(
                    "warning",
                    "workspace-pointer",
                    f"Missing or stale workspace pointer: {pointer}",
                    True,
                )
            )
            if fix:
                workspace_root.mkdir(parents=True, exist_ok=True)
                pointer.write_text(workspace_id + "\n", encoding="utf-8")

        expected_path, expected_payload = build_vscode_workspace(config, data)
        actual_payload = None
        if expected_path.exists():
            try:
                actual_payload = json.loads(expected_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                actual_payload = None
        if actual_payload != expected_payload:
            issues.append(
                doctor_issue(
                    "warning",
                    "vscode-workspace",
                    f"Missing or stale VS Code workspace file: {expected_path}",
                    True,
                )
            )
            if fix:
                write_vscode_workspace(config, data)

    for repo in data.get("repos", []):
        path = repo.get("worktreePath")
        name = repo.get("name") or path or "?"
        if not path or not Path(path).exists():
            issues.append(doctor_issue("error", "repo-missing", f"Repo worktree missing: {name}"))
            continue
        try:
            info = git_info(path, config)
        except SystemExit as error:
            issues.append(doctor_issue("error", "repo-not-git", f"Repo is not valid git: {name}: {error}"))
            continue
        if info.get("branch") != repo.get("branch"):
            issues.append(
                doctor_issue(
                    "warning",
                    "repo-branch-drift",
                    f"Repo branch drift for {name}: ledger={repo.get('branch')} current={info.get('branch')}",
                )
            )
        if info.get("dirty"):
            issues.append(doctor_issue("warning", "repo-dirty", f"Repo has uncommitted changes: {name}"))

    if fix:
        data = load_workspace(config, workspace_id)
        save_workspace(config, data)
    return issues


def print_doctor_report(workspace_id, issues):
    if not issues:
        print(f"{workspace_id}: ok")
        return
    print(f"{workspace_id}: {len(issues)} issue(s)")
    for issue in issues:
        suffix = " fixable" if issue.get("fixable") else ""
        print(f"- {issue['severity']} {issue['code']}{suffix}: {issue['message']}")


def cmd_doctor(args):
    config = load_config()
    issues = doctor_workspace(config, args.id, fix=args.fix)
    if args.json:
        print(json.dumps({"workspace": args.id, "issues": issues}, indent=2))
        return
    print_doctor_report(args.id, issues)


def repo_summary(data, config):
    repo_names = []
    dirty = 0
    ahead = 0
    missing = 0
    for repo in data.get("repos", []):
        repo_names.append(repo.get("name") or "?")
        path = repo.get("worktreePath")
        if not path or not Path(path).exists():
            missing += 1
            continue
        info = git_info(path, config)
        if info["dirty"]:
            dirty += 1
        if info["aheadOfBase"]:
            ahead += info["aheadOfBase"]

    local = []
    if ahead:
        local.append(f"+{ahead}")
    if dirty:
        local.append(f"{dirty} dirty")
    if missing:
        local.append(f"{missing} missing")
    return ",".join(repo_names) or "-", " ".join(local) or "clean"


def cmd_list(args):
    config = load_config()
    rows = []
    for data in iter_workspaces(config):
        state = data.get("state", "")
        if args.state and state != args.state:
            continue
        active_only = args.active or (not args.all and not args.state)
        if active_only and state in DONE_STATES:
            continue
        repos, local = repo_summary(data, config)
        prs = len(data.get("links", {}).get("githubPrs", []))
        rows.append(
            {
                "id": data.get("id", ""),
                "state": state,
                "updated": str(data.get("updatedAt", ""))[:10],
                "repos": repos,
                "local": local,
                "prs": str(prs),
                "title": data.get("title", ""),
            }
        )

    rows.sort(key=lambda row: (STATE_ORDER.get(row["state"], 99), row["updated"], row["id"]))
    if not rows:
        print("No workspaces found.")
        return

    state_counts = {}
    for row in rows:
        state_counts[row["state"]] = state_counts.get(row["state"], 0) + 1
    summary = "  ".join(f"{state}:{count}" for state, count in sorted(state_counts.items()))
    scope = args.state or ("all" if args.all else "active")
    print(f"Workspaces ({scope}): {len(rows)}  {summary}")
    print("")
    print(f"{'ID':32} {'STATE':19} {'UPDATED':10} {'REPOS':24} {'LOCAL':12} {'PRS':3} TITLE")
    for row in rows:
        print(
            f"{row['id'][:32]:32} "
            f"{row['state'][:19]:19} "
            f"{row['updated'][:10]:10} "
            f"{row['repos'][:24]:24} "
            f"{row['local'][:12]:12} "
            f"{row['prs'][:3]:3} "
            f"{row['title']}"
        )


def cmd_note(args):
    config = load_config()
    append_note(config, args.id, args.text)
    data = load_workspace(config, args.id)
    save_workspace(config, data)


def cmd_close(args):
    config = load_config()
    data = load_workspace(config, args.id)
    data["state"] = "closed"
    save_workspace(config, data)
    append_note(config, args.id, "Closed workspace.")


def cmd_state(args):
    config = load_config()
    data = load_workspace(config, args.id)
    old_state = data.get("state")
    data["state"] = args.state
    save_workspace(config, data)
    note = args.note or f"State changed from `{old_state}` to `{args.state}`."
    append_note(config, args.id, note)


def print_issue_row(issue):
    meta = issue["meta"]
    blockers = ",".join(str(item) for item in meta.get("blockedBy") or []) or "-"
    print(
        f"{str(meta.get('id', ''))[:16]:16} "
        f"{str(meta.get('type', ''))[:4]:4} "
        f"{str(meta.get('status', ''))[:12]:12} "
        f"{blockers[:18]:18} "
        f"{str(meta.get('reviewStatus', ''))[:12]:12} "
        f"{meta.get('title', '')}"
    )


def cmd_issue_create(args):
    config = load_config()
    load_workspace(config, args.workspace_id)
    path = issue_path(config, args.workspace_id, args.issue_id)
    if path.exists() and not args.force:
        raise SystemExit(f"Issue already exists: {path}")
    meta = {
        "id": args.issue_id,
        "title": args.title,
        "type": args.type.upper(),
        "status": args.status,
        "blockedBy": args.blocked_by or [],
    }
    if args.branch:
        meta["branch"] = args.branch
    body = issue_body_from_args(args)
    path, meta = write_issue(config, args.workspace_id, args.issue_id, meta, body)
    data = load_workspace(config, args.workspace_id)
    save_workspace(config, data)
    append_note(config, args.workspace_id, f"Created issue `{meta['id']}`: {meta['title']}")
    print(str(path))


def cmd_issue_list(args):
    config = load_config()
    load_workspace(config, args.workspace_id)
    issues = iter_issues(config, args.workspace_id)
    if args.status:
        issues = [issue for issue in issues if issue["meta"].get("status") == args.status]
    if args.type:
        issue_type = args.type.upper()
        issues = [
            issue
            for issue in issues
            if str(issue["meta"].get("type", "")).upper() == issue_type
        ]
    if not args.all:
        issues = [
            issue
            for issue in issues
            if issue["meta"].get("status") not in ISSUE_DONE_STATUSES
        ]
    if not issues:
        print("No issues found.")
        return
    print(f"{'ID':16} {'TYPE':4} {'STATUS':12} {'BLOCKED BY':18} {'REVIEW':12} TITLE")
    for issue in issues:
        print_issue_row(issue)


def cmd_issue_ready(args):
    config = load_config()
    load_workspace(config, args.workspace_id)
    ready, hitl = ready_issues(config, args.workspace_id)
    if args.json:
        payload = {
            "issues": [
                {
                    "id": issue["meta"]["id"],
                    "title": issue["meta"]["title"],
                    "branch": issue["meta"]["branch"],
                    "type": issue["meta"]["type"],
                    "status": issue["meta"]["status"],
                    "path": str(issue["path"]),
                }
                for issue in ready
            ],
            "hitl": [
                {
                    "id": issue["meta"]["id"],
                    "title": issue["meta"]["title"],
                    "status": issue["meta"]["status"],
                    "path": str(issue["path"]),
                }
                for issue in hitl
            ],
        }
        print(json.dumps(payload, indent=2))
        return
    if ready:
        print("Ready AFK issues:")
        print(f"{'ID':16} {'TYPE':4} {'STATUS':12} {'BLOCKED BY':18} {'REVIEW':12} TITLE")
        for issue in ready:
            print_issue_row(issue)
    else:
        print("Ready AFK issues: none")
    if hitl:
        print("")
        print("Ready HITL issues:")
        print(f"{'ID':16} {'TYPE':4} {'STATUS':12} {'BLOCKED BY':18} {'REVIEW':12} TITLE")
        for issue in hitl:
            print_issue_row(issue)


def cmd_issue_show(args):
    config = load_config()
    load_workspace(config, args.workspace_id)
    path = issue_path(config, args.workspace_id, args.issue_id)
    if not path.exists():
        raise SystemExit(f"Issue not found: {args.issue_id}")
    print(path.read_text(encoding="utf-8").rstrip())


def cmd_issue_set_status(args):
    config = load_config()
    load_workspace(config, args.workspace_id)
    path, meta, old_status = set_issue_status(
        config,
        args.workspace_id,
        args.issue_id,
        args.status,
        review_status=args.review_status,
        branch=args.branch,
    )
    data = load_workspace(config, args.workspace_id)
    save_workspace(config, data)
    note = args.note or f"Issue `{meta['id']}` status changed from `{old_status}` to `{args.status}`."
    append_note(config, args.workspace_id, note)
    maybe_mark_user_review(config, args.workspace_id)
    print(str(path))


def cmd_sandcastle(args):
    config = load_config()
    if args.reconcile_result:
        applied = apply_sandcastle_result(config, args.id, args.reconcile_result)
        print(f"Reconciled {len(applied)} issue update(s).")
        return

    data = load_workspace(config, args.id)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be greater than 0")
    repo = select_repo(data, args.repo)
    if args.init_runner:
        written = init_sandcastle_runner(repo, force=args.force)
        append_note(
            config,
            args.id,
            f"Initialized Sandcastle runner in `{repo.get('worktreePath')}` with {len(written)} file(s).",
        )
        for path in written:
            print(path)
        return
    worktree = repo.get("worktreePath")
    if not worktree or not Path(worktree).exists():
        raise SystemExit(f"Repo worktree missing on disk: {worktree}")

    ready, hitl = ready_issues(config, args.id)
    plan_path, payload = write_sandcastle_plan(
        config,
        data,
        repo,
        ready,
        hitl,
        limit=args.limit,
    )
    result_path = plan_path.with_name(
        plan_path.name.replace("sandcastle-plan-", "sandcastle-result-")
    )

    if args.json:
        print(
            json.dumps(
                {**payload, "planPath": str(plan_path), "resultPath": str(result_path)},
                indent=2,
            )
        )
    else:
        print(f"Wrote Sandcastle plan: {plan_path}")
        print(f"Expected Sandcastle result: {result_path}")
        print(f"Ready AFK issues: {len(payload['issues'])}")
        print(f"Ready HITL issues: {len(payload['hitl'])}")
        if payload["issues"]:
            for issue in payload["issues"]:
                print(f"- {issue['id']}: {issue['title']} -> {issue['branch']}")
        elif payload["hitl"]:
            print("No AFK issues are ready. Resolve HITL work before rerunning.")
        else:
            print("No dependency-ready issues found.")

    append_note(
        config,
        args.id,
        f"Prepared Sandcastle plan `{plan_path}` with {len(payload['issues'])} AFK issue(s) and {len(payload['hitl'])} HITL issue(s).",
    )

    if not args.execute:
        return
    if not args.command:
        raise SystemExit("--execute requires --command")
    if not payload["issues"]:
        raise SystemExit("No ready AFK issues to execute.")

    lock_path = acquire_sandcastle_lock(
        config,
        args.id,
        {"planPath": str(plan_path), "issues": [issue["id"] for issue in payload["issues"]]},
    )
    try:
        for issue in payload["issues"]:
            set_issue_status(config, args.id, issue["id"], "running")
        data = load_workspace(config, args.id)
        data["state"] = "in-progress"
        save_workspace(config, data)
        append_note(config, args.id, f"Started Sandcastle command for plan `{plan_path}`.")

        env = os.environ.copy()
        env.update(
            {
                "WORKSPACE_ID": args.id,
                "WORKSPACE_LEDGER_DIR": str(ledger_dir(config, args.id)),
                "WORKSPACE_SANDCASTLE_PLAN": str(plan_path),
                "WORKSPACE_SANDCASTLE_RESULT": str(result_path),
                "WORKSPACE_REPO_NAME": str(repo.get("name") or ""),
                "WORKSPACE_REPO_PATH": str(worktree),
            }
        )
        proc = subprocess.run(
            args.command,
            cwd=worktree,
            shell=True,
            env=env,
            check=False,
        )
        append_note(
            config,
            args.id,
            f"Sandcastle command exited with code {proc.returncode} for plan `{plan_path}`.",
        )
        if result_path.exists():
            apply_sandcastle_result(config, args.id, result_path)
        elif proc.returncode != 0:
            for issue in payload["issues"]:
                set_issue_status(
                    config,
                    args.id,
                    issue["id"],
                    "failed",
                    extra={"lastError": f"Sandcastle command exited with code {proc.returncode}"},
                )
            data = load_workspace(config, args.id)
            save_workspace(config, data)
            append_note(
                config,
                args.id,
                f"Marked {len(payload['issues'])} issue(s) failed because no result file was written.",
            )
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)
    finally:
        release_sandcastle_lock(lock_path)


def cmd_cleanup_plan(args):
    config = load_config()
    workspace_ids = args.id
    if args.all:
        workspace_ids = [data["id"] for data in iter_workspaces(config)]
    if not workspace_ids:
        raise SystemExit("Pass at least one workspace id, or --all.")
    if args.issues:
        if args.write:
            raise SystemExit("--write is only supported for workspace cleanup plans.")
        candidates = cleanup_candidates(config, workspace_ids, repo_name=args.repo)
        if args.json:
            print(json.dumps({"candidates": candidates}, indent=2))
            return
        print_cleanup_plan(candidates)
        return
    candidates = workspace_cleanup_candidates(config, workspace_ids)
    plan_path = None
    if args.write:
        plan_path, _ = write_cleanup_plan(config, candidates)
    if args.json:
        payload = {"candidates": candidates}
        if plan_path:
            payload["planPath"] = str(plan_path)
        print(json.dumps(payload, indent=2))
        return
    print_workspace_cleanup_plan(candidates)
    if plan_path:
        print(f"\nWrote cleanup plan: {plan_path}")


def cmd_cleanup(args):
    config = load_config()
    if args.item and (args.id or args.from_plan):
        raise SystemExit("Pass workspace ids/from-plan or --item values, not both.")
    if args.from_plan and not args.id:
        raise SystemExit("--from-plan requires one or more approved workspace ids.")
    results = []
    if args.item:
        for item in args.item:
            results.append(cleanup_item(config, item, repo_name=args.repo))
    else:
        if not args.id:
            raise SystemExit("Pass one or more workspace ids, or --item WORKSPACE:ISSUE values.")
        plan = load_cleanup_plan(args.from_plan) if args.from_plan else None
        for workspace_id in args.id:
            if plan:
                validate_cleanup_plan_candidate(config, plan, workspace_id)
            results.append(cleanup_workspace(config, workspace_id))
    if args.json:
        print(json.dumps({"cleaned": results}, indent=2))
        return
    for result in results:
        label = result.get("item") or result.get("workspace")
        print(f"Cleaned {label}: {result['recommendedAction']}")


def cmd_sync_github(args):
    config = load_config()
    if shutil.which("gh") is None:
        raise SystemExit("gh is not installed or not on PATH")
    data = load_workspace(config, args.id)
    links = data.setdefault("links", {})
    existing = links.setdefault("githubPrs", [])
    seen = []
    for repo in data.get("repos", []):
        path = repo.get("worktreePath")
        if not path or not Path(path).exists():
            continue
        info = git_info(path, config)
        branch = info["branch"]
        remote = info["remote"]
        full_name = github_repo_from_remote(remote or "")
        if not branch or not full_name:
            continue
        raw = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                full_name,
                "--head",
                branch,
                "--json",
                "number,url,state,isDraft,headRefName,baseRefName,mergedAt,title",
                "--limit",
                "10",
            ],
            check=False,
        )
        try:
            prs = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            prs = []
        for pr in prs:
            item = {
                "repo": repo.get("name"),
                "number": pr.get("number"),
                "url": pr.get("url"),
                "branch": pr.get("headRefName") or branch,
                "state": pr.get("state"),
                "merged": bool(pr.get("mergedAt")),
                "lastSeenAt": now(),
                "title": pr.get("title"),
            }
            seen.append(item)
    for item in seen:
        replaced = False
        for index, old in enumerate(existing):
            if old.get("repo") == item.get("repo") and old.get("branch") == item.get("branch"):
                existing[index] = {**old, **item}
                replaced = True
                break
        if not replaced:
            existing.append(item)
    save_workspace(config, data)
    append_note(config, args.id, f"Synced GitHub PR snapshots. Found {len(seen)} PR(s).")
    print(f"Found {len(seen)} PR(s).")


def cmd_pr(args):
    config = load_config()
    data = load_workspace(config, args.id)
    state_by_event = {
        "opened": "pr-review",
        "feedback": "review-feedback",
        "merged": "dev-complete",
    }
    data["state"] = state_by_event[args.event]
    if args.url or args.number:
        links = data.setdefault("links", {})
        prs = links.setdefault("githubPrs", [])
        item = {
            "url": args.url,
            "number": args.number,
            "state": "MERGED" if args.event == "merged" else "OPEN",
            "merged": args.event == "merged",
            "lastSeenAt": now(),
        }
        prs.append({key: value for key, value in item.items() if value is not None})
    save_workspace(config, data)
    note = args.note or f"PR lifecycle event `{args.event}` set workspace state to `{data['state']}`."
    append_note(config, args.id, note)
    print(data["state"])


def build_parser():
    parser = argparse.ArgumentParser(description="Manage local workspace ledger.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init_config)

    p = sub.add_parser("config")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("create")
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--state", default="captured")
    p.add_argument("--input")
    p.add_argument("--repo", action="append")
    p.add_argument("--branch")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("adopt")
    p.add_argument("path")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--name")
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("status")
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("open")
    p.add_argument("id")
    p.add_argument("--command")
    p.add_argument("--print", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("doctor")
    p.add_argument("id")
    p.add_argument("--fix", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("list")
    p.add_argument("--state")
    p.add_argument("--active", action="store_true")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("note")
    p.add_argument("id")
    p.add_argument("text")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("close")
    p.add_argument("id")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("state")
    p.add_argument("id")
    p.add_argument("state")
    p.add_argument("--note")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("sync-github")
    p.add_argument("id")
    p.set_defaults(func=cmd_sync_github)

    p = sub.add_parser("pr")
    p.add_argument("id")
    p.add_argument("event", choices=["opened", "feedback", "merged"])
    p.add_argument("--url")
    p.add_argument("--number", type=int)
    p.add_argument("--note")
    p.set_defaults(func=cmd_pr)

    repo = sub.add_parser("repo")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)

    p = repo_sub.add_parser("list")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_repo_list)

    p = repo_sub.add_parser("add")
    p.add_argument("id")
    p.add_argument("repo")
    p.add_argument("--branch")
    p.set_defaults(func=cmd_repo_add)

    p = repo_sub.add_parser("adopt")
    p.add_argument("id")
    p.add_argument("path")
    p.add_argument("--name")
    p.set_defaults(func=cmd_repo_adopt)

    p = repo_sub.add_parser("remove")
    p.add_argument("id")
    p.add_argument("repo")
    p.add_argument("--delete-worktree", action="store_true")
    p.set_defaults(func=cmd_repo_remove)

    p = repo_sub.add_parser("refresh")
    p.add_argument("id")
    p.add_argument("repo", nargs="?")
    p.set_defaults(func=cmd_repo_refresh)

    p = sub.add_parser("cleanup-plan")
    p.add_argument("id", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--issues", action="store_true")
    p.add_argument("--repo")
    p.add_argument("--write", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cleanup_plan)

    p = sub.add_parser("cleanup")
    p.add_argument("id", nargs="*")
    p.add_argument("--item", action="append")
    p.add_argument("--from-plan")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("sandcastle")
    p.add_argument("id")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--command")
    p.add_argument("--reconcile-result")
    p.add_argument("--init-runner", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_sandcastle)

    issue = sub.add_parser("issue")
    issue_sub = issue.add_subparsers(dest="issue_command", required=True)

    p = issue_sub.add_parser("create")
    p.add_argument("workspace_id")
    p.add_argument("issue_id")
    p.add_argument("--title", required=True)
    p.add_argument("--type", default="AFK")
    p.add_argument("--status", default="planned")
    p.add_argument("--blocked-by", action="append")
    p.add_argument("--acceptance", action="append")
    p.add_argument("--body")
    p.add_argument("--branch")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_issue_create)

    p = issue_sub.add_parser("list")
    p.add_argument("workspace_id")
    p.add_argument("--status")
    p.add_argument("--type")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_issue_list)

    p = issue_sub.add_parser("ready")
    p.add_argument("workspace_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_issue_ready)

    p = issue_sub.add_parser("show")
    p.add_argument("workspace_id")
    p.add_argument("issue_id")
    p.set_defaults(func=cmd_issue_show)

    p = issue_sub.add_parser("set-status")
    p.add_argument("workspace_id")
    p.add_argument("issue_id")
    p.add_argument("status")
    p.add_argument("--review-status")
    p.add_argument("--branch")
    p.add_argument("--note")
    p.set_defaults(func=cmd_issue_set_status)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
