import yaml

from .common import now, slug


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
LEGACY_SANDCASTLE_KEYS = {"type", "reviewStatus"}


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


def _sandcastle_block(meta):
    """Return meta's sandcastle block as a dict, treating a non-dict value as empty."""
    block = meta.get("sandcastle")
    return dict(block) if isinstance(block, dict) else {}


def normalize_legacy_sandcastle_fields(meta):
    """Reshape legacy top-level Sandcastle fields into a nested sandcastle block.

    A legacy top-level field only fills in a key the nested block doesn't
    already define; an existing block value always wins on conflict. Legacy
    top-level keys are removed either way so they can never resurface.
    """
    result = dict(meta)
    had_block_key = "sandcastle" in result
    block = _sandcastle_block(result)
    if "type" in result:
        block.setdefault("type", result.pop("type"))
    if "reviewStatus" in result:
        block.setdefault("reviewStatus", result.pop("reviewStatus"))
    if block or had_block_key:
        result["sandcastle"] = block
    return result


def sandcastle_issue_meta(meta):
    """Read Sandcastle execution fields from nested or legacy top-level frontmatter.

    A legacy top-level field is only consulted for a key the nested block
    doesn't already define; the nested block always wins on conflict, mirroring
    the write-side merge policy in `normalize_legacy_sandcastle_fields`.
    """
    block = _sandcastle_block(meta)
    if "type" not in block and "type" in meta:
        block["type"] = meta.get("type")
    if "reviewStatus" not in block and "reviewStatus" in meta:
        block["reviewStatus"] = meta.get("reviewStatus")
    return {
        "type": str(block.get("type") or "AFK").upper(),
        "reviewStatus": block.get("reviewStatus") or "pending",
    }


def resolve_issue_repo(meta, all_repos):
    """Return the workspace repo name an issue belongs to, if known.

    `all_repos` must be the FULL workspace repo list, never a scope-filtered
    subset (e.g. from `--repo`) — the single-repo-workspace default and the
    "known repo names" check are both defined relative to the whole
    workspace, independent of what a given planning invocation is scoped to.
    """
    explicit = meta.get("repo")
    names = {str(repo.get("name") or "") for repo in all_repos}
    if explicit is not None:
        name = str(explicit)
        if name in names:
            return name
        return None
    if len(all_repos) == 1:
        return all_repos[0].get("name")
    return None


def resolve_issue_repo_or_raw(meta, all_repos):
    """Like `resolve_issue_repo`, but fall back to a stale/unknown explicit `repo:`
    value instead of dropping it, so a repo rename/removal is reported as staleness
    against the *original* name rather than silently resolved to `None`.
    """
    repo = resolve_issue_repo(meta, all_repos)
    if repo is not None:
        return repo
    explicit = meta.get("repo")
    return str(explicit) if explicit is not None else None


def default_issue_repo(repos):
    if len(repos) == 1:
        return repos[0].get("name")
    return None


def normalize_issue_meta(ledger, workspace_id, issue_id, meta, *, sandcastle=False):
    if sandcastle:
        meta = normalize_legacy_sandcastle_fields(meta)
    issue_id = str(meta.get("id") or issue_id)
    blocked_by = meta.get("blockedBy") or []
    if isinstance(blocked_by, str):
        blocked_by = [blocked_by]
    status = str(meta.get("status") or "planned")
    created = meta.get("createdAt") or now()
    normalized = {
        "id": issue_id,
        "title": meta.get("title") or issue_id,
        "status": status,
        "blockedBy": [str(item) for item in blocked_by],
        "branch": meta.get("branch") or sandcastle_branch(workspace_id, issue_id),
        "createdAt": created,
        "updatedAt": now(),
    }
    if sandcastle:
        block = _sandcastle_block(meta)
        issue_type = str(block.get("type") or "AFK").upper()
        if issue_type not in ISSUE_TYPES:
            raise SystemExit(f"Invalid issue type for {issue_id}: {issue_type}")
        block["type"] = issue_type
        block["reviewStatus"] = block.get("reviewStatus") or "pending"
        normalized["sandcastle"] = block
    skip_keys = LEGACY_SANDCASTLE_KEYS if sandcastle else set()
    for key, value in meta.items():
        if key not in normalized and key not in skip_keys:
            normalized[key] = value
    return normalized


