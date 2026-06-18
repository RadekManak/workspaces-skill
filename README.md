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

~/workspaces/<workspace-id>/<workspace-id>.code-workspace
  VS Code workspace file maintained from the ledger.
```

## First Run

```bash
/home/rmanak/git/workspaces/scripts/workspace.py init-config
```

Review `~/workspaces/config.yaml` after creation.

## Common Commands

Create a ledger-only workspace:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py create BILL-456 --title "Add invoice export" --input "Add invoice export."
```

Create a workspace with repo worktrees:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py create BILL-456 --repo admin-web --repo billing-api
```

Adopt an existing worktree:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py adopt /path/to/worktree --id BILL-456
```

List active workspaces:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py list
/home/rmanak/git/workspaces/scripts/workspace.py list --state user-review
/home/rmanak/git/workspaces/scripts/workspace.py list --all
```

Check local status:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py status BILL-456
```

Open the generated VS Code workspace:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py open BILL-456
```

Check and repair derived workspace files:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py doctor BILL-456
/home/rmanak/git/workspaces/scripts/workspace.py doctor BILL-456 --fix
```

Append a note:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py note BILL-456 "Opened frontend PR."
```

Set state:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py state BILL-456 scoped
```

Manage repos in an existing workspace:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py repo list BILL-456
/home/rmanak/git/workspaces/scripts/workspace.py repo add BILL-456 billing-api
/home/rmanak/git/workspaces/scripts/workspace.py repo adopt BILL-456 /path/to/worktree --name admin-web
/home/rmanak/git/workspaces/scripts/workspace.py repo refresh BILL-456
/home/rmanak/git/workspaces/scripts/workspace.py repo remove BILL-456 billing-api
```

Create local implementation issues:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py issue create BILL-456 1 --title "Add invoice export model" --acceptance "Export includes paid invoices"
/home/rmanak/git/workspaces/scripts/workspace.py issue create BILL-456 2 --title "Add export button" --blocked-by 1
```

Inspect local issues and the dependency-ready AFK set:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py issue list BILL-456
/home/rmanak/git/workspaces/scripts/workspace.py issue ready BILL-456
/home/rmanak/git/workspaces/scripts/workspace.py issue ready BILL-456 --json
/home/rmanak/git/workspaces/scripts/workspace.py issue set-status BILL-456 1 merged
```

Prepare a Sandcastle run plan for the dependency-ready AFK issues:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py sandcastle BILL-456
/home/rmanak/git/workspaces/scripts/workspace.py sandcastle BILL-456 --json
```

Scaffold a repo-local Sandcastle runner:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py sandcastle BILL-456 --init-runner
```

Execute a repo-local Sandcastle runner with the plan path in the environment:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py sandcastle BILL-456 --execute --command "npx tsx .sandcastle/main.mts"
```

Reconcile a Sandcastle result file:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py sandcastle BILL-456 --reconcile-result ~/workspaces/_ledger/workspaces/BILL-456/runs/sandcastle-result-20260618-120000-000000.json
```

Plan cleanup for completed workspaces:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py cleanup-plan BILL-456
/home/rmanak/git/workspaces/scripts/workspace.py cleanup-plan BILL-456 --json
/home/rmanak/git/workspaces/scripts/workspace.py cleanup-plan --all --write
```

After reviewing the plan, clean up explicitly approved workspaces. For bulk cleanup, use the written plan so the approval is auditable:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py cleanup BILL-456
/home/rmanak/git/workspaces/scripts/workspace.py cleanup --from-plan ~/workspaces/_ledger/runs/cleanup-plan-20260618-120000-000000.json BILL-456
```

Task-branch cleanup is available when needed, but it is not the normal workspace cleanup path:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py cleanup-plan BILL-456 --issues
/home/rmanak/git/workspaces/scripts/workspace.py cleanup --item BILL-456:1
```

Sync GitHub PR snapshots explicitly:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py sync-github BILL-456
```

Record PR lifecycle state:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py pr BILL-456 opened --url https://github.com/org/repo/pull/123
/home/rmanak/git/workspaces/scripts/workspace.py pr BILL-456 feedback
/home/rmanak/git/workspaces/scripts/workspace.py pr BILL-456 merged
```

Intake PR feedback through an agent session:

```text
Look at PR feedback for workspace BILL-456. Classify it into direct fixes, local workspace issues, or HITL questions.
```

Close a workspace:

```bash
/home/rmanak/git/workspaces/scripts/workspace.py close BILL-456
```

Run self-checks:

```bash
/home/rmanak/git/workspaces/scripts/self_check.py
```

## Principles

- The ledger is canonical.
- Creation captures the request and defaults to `captured`; it does not imply planning is complete.
- Runtime metadata is plain files.
- `spec.md` is mutable.
- Local issues live in `issues/*.md` with YAML frontmatter.
- `<workspace-id>.code-workspace` is generated from `workspace.yaml`.
- `notes.md` is append-only-ish.
- Normal list/status uses local files and local git only.
- Jira and GitHub sync are explicit.
- Closing a workspace does not delete worktrees.

## Remaining Work

Follow-up implementation issues are in `docs/issues/`.
