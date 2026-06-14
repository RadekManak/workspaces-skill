---
name: workspaces
description: Manage local AI-assisted development workspaces with a plain-file ledger, mutable specs, append-only notes, local git worktrees, explicit GitHub/Jira link tracking, and adoption of existing worktrees. Use when Codex needs to create, adopt, inspect, plan, update, review, sync, or close a development workspace across one or more repos.
---

# Workspaces

Use this skill to keep a local workspace ledger up to date while doing development work.

## Layout

- Skill/tooling repo: `/home/rmanak/git/workspaces`
- Runtime config: `~/workspaces/config.yaml`
- Runtime ledger: `~/workspaces/_ledger/workspaces/<workspace-id>/`
- Code worktrees: `~/workspaces/<workspace-id>/<repo>/`

The ledger is canonical. Code workspaces may contain `.workspace-id`, but must not duplicate metadata.

## Files

Each workspace ledger contains:

```text
workspace.yaml  # machine-readable metadata, links, state, repos
spec.md         # mutable current spec/plan
notes.md        # append-only-ish chronological notes
runs/           # large/full agent run outputs
```

Use one mutable `spec.md`. Append notes at the bottom of `notes.md`.

## Configuration

Before using the skill, ensure config exists:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py init-config
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

Use the helper script for deterministic file and git operations:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py create <id> --title "<title>"
/home/rmanak/git/workspaces/scripts/workspace.py create <id> --repo <repo>
/home/rmanak/git/workspaces/scripts/workspace.py adopt <path> --id <id>
/home/rmanak/git/workspaces/scripts/workspace.py status [id]
/home/rmanak/git/workspaces/scripts/workspace.py note <id> "note text"
/home/rmanak/git/workspaces/scripts/workspace.py close <id>
/home/rmanak/git/workspaces/scripts/workspace.py sync-github <id>
```

If a command is missing a behavior, inspect and edit the ledger directly rather than inventing another tracker.

## Workflow

### Create

Create a ledger-only workspace when work is imported or not yet triaged.

Create with repos when the user is ready to code. Repos are created as git worktrees from `source_root/<repo>` into `workspace_root/<workspace-id>/<repo>`, on a branch named after the workspace ID. Do not push during create.

### Adopt

Adopt existing Cursor/manual worktrees with `adopt`. Mixed branch names are allowed. Warn in notes if mixed branches matter, but do not block.

### Plan

Write or groom `spec.md` with the user. Put Jira input in `## Input` when available. Keep the spec implementable and current.

### Status

Use `status` for local inspection. It reads the ledger and local git state only. Do not perform Jira/GitHub network calls during normal status.

### GitHub Actions Rule

Whenever you push a branch, create a PR, update a PR, merge a PR, or observe that a PR was merged, update the workspace ledger before ending the session.

Update:

- `workspace.yaml` `links.githubPrs`
- `workspace.yaml` `state` when appropriate
- `notes.md` with a short chronological entry

Use `sync-github` for explicit PR discovery when `gh` is available.

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
inbox
planning
ready
in-progress
review
dev-complete
closed
blocked
```

Transitions are loose. Preserve unfamiliar state strings unless the user asks to normalize them.
