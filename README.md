# Workspaces

Local workspace tracking for AI-assisted development.

**Status: early alpha.** The API, CLI, ledger layout, and skill instructions may change without notice. Expect rough edges.

**`workspaces-with-sandcastle` is untested** in real workflows. Treat Sandcastle / AFK / runner automation as experimental; prefer the base `/workspaces` skill for normal use.

This repo contains the Codex skill, templates, and helper scripts. Runtime workspace data lives outside this repo under `~/workspaces`. Licensed under the [Apache License 2.0](LICENSE).

## Layout

```text
<skill-dir>
  Skill/tooling repo.

~/workspaces/config.yaml
  Runtime configuration.

~/workspaces/_ledger/workspaces/<workspace-id>
  Workspace metadata, spec, notes, and run outputs.

~/workspaces/_ledger/projects/<repository-identity>
  Private glossary and ADRs shared across workspaces for one repository.

~/workspaces/_ledger/wayfinders/<map-id>
  Private decision maps and tickets that may outlive a workspace.

~/workspaces/<workspace-id>/<repo>
  Actual git worktrees.

~/workspaces/<workspace-id>/<workspace-id>.code-workspace
  VS Code workspace file maintained from the ledger.
```

## First Run

Run these commands from the skill/tooling directory. The paths are relative so the same tree works from a source checkout or an installed skill copy.

Install or update the skill:

```bash
make install
```

```bash
scripts/workspace.py init-config
```

Review `~/workspaces/config.yaml` after creation.

## Common Commands

Create a ledger-only workspace:

```bash
scripts/workspace.py create BILL-456 --title "Add invoice export" --input "Add invoice export."
```

Create a workspace with repo worktrees:

```bash
scripts/workspace.py create BILL-456 --repo admin-web --repo billing-api
```

Adopt an existing worktree:

```bash
scripts/workspace.py adopt /path/to/worktree --id BILL-456
```

List active workspaces:

```bash
scripts/workspace.py list
scripts/workspace.py list --state user-review
scripts/workspace.py list --all
```

Check local status:

```bash
scripts/workspace.py status BILL-456
scripts/workspace.py status BILL-456 --show-config
```

Open the generated VS Code workspace:

```bash
scripts/workspace.py open BILL-456
```

Check and repair derived workspace files:

```bash
scripts/workspace.py doctor BILL-456
scripts/workspace.py doctor BILL-456 --fix
```

Append a note:

```bash
scripts/workspace.py note BILL-456 "Opened frontend PR."
```

Set state:

```bash
scripts/workspace.py state BILL-456 scoped
```

Manage repos in an existing workspace:

```bash
scripts/workspace.py repo list BILL-456
scripts/workspace.py repo add BILL-456 billing-api
scripts/workspace.py repo adopt BILL-456 /path/to/worktree --name admin-web
scripts/workspace.py repo refresh BILL-456
scripts/workspace.py repo remove BILL-456 billing-api
```

Create local implementation issues:

```bash
scripts/workspace.py issue create BILL-456 1 --title "Add invoice export model" --acceptance "Export includes paid invoices"
scripts/workspace.py issue create BILL-456 2 --title "Add export button" --blocked-by 1
```

Inspect local issues and dependency-ready work:

```bash
scripts/workspace.py issue list BILL-456
scripts/workspace.py issue ready BILL-456
scripts/workspace.py issue ready BILL-456 --json
scripts/workspace.py issue set-status BILL-456 1 merged
```

Plan cleanup for completed workspaces:

```bash
scripts/workspace.py cleanup-plan BILL-456
scripts/workspace.py cleanup-plan BILL-456 --json
scripts/workspace.py cleanup-plan --all --write
```

After reviewing the plan, clean up explicitly approved workspaces. For bulk cleanup, use the written plan so the approval is auditable:

```bash
scripts/workspace.py cleanup BILL-456
scripts/workspace.py cleanup --from-plan ~/workspaces/_ledger/runs/cleanup-plan-20260618-120000-000000.json BILL-456
```

Task-branch cleanup is available when needed, but it is not the normal workspace cleanup path:

```bash
scripts/workspace.py cleanup-plan BILL-456 --issues
scripts/workspace.py cleanup --item BILL-456:1
```

Sync GitHub PR snapshots explicitly:

```bash
scripts/workspace.py sync-github BILL-456
```

Record PR lifecycle state:

```bash
scripts/workspace.py pr BILL-456 opened --url https://github.com/org/repo/pull/123
scripts/workspace.py pr BILL-456 feedback
scripts/workspace.py pr BILL-456 merged
```

Intake PR feedback through an agent session:

```text
Look at PR feedback for workspace BILL-456. Classify it into direct fixes, local workspace issues, or HITL questions.
```

Close a workspace:

```bash
scripts/workspace.py close BILL-456
```

Run self-checks:

```bash
scripts/self_check.py
```

## Principles

- The ledger is canonical.
- The default workflow is manual or subagent-driven development through `workspace.py`.
- For explicit Sandcastle, AFK, or runner-based execution, use `/workspaces-with-sandcastle` and `workspace_with_sandcastle.py`.
- Creation captures the request and defaults to `captured`; it does not imply planning is complete.
- Runtime metadata is plain files.
- `spec.md` is mutable.
- Local issues live in `issues/*.md` with YAML frontmatter.
- `<workspace-id>.code-workspace` is generated from `workspace.yaml`.
- `notes.md` is append-only-ish.
- Normal list/status uses local files and local git only.
- Jira and GitHub sync are explicit.
- Closing a workspace does not delete worktrees.

## Skill Interoperability

Within a managed workspace, the workspace ledger overrides generic publication and discovery behavior from cooperating skills:

- `to-spec` updates `spec.md` and sets the workspace to `scoped`; it does not create an issue.
- `to-tickets` creates local implementation issues under `issues/`, including blocker metadata; it does not publish elsewhere.
- `implement` works from a selected local ticket with `spec.md` as context, or directly from `spec.md` for a single-slice change with no tickets.
- `code-review` uses `spec.md` and the local tickets represented by the diff as its Spec sources. Private project docs inform Spec review, not hard Standards findings.
- `wayfinder` keeps private maps under `_ledger/wayfinders/`, reads source worktrees without modifying them by default, and uses linked prototype workspaces for experiments. External publication requires explicit approval.
- `grilling` waits for confirmed shared understanding before changing workspace artifacts; `to-spec` captures the result.
- `grill-with-docs` uses an existing repository `CONTEXT.md` or ADR directory. Missing domain-doc artifacts default to `_ledger/projects/<repository-identity>/`; creating them in the repository requires explicit approval.

Private project knowledge currently covers one repository identity. Multi-repository domain models and first-class helper commands for wayfinders or prototype workspaces are intentionally deferred until real usage exposes the needed operations.
