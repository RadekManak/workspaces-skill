"""Workspace domain object: the in-memory shape of workspace.yaml.

`WorkspaceLedger` (ledger.py) owns filesystem I/O and hands back/accepts
`Workspace` instances; this module owns the schema plus the domain rules
around repos, links, and the Sandcastle plan pointer. Mutations are
in-memory only -- callers persist via `WorkspaceLedger.save(workspace)`
when ready, so multiple mutations can be batched into one write.

Rarely-touched fields (title, createdAt, spec text, jira links, etc.) have
no dedicated accessor; read them via `to_dict()`, which returns a deep copy
so callers can never mutate the real object through it.
"""
import copy


class Workspace:
    def __init__(self, data):
        self._data = data

    @property
    def id(self):
        return self._data["id"]

    # ---- state ----

    @property
    def state(self):
        return self._data.get("state")

    def set_state(self, value):
        self._data["state"] = value

    # ---- cleanup ----

    @property
    def cleanup_status(self):
        return self._data.get("cleanupStatus")

    def mark_cleanup_done(self, *, action, at):
        self._data["state"] = "closed"
        self._data["cleanupStatus"] = "done"
        self._data["cleanupAt"] = at
        self._data["cleanupAction"] = action

    @property
    def vscode_workspace_path(self):
        return self._data.get("vscodeWorkspacePath")

    def set_vscode_workspace_path(self, value):
        self._data["vscodeWorkspacePath"] = value

    def clear_vscode_workspace_path(self):
        self._data.pop("vscodeWorkspacePath", None)

    def set_updated_at(self, value):
        self._data["updatedAt"] = value

    # ---- repos ----

    @property
    def repos(self):
        return copy.deepcopy(self._data.get("repos", []))

    def find_repo(self, name):
        for repo in self._data.get("repos", []):
            if repo.get("name") == name:
                return copy.deepcopy(repo)
        raise SystemExit(f"Repo not found in workspace: {name}")

    def upsert_repo(self, repo):
        """Add `repo`, or merge onto an existing entry matched by worktreePath or name."""
        repos = self._data.setdefault("repos", [])
        for index, existing in enumerate(repos):
            if (
                existing.get("worktreePath") == repo.get("worktreePath")
                or existing.get("name") == repo.get("name")
            ):
                repos[index] = {**existing, **repo}
                return
        repos.append(dict(repo))

    def remove_repo(self, name):
        repos = self._data.get("repos", [])
        for index, repo in enumerate(repos):
            if repo.get("name") == name:
                return repos.pop(index)
        raise SystemExit(f"Repo not found in workspace: {name}")

    def replace_repo(self, name, updated):
        repos = self._data.get("repos", [])
        for index, repo in enumerate(repos):
            if repo.get("name") == name:
                repos[index] = updated
                return
        raise SystemExit(f"Repo not found in workspace: {name}")

    def replace_all_repos(self, repos):
        self._data["repos"] = list(repos)

    # ---- links ----

    @property
    def github_prs(self):
        return copy.deepcopy(self._data.get("links", {}).get("githubPrs", []))

    def record_github_pr(self, item):
        """Upsert a GitHub PR snapshot, matched by (repo, branch); appends otherwise."""
        links = self._data.setdefault("links", {})
        prs = links.setdefault("githubPrs", [])
        for index, existing in enumerate(prs):
            if existing.get("repo") == item.get("repo") and existing.get("branch") == item.get("branch"):
                prs[index] = {**existing, **item}
                return
        prs.append(dict(item))

    def append_github_pr(self, item):
        """Unconditionally append a PR lifecycle event, keeping full history."""
        links = self._data.setdefault("links", {})
        links.setdefault("githubPrs", []).append(dict(item))

    # ---- sandcastle plan pointer ----

    @property
    def current_sandcastle_plan(self):
        return copy.deepcopy((self._data.get("sandcastle") or {}).get("currentPlan"))

    def targeted_issue_ids(self):
        pointer = self.current_sandcastle_plan
        if not pointer:
            return set()
        return {str(item) for item in pointer.get("targetedIssueIds") or []}

    def set_current_sandcastle_plan(self, pointer):
        sandcastle = dict(self._data.get("sandcastle") or {})
        sandcastle["currentPlan"] = pointer
        self._data["sandcastle"] = sandcastle

    def clear_current_sandcastle_plan(self):
        """Drop the current-plan pointer. Returns whether there was one to drop."""
        sandcastle = dict(self._data.get("sandcastle") or {})
        if "currentPlan" not in sandcastle:
            return False
        sandcastle.pop("currentPlan", None)
        if sandcastle:
            self._data["sandcastle"] = sandcastle
        else:
            self._data.pop("sandcastle", None)
        return True

    # ---- escape hatch for the long tail ----

    def to_dict(self):
        return copy.deepcopy(self._data)
