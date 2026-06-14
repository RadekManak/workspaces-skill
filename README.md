# Workspaces

Local workspace tracking for AI-assisted development.

This repo contains the Codex skill, templates, and helper scripts. Runtime workspace data lives outside this repo under `~/workspaces`.

## Layout

```text
~/git/workspaces
  Skill/tooling repo.

~/workspaces/config.yaml
  Runtime configuration.

~/workspaces/_ledger/workspaces/<workspace-id>
  Workspace metadata, spec, notes, and run outputs.

~/workspaces/<workspace-id>/<repo>
  Actual git worktrees.
```

## First Run

```bash
/home/rmanak/git/workspaces/scripts/workspace.py init-config
```

Review `~/workspaces/config.yaml` after creation.

## Common Commands

Create a ledger-only workspace:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py create BILL-456 --title "Add invoice export"
```

Create a workspace with repo worktrees:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py create BILL-456 --repo admin-web --repo billing-api
```

Adopt an existing worktree:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py adopt /path/to/worktree --id BILL-456
```

Check local status:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py status BILL-456
```

Append a note:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py note BILL-456 "Opened frontend PR."
```

Sync GitHub PR snapshots explicitly:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py sync-github BILL-456
```

Close a workspace:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py close BILL-456
```

## Principles

- The ledger is canonical.
- Runtime metadata is plain files.
- `spec.md` is mutable.
- `notes.md` is append-only-ish.
- Normal status uses local files and local git only.
- Jira and GitHub sync are explicit.
- Closing a workspace does not delete worktrees.

## Remaining Work

Follow-up implementation issues are in `docs/issues/`.
