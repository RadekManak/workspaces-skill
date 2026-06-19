---
name: workspaces
description: Manage local AI-assisted development workspaces with a plain-file ledger, mutable specs, append-only notes, local git worktrees, explicit GitHub/Jira link tracking, and adoption of existing worktrees. Use when Codex needs to create, adopt, inspect, plan, update, review, sync, or close a development workspace across one or more repos.
---

# Workspaces

Use this skill to keep a local workspace ledger up to date while doing development work.

## Layout

- Skill/tooling repo: this skill directory
- Runtime config: `~/workspaces/config.yaml`
- Runtime ledger: `~/workspaces/_ledger/workspaces/<workspace-id>/`
- Code worktrees: `~/workspaces/<workspace-id>/<repo>/`
- VS Code workspace file: `~/workspaces/<workspace-id>/<workspace-id>.code-workspace`

The ledger is canonical. Code workspaces may contain `.workspace-id` and a generated `.code-workspace` file, but must not duplicate durable metadata.

## Files

Each workspace ledger contains:

```text
workspace.yaml  # machine-readable metadata, links, state, repos
spec.md         # mutable current spec/plan
issues/         # local implementation issue markdown files
notes.md        # append-only-ish chronological notes
runs/           # large/full agent run outputs
```

The generated VS Code workspace file includes the ledger folder and every repo worktree recorded in `workspace.yaml`. Treat it as generated output: update the ledger through helper commands instead of hand-editing the workspace file.

Use one mutable `spec.md`. Append notes at the bottom of `notes.md`.

## Configuration

Before using the skill, ensure config exists:

```bash
scripts/workspace.py init-config
```

The config is simple YAML. Defaults are shown in `templates/config.yaml`.

Important defaults:

- `source_root`: `~/git`
- `workspace_root`: `~/workspaces`
- `ledger_root`: `~/workspaces/_ledger`
- `base_remote`: `origin`
- `base_branch`: `main`
- `push_remote`: `fork`

## Commands

Use the helper script for deterministic file and git operations. Paths below are relative to this skill directory, so an installed copy can run from any skill root:

```bash
scripts/workspace.py create <id> --title "<title>"
scripts/workspace.py create <id> --repo <repo>
scripts/workspace.py adopt <path> --id <id>
scripts/workspace.py open <id>
scripts/workspace.py doctor <id> [--fix]
scripts/workspace.py list [--active] [--state <state>] [--all]
scripts/workspace.py status [id]
scripts/workspace.py note <id> "note text"
scripts/workspace.py state <id> <state>
scripts/workspace.py repo list <id>
scripts/workspace.py repo add <id> <repo>
scripts/workspace.py repo adopt <id> <path> [--name <name>]
scripts/workspace.py repo refresh <id> [repo]
scripts/workspace.py repo remove <id> <repo> [--delete-worktree]
scripts/workspace.py issue create <id> <issue-id> --title "<title>"
scripts/workspace.py issue list <id> [--all] [--status <status>] [--type AFK|HITL]
scripts/workspace.py issue ready <id> [--json]
scripts/workspace.py issue show <id> <issue-id>
scripts/workspace.py issue set-status <id> <issue-id> <status>
scripts/workspace.py sandcastle <id> [--repo <repo>] [--json]
scripts/workspace.py sandcastle <id> --init-runner [--force]
scripts/workspace.py sandcastle <id> --execute --command "<repo-local command>"
scripts/workspace.py sandcastle <id> --reconcile-result <result.json>
scripts/workspace.py cleanup-plan <id> [<id> ...] [--json] [--write]
scripts/workspace.py cleanup-plan --all --write
scripts/workspace.py cleanup <id> [<id> ...]
scripts/workspace.py cleanup --from-plan <plan.json> <id> [<id> ...]
scripts/workspace.py cleanup-plan <id> --issues [--json]
scripts/workspace.py cleanup --item <id>:<issue-id> [--item <id>:<issue-id> ...]
scripts/workspace.py close <id>
scripts/workspace.py sync-github <id>
scripts/workspace.py pr <id> opened|feedback|merged
```