def write_issue(ledger, workspace_id, issue_id, meta, body, *, sandcastle=False, skip_plan_invalidation=False):
    path = ledger.issue_path(workspace_id, issue_id)
    old_meta = None
    if path.exists():
        old_meta = read_issue(path)["meta"]
    meta = normalize_issue_meta(ledger, workspace_id, issue_id, meta, sandcastle=sandcastle)
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=False).strip()
    path.write_text(f"---\n{frontmatter}\n---\n{body.strip()}\n", encoding="utf-8")
    if old_meta is not None and not skip_plan_invalidation:
        from .sandcastle_state import maybe_invalidate_plan_for_issue_edit

        maybe_invalidate_plan_for_issue_edit(
            ledger, workspace_id, meta["id"], old_meta, meta
        )
    return path, meta


def read_issue(path):
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    issue_id = meta.get("id") or path.stem
    return {"path": path, "meta": meta, "body": body, "id": str(issue_id)}


def iter_issues(ledger, workspace_id):
    root = ledger.issues_dir(workspace_id)
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


def issue_index(ledger, workspace_id):
    return {issue["id"]: issue for issue in iter_issues(ledger, workspace_id)}


def blockers_complete(index, issue):
    for blocker_id in issue["meta"].get("blockedBy") or []:
        blocker = index.get(str(blocker_id))
        if not blocker:
            return False
        if blocker["meta"].get("status") not in ISSUE_DONE_STATUSES:
            return False
    return True


def ready_issues(ledger, workspace_id, *, sandcastle=False):
    index = issue_index(ledger, workspace_id)
    ready = []
    blocked_hitl = []
    for issue in index.values():
        meta = issue["meta"]
        status = meta.get("status")
        issue_type = sandcastle_issue_meta(meta)["type"]
        if status not in ISSUE_ACTIVE_STATUSES:
            continue
        if not blockers_complete(index, issue):
            continue
        if sandcastle:
            if issue_type == "AFK":
                ready.append(issue)
            elif issue_type == "HITL":
                blocked_hitl.append(issue)
        else:
            ready.append(issue)
    ready.sort(key=lambda item: str(item["meta"].get("id", "")))
    blocked_hitl.sort(key=lambda item: str(item["meta"].get("id", "")))
    return ready, blocked_hitl


def issue_payload(issue, *, repo=None):
    meta = issue["meta"]
    sandcastle = sandcastle_issue_meta(meta)
    payload = {
        "id": meta.get("id"),
        "title": meta.get("title"),
        "type": sandcastle["type"],
        "status": meta.get("status"),
        "blockedBy": meta.get("blockedBy") or [],
        "branch": meta.get("branch"),
        "reviewStatus": sandcastle["reviewStatus"],
        "path": str(issue["path"]),
        "body": issue["body"].strip(),
    }
    if repo is not None:
        payload["repo"] = repo
    elif meta.get("repo") is not None:
        payload["repo"] = meta.get("repo")
    return payload


def set_issue_status(
    ledger,
    workspace_id,
    issue_id,
    status,
    review_status=None,
    branch=None,
    extra=None,
    *,
    sandcastle=False,
    skip_plan_invalidation=False,
):
    path = ledger.issue_path(workspace_id, issue_id)
    if not path.exists():
        raise SystemExit(f"Issue not found: {issue_id}")
    issue = read_issue(path)
    meta = dict(issue["meta"])
    old_status = meta.get("status")
    meta["status"] = status
    meta["updatedAt"] = now()
    if sandcastle:
        meta = normalize_legacy_sandcastle_fields(meta)
        block = _sandcastle_block(meta)
        if review_status:
            block["reviewStatus"] = review_status
        meta["sandcastle"] = block
    if branch:
        meta["branch"] = branch
    for key, value in (extra or {}).items():
        meta[key] = value
    path, meta = write_issue(
        ledger,
        workspace_id,
        issue_id,
        meta,
        issue["body"],
        sandcastle=sandcastle,
        skip_plan_invalidation=skip_plan_invalidation,
    )
    return path, meta, old_status


def maybe_mark_user_review(ledger, workspace_id):
    issues = iter_issues(ledger, workspace_id)
    if not issues:
        return
    if any(issue["meta"].get("status") not in ISSUE_DONE_STATUSES for issue in issues):
        return
    workspace = ledger.load(workspace_id)
    if workspace.state in {"closed", "dev-complete", "user-review"}:
        return
    old_state = workspace.state
    workspace.set_state("user-review")
    ledger.save(workspace)
    ledger.append_note(
        workspace_id,
        f"State changed from `{old_state}` to `user-review` because all local issues are complete.",
    )
