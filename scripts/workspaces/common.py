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


def repo_projection(repo, *fields):
    """Project a repo dict down to a plan/runner-safe subset of fields."""
    return {field: repo.get(field) for field in fields}


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


_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_workspace_id(workspace_id):
    """Validate a workspace id used to build ledger/workspace filesystem paths.

    Workspace ids are joined onto trusted roots (`ledger_root`, `workspace_root`)
    to derive read/write/delete paths, so an id containing path separators or
    `..` segments would let an operation escape those roots. Reject anything that
    is not a plain identifier before it can reach the filesystem.
    """
    text = str(workspace_id or "").strip()
    if not _WORKSPACE_ID_RE.fullmatch(text):
        raise SystemExit(
            f"Invalid workspace id: {workspace_id!r}. Use letters, digits, '.', '_', or '-' "
            "and no path separators."
        )
    return text


def validate_repo_name(repo_name):
    """Validate a repo name that is joined onto `source_root`/`workspace_root`.

    Mirrors `validate_workspace_id`: the name becomes a path segment for the
    source checkout and worktree destination, so path separators or `..`
    segments must be rejected to keep both inside their intended roots.
    """
    text = str(repo_name or "").strip()
    if not _REPO_NAME_RE.fullmatch(text):
        raise SystemExit(
            f"Invalid repo name: {repo_name!r}. Use letters, digits, '.', '_', or '-' "
            "and no path separators."
        )
    return text


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


def relative_or_absolute(path, base):
    path = Path(path).expanduser()
    base = Path(base).expanduser()
    try:
        return os.path.relpath(path.resolve(), base.resolve())
    except FileNotFoundError:
        return os.path.relpath(path.absolute(), base.absolute())


def template(name):
    return (SKILL_ROOT / "templates" / name).read_text(encoding="utf-8")