If a command is missing a behavior, inspect and edit the ledger directly rather than inventing another tracker.

## Workflow

### Create

Create a workspace to capture the user's raw request and optional repo worktrees. Creation defaults to `captured`.

Use `--input` to preserve the user's actual instruction in `spec.md`. Repos are created as git worktrees from `source_root/<repo>` into `workspace_root/<workspace-id>/<repo>`, on a branch named after the workspace ID. Do not push during create.

Create also writes `<workspace-id>.code-workspace` under the workspace root. Ledger-only workspaces still get a VS Code workspace file containing the ledger folder.

During ordinary creation, do not explore the project or groom the task. Verify only what is needed to create the workspace: config, source repo existence, branch/base availability, and resulting local status. Ask for clarification only if creation is impossible or the request is immediately contradictory.

### Adopt

Adopt existing Cursor/manual worktrees with `adopt`. Mixed branch names are allowed. Warn in notes if mixed branches matter, but do not block.

Adopt updates the generated VS Code workspace file to include the adopted repo path.

### Open

Use `open <id>` to launch the generated `.code-workspace` using `editor_command` from config. Use `open <id> --print` when an agent needs the path without launching an editor.

### Doctor

Use `doctor <id>` when the workspace feels inconsistent. It checks missing ledger scaffolding, missing or stale `.workspace-id`, missing or stale `.code-workspace`, missing repos, branch drift, and dirty repos.

Use `doctor <id> --fix` only for safe derived-file repairs: missing ledger dirs/files, `.workspace-id`, and the generated `.code-workspace`. It must not discard user code changes or rewrite repo branches.

### Repos

Use `repo add` to add another configured source repo to an existing workspace as a linked worktree. Use `repo adopt` to add an existing checkout or worktree to the workspace metadata.

Use `repo refresh` after branch or remote changes to update repo metadata from local git. Use `repo remove` to remove a repo from workspace metadata. Pass `--delete-worktree` only after the user explicitly wants the clean linked worktree removed; the command refuses dirty worktrees, non-linked checkouts, and paths outside the workspace root.

### Plan

Write or groom `spec.md` only when the user asks to scope, plan, or start the work. Put Jira input in `## Input` when available. Keep the spec implementable and current.

When the task is scoped enough for an agent to start without re-asking basics, set state to `scoped`. When implementation begins, set state to `in-progress`.

### Issues

Local workspace issues live in `issues/*.md` under the workspace ledger. They are the execution contract for Sandcastle-style AFK work and do not need to be GitHub issues.

Use `issue create` to write issues with YAML frontmatter. Each issue tracks an id, title, `AFK` or `HITL` type, status, blockers, deterministic Sandcastle branch, and review status.

Use `issue ready` to find dependency-ready work. It reports:

- ready AFK issues that Sandcastle can run
- ready HITL issues that require user input

Use `issue ready --json` when another script or runner needs structured input.

Mark merged Sandcastle child branches with `issue set-status <id> <issue-id> merged`. Do not delete child branches or worktrees automatically; cleanup is user-triggered later.

### Sandcastle

Use `sandcastle <id>` to prepare a run plan from dependency-ready AFK issues. The command writes a JSON artifact under `runs/` and reports ready AFK and HITL counts.

Use `sandcastle <id> --json` when another script needs the full plan payload.

Use `sandcastle <id> --init-runner` to scaffold `.sandcastle/main.mts`, `implement-prompt.md`, and `review-prompt.md` into the selected repo worktree. Pass `--force` to overwrite existing generated files.

Use `sandcastle <id> --execute --command "<repo-local command>"` to run a repo-local Sandcastle entrypoint. The command runs from the selected repo worktree and receives:

