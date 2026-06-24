import datetime as dt
import json
import shutil
from pathlib import Path

from .common import DONE_STATES, now, run
from .git import (
    branch_exists,
    branch_merged,
    git_info,
    is_linked_worktree,
    path_is_under,
    remove_linked_worktree,
    worktree_dirty,
    worktree_for_branch,
)
from .issues import ISSUE_DONE_STATUSES, issue_index, iter_issues, set_issue_status
from .ledger import WorkspaceLedger


class CleanupSafety:
    def __init__(self, config, select_repo=None):
        self.config = config
        self.ledger = WorkspaceLedger(config)
        self.select_repo = select_repo

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

        soft_reasons = []
        hard_reasons = []
        if data.get("state") not in DONE_STATES:
            soft_reasons.append("workspace is not closed or dev-complete")
        if dirty_repos:
            soft_reasons.append("repo worktree has uncommitted changes")
        if non_linked_repos:
            hard_reasons.append("repo is not a linked git worktree")
        if external_repos:
            hard_reasons.append("repo worktree is outside the workspace root")
        if extra_paths:
            soft_reasons.append("workspace root contains extra files")
        if open_prs:
            soft_reasons.append("workspace has open PR snapshots")
        if self.ledger.sandcastle_run_active(workspace_id):
            hard_reasons.append("sandcastle run is active")

        reasons = hard_reasons + soft_reasons
        if reasons:
            candidate["reason"] = "; ".join(reasons)
            if hard_reasons:
                candidate["recommendedAction"] = "needs-review"
            else:
                candidate["recommendedAction"] = "force-eligible"
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

    def _remove_workspace_worktrees(self, data, *, force=False):
        root = self.ledger.workspace_root_path(data["id"])
        for repo in data.get("repos", []):
            remove_linked_worktree(
                repo.get("worktreePath"),
                root,
                source_path=repo.get("sourcePath"),
                force=force,
            )

    def _remove_workspace_root(self, workspace_id):
        root = self.ledger.workspace_root_path(workspace_id)
        if root.exists():
            shutil.rmtree(root)

    def _finalize_workspace_cleanup(self, data, candidate, action, note):
        workspace_id = data["id"]
        data["state"] = "closed"
        data["cleanupStatus"] = "done"
        data["cleanupAt"] = now()
        data["cleanupAction"] = action
        self.ledger.save(data)
        self.ledger.append_note(workspace_id, note)
        return {**candidate, "cleanupStatus": data["cleanupStatus"], "cleanupAction": action}

    def _force_cleanup_note(self, candidate):
        parts = [f"Force-cleaned workspace. {candidate['reason']}."]
        if candidate.get("aheadOfBase"):
            parts.append(f"Ahead of base: {candidate['aheadOfBase']}.")
        parts.append("Task branches kept on source repos.")
        return " ".join(parts)

    def cleanup_workspace(self, workspace_id, force=False):
        candidate = self.workspace_candidate(workspace_id)
        action = candidate["recommendedAction"]

        if action == "needs-review":
            raise SystemExit(
                f"Refusing cleanup for {workspace_id}: {candidate['reason']} ({action})"
            )

        if action == "mark-done":
            data = self.ledger.load(workspace_id)
            return self._finalize_workspace_cleanup(
                data,
                candidate,
                "mark-done",
                "Cleaned up workspace with action `mark-done`.",
            )

        if action == "force-eligible" and not force:
            raise SystemExit(
                f"Refusing cleanup for {workspace_id}: {candidate['reason']} "
                f"({action}). Pass --force."
            )

        data = self.ledger.load(workspace_id)
        if action in {"force-eligible", "safe-to-remove"}:
            self._remove_workspace_worktrees(data, force=action == "force-eligible")
            self._remove_workspace_root(workspace_id)
            cleanup_action = "force-removed" if action == "force-eligible" else "safe-to-remove"
            note = (
                self._force_cleanup_note(candidate)
                if action == "force-eligible"
                else "Cleaned up workspace with action `safe-to-remove`."
            )
            return self._finalize_workspace_cleanup(data, candidate, cleanup_action, note)

        raise SystemExit(
            f"Refusing cleanup for {workspace_id}: {candidate['reason']} ({action})"
        )

    def issue_candidates(self, workspace_ids, repo_name=None):
        if self.select_repo is None:
            raise SystemExit("CleanupSafety requires a repo selector for issue cleanup.")
        candidates = []
        for workspace_id in workspace_ids:
            data = self.ledger.load(workspace_id)
            repo = self.select_repo(data, repo_name)
            candidates.extend(self.issue_candidates_for_workspace(workspace_id, data, repo))
        return candidates

    def issue_candidates_for_workspace(self, workspace_id, data, repo):
        repo_path = repo.get("worktreePath")
        task_branch = repo.get("branch")
        repo_exists = bool(repo_path and Path(repo_path).exists())
        candidates = []
        for issue in iter_issues(self.ledger, workspace_id):
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

        issue = issue_index(self.ledger, workspace_id).get(issue_id)
        extra = {
            "cleanupStatus": "done",
            "cleanupAt": now(),
            "cleanupAction": action,
        }
        _, meta, _ = set_issue_status(
            self.ledger,
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
