import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

import yaml


SKILL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = Path("~/workspaces/config.yaml").expanduser()

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

WORKSPACE_POINTER = ".workspace-id"

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


def now():
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def timestamp_slug():
    """Filename-safe local timestamp used to name generated run artifacts."""
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")


def expand(value):
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value))
    return value


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=False)


def read_yaml(path):
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def parse_json(text, default=None):
    """Parse `text` as JSON, returning `default` when it is empty or malformed."""
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def project(data, *fields):
    """Project a dict down to a fixed subset of fields, defaulting missing ones to None."""
    return {field: data.get(field) for field in fields}


def repo_identity_snapshot(repos):
    """Comparable snapshot of a repo list's identity (name + worktree path).

    Used to detect whether "the set of repos" changed for Sandcastle plan
    staleness/invalidation checks, independent of other repo fields.
    """
    return sorted(
        [
            {
                "name": repo.get("name"),
                "worktreePath": repo.get("worktreePath"),
            }
            for repo in repos or []
        ],
        key=lambda item: str(item.get("name") or ""),
    )


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


def read_workspace_pointer(start):
    """Return the workspace id from the nearest `.workspace-id` at or above `start`."""
    current = Path(start).resolve()
    for parent in [current, *current.parents]:
        pointer = parent / WORKSPACE_POINTER
        if pointer.exists():
            return pointer.read_text(encoding="utf-8").strip()
    return None


def write_workspace_pointer(root, workspace_id):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    pointer = root / WORKSPACE_POINTER
    pointer.write_text(workspace_id + "\n", encoding="utf-8")
    return pointer


def relative_or_absolute(path, base):
    path = Path(path).expanduser()
    base = Path(base).expanduser()
    try:
        return os.path.relpath(path.resolve(), base.resolve())
    except FileNotFoundError:
        return os.path.relpath(path.absolute(), base.absolute())


def template(name):
    return (SKILL_ROOT / "templates" / name).read_text(encoding="utf-8")