```text
WORKSPACE_ID
WORKSPACE_LEDGER_DIR
WORKSPACE_SANDCASTLE_PLAN
WORKSPACE_SANDCASTLE_RESULT
WORKSPACE_REPO_NAME
WORKSPACE_REPO_PATH
```

The repo-local runner should write `WORKSPACE_SANDCASTLE_RESULT` as JSON:

```json
{
  "issues": [
    {
      "id": "1",
      "status": "merged",
      "reviewStatus": "approved",
      "commits": [{ "sha": "abc123" }],
      "cleanupStatus": "pending"
    }
  ]
}
```

Use `sandcastle <id> --reconcile-result <result.json>` to apply a result manually. Reconciliation updates issue frontmatter, appends notes, and moves the workspace to `user-review` when every local issue is complete.

The generated runner uses a bounded loop for each issue:

```text
implementer -> reviewer -> implementer fixes reviewer feedback -> reviewer
```

Approved issue branches are merged back into the workspace task branch serially after the parallel implement/review pipelines finish. This avoids concurrent writes to the task branch.

Only one Sandcastle execution may run per workspace at a time. The helper writes `runs/active-sandcastle-run.json` while `sandcastle --execute` is active and refuses overlapping executions.

If an approved child branch cannot merge into the workspace task branch, mark the issue `blocked-hitl` with `reviewStatus: approved`, preserve the child branch, and record the merge error. Do not treat merge conflicts as generic runner failures.

The first version supports one repo automatically. If the workspace has multiple repos, pass `--repo <repo>`.

### Cleanup

Cleanup is HITL. Always run `cleanup-plan` first and show the user the result.

By default, cleanup is workspace-level. `cleanup-plan <workspace-id>` is read-only and reports whether a whole workspace can be removed: state, repo worktree count, dirty repo count, ahead count, open PR snapshots, unexpected files under the workspace root, and the recommended action.

Use `cleanup-plan --all --write` for bulk cleanup review. It writes an auditable JSON plan under `ledger_root/runs/`. After the user approves specific workspace ids from that output, run `cleanup --from-plan <plan.json> <workspace-id> ...`. Cleanup revalidates each approved workspace against the plan and refuses stale plans.

Only run `cleanup <workspace-id>` or `cleanup --from-plan <plan.json> <workspace-id>` after the user explicitly approves that workspace. Workspace cleanup removes clean linked worktrees under the configured workspace root, removes the workspace root including the generated `.code-workspace` file, keeps the ledger, and marks the workspace closed with `cleanupStatus: done`.

Workspace cleanup refuses unsafe workspaces: active states, dirty repos, non-linked git checkouts, worktrees outside the workspace root, unexpected files in the workspace root, or open PR snapshots.

Per-issue branch cleanup remains available for Sandcastle child branches, but it is not the normal cleanup path. Use `cleanup-plan <workspace-id> --issues` to inspect completed issues with `cleanupStatus: pending`, then `cleanup --item <workspace-id>:<issue-id>` for approved issue-level cleanup.

Issue cleanup refuses unsafe items. It only handles:

- `safe-to-delete`: branch is merged into the task branch and any child worktree is clean.
- `mark-done`: branch and child worktree are already gone.

Issue cleanup must not delete the workspace task branch and must not touch failed, needs-fix, blocked-HITL, unmerged, or dirty work.

### Status

Use `status` for local inspection. It reads the ledger and local git state only. Do not perform Jira/GitHub network calls during normal status.

Use `list` as the first overview after time away. It defaults to active workspaces, excluding `closed` and `dev-complete`, and shows state, updated date, repos, local dirty/ahead summary, PR count, and title. Use `list --state user-review` to find items waiting on the user, `list --state review-feedback` for PR comments that need work, and `list --all` to include archived work.

### GitHub Actions Rule

Whenever you push a branch, create a PR, update a PR, merge a PR, or observe that a PR was merged, update the workspace ledger before ending the session.

Update:

