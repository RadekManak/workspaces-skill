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


def normalize_issue_meta(ledger, workspace_id, issue_id, meta):
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


def write_issue(ledger, workspace_id, issue_id, meta, body):
    meta = normalize_issue_meta(ledger, workspace_id, issue_id, meta)
    path = ledger.issue_path(workspace_id, meta["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=False).strip()
    path.write_text(f"---\n{frontmatter}\n---\n{body.strip()}\n", encoding="utf-8")
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


def ready_issues(ledger, workspace_id):
    index = issue_index(ledger, workspace_id)
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


def set_issue_status(
    ledger,
    workspace_id,
    issue_id,
    status,
    review_status=None,
    branch=None,
    extra=None,
):
    path = ledger.issue_path(workspace_id, issue_id)
    if not path.exists():
        raise SystemExit(f"Issue not found: {issue_id}")
    issue = read_issue(path)
    meta = issue["meta"]
    old_status = meta.get("status")
    meta["status"] = status
    meta["updatedAt"] = now()
    if review_status:
        meta["reviewStatus"] = review_status
    if branch:
        meta["branch"] = branch
    for key, value in (extra or {}).items():
        meta[key] = value
    path, meta = write_issue(ledger, workspace_id, issue_id, meta, issue["body"])
    return path, meta, old_status


def maybe_mark_user_review(ledger, workspace_id):
    issues = iter_issues(ledger, workspace_id)
    if not issues:
        return
    if any(issue["meta"].get("status") not in ISSUE_DONE_STATUSES for issue in issues):
        return
    data = ledger.load(workspace_id)
    if data.get("state") in {"closed", "dev-complete", "user-review"}:
        return
    old_state = data.get("state")
    data["state"] = "user-review"
    ledger.save(data)
    ledger.append_note(
        workspace_id,
        f"State changed from `{old_state}` to `user-review` because all local issues are complete.",
    )
