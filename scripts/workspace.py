#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import re
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


def load_workspace(config, workspace_id):
    path = workspace_yaml_path(config, workspace_id)
    if not path.exists():
        raise SystemExit(f"Workspace not found: {workspace_id}")
    return read_yaml(path)


def save_workspace(config, data):
    data["updatedAt"] = now()
    write_yaml(workspace_yaml_path(config, data["id"]), data)


def template(name):
    return (SKILL_ROOT / "templates" / name).read_text(encoding="utf-8")


def append_note(config, workspace_id, text):
    path = ledger_dir(config, workspace_id) / "notes.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(template("notes.md"), encoding="utf-8")
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n## {stamp}\n{text.strip()}\n")


def ensure_workspace(config, workspace_id, title=None, state="planning"):
    root = ledger_dir(config, workspace_id)
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
    write_yaml(meta, data)
    (root / "spec.md").write_text(
        template("spec.md").format(id=workspace_id, title=data["title"]),
        encoding="utf-8",
    )
    (root / "notes.md").write_text(template("notes.md"), encoding="utf-8")
    append_note(config, workspace_id, f"Created workspace `{workspace_id}`.")
    return data


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


def cmd_create(args):
    config = load_config()
    data = ensure_workspace(config, args.id, args.title, args.state)
    workspace_root = Path(config["workspace_root"]) / args.id
    if args.repo:
        workspace_root.mkdir(parents=True, exist_ok=True)
        (workspace_root / ".workspace-id").write_text(args.id + "\n", encoding="utf-8")
    for repo in args.repo or []:
        source = Path(config["source_root"]) / repo
        dest = workspace_root / repo
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
                    args.branch or args.id,
                    str(dest),
                    f"{config['base_remote']}/{config['base_branch']}",
                ],
                capture=True,
            )
        info = git_info(dest, config)
        upsert_repo(
            data,
            {
                "name": repo,
                "sourcePath": str(source),
                "worktreePath": str(dest),
                "branch": info["branch"],
                "baseRemote": config["base_remote"],
                "baseBranch": config["base_branch"],
                "pushRemote": config["push_remote"],
                "remote": info["remote"],
            },
        )
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
    p.add_argument("--state", default="planning")
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

    p = sub.add_parser("note")
    p.add_argument("id")
    p.add_argument("text")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("close")
    p.add_argument("id")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("sync-github")
    p.add_argument("id")
    p.set_defaults(func=cmd_sync_github)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