- `workspace.yaml` `links.githubPrs`
- `workspace.yaml` `state` when appropriate
- `notes.md` with a short chronological entry

State guidance:

- Set `pr-review` when a PR is open and the next action is reviewer, CI, or external review.
- Set `review-feedback` when review comments or requested changes require local work.
- Set `dev-complete` when the PR is accepted or merged and no implementation work remains.

Use `sync-github` for explicit PR discovery when `gh` is available.

Use `pr <id> opened`, `pr <id> feedback`, and `pr <id> merged` to record lifecycle transitions when the user or agent observes them directly. These commands set `pr-review`, `review-feedback`, and `dev-complete` respectively and append a note.

### PR Feedback Intake

PR feedback intake is an LLM workflow, not a special helper command yet. Use it when the user asks to look at PR comments, requested changes, review feedback, or CI/reviewer follow-up for a workspace.

First orient from the workspace ledger:

- run `status <workspace-id>`
- run `sync-github <workspace-id>` when PR snapshots may be stale and `gh` is available
- identify the repo, task branch, and PR URL/number from `workspace.yaml`

Then inspect PR feedback with the available GitHub tools or `gh`. Prefer unresolved review threads and requested-changes reviews over stale resolved comments. Also check top-level PR comments when they contain actionable follow-up.

Classify every actionable item into one of three outcomes:

- `direct-fix`: small, obvious, single-context fix that can be made on the workspace task branch.
- `workspace-issue`: non-trivial, multi-file, dependency-bearing, or independently reviewable follow-up that should become a local issue.
- `HITL`: product decision, reviewer negotiation, design judgment, credential/environment access, or ambiguous feedback that needs the user.

For `direct-fix`, implement the fix on the workspace task branch, run relevant checks, append a note, and leave the workspace in `review-feedback` until the PR is updated.

For `workspace-issue`, create local issues with `issue create`. Use `AFK` for feedback an agent can handle independently and `HITL` for feedback needing the user. Preserve links or short references back to the original PR comment in the issue body. After issue creation, Sandcastle can run the ready AFK subset again.

For `HITL`, do not guess. Create or update a HITL issue, set or keep the workspace state as `review-feedback`, and ask the user for the missing decision.

After intake, always append a note with:

- PR reviewed
- number of actionable items
- direct fixes planned or made
- local issues created
- HITL decisions needed

Set state guidance:

- `review-feedback`: actionable PR feedback exists or follow-up issues were created.
- `pr-review`: fixes have been pushed and the next action is reviewer/CI.
- `dev-complete`: PR is accepted or merged and no implementation work remains.

### Jira Actions Rule

Whenever you create, read, or update a Jira issue as part of workspace work, update the workspace ledger before ending the session.

Update:

- `workspace.yaml` `links.jira`
- `notes.md` with what changed or what was observed

Do not continuously mirror Jira. Store useful links/status/snapshots only.

### Close

Closing marks the ledger state as `closed` and appends a note. It must not delete worktrees.

## State

State is explicit in `workspace.yaml`. Common states:

```text
captured
needs-clarification
scoped
in-progress
user-review
pr-review
review-feedback
dev-complete
blocked
closed
```

State meanings:

- `captured`: workspace exists and raw user input is recorded; no grooming implied.
- `needs-clarification`: creation or scoping found a critical missing input or contradiction.
- `scoped`: task is clear enough to implement without re-asking basics.
- `in-progress`: implementation work has started.
- `user-review`: local work is ready for the user to inspect before publishing or PR review.
- `pr-review`: PR is open and waiting on reviewers, CI, or external review.
- `review-feedback`: PR has actionable reviewer comments or requested changes.
- `dev-complete`: implementation is accepted or merged, but workspace is not closed.
- `blocked`: work cannot continue without external input or state change.
- `closed`: workspace is intentionally archived.

Transitions are loose. Preserve unfamiliar state strings unless the user asks to normalize them.
