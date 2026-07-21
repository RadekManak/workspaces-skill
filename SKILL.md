---
name: workspaces
description: Manage local AI-assisted development workspaces with a plain-file ledger, mutable specs, append-only notes, local git worktrees, explicit GitHub/Jira link tracking, and adoption of existing worktrees. Use when Codex needs to create, adopt, inspect, plan, update, review, sync, or close a development workspace across one or more repos.
---

# Workspaces

Keep the local workspace ledger current while doing development work. Use manual or subagent-driven development by default. For explicit Sandcastle, AFK, or runner-based execution, load `/workspaces-with-sandcastle` directly.

## Model and layout

The ledger is canonical; generated code-workspace files and `.workspace-id` pointers must not duplicate durable metadata.

```text
~/workspaces/config.yaml
~/workspaces/_ledger/
  workspaces/<workspace-id>/
    workspace.yaml  # metadata, links, state, repos
    spec.md         # mutable current spec
    issues/         # local implementation issues
    notes.md        # chronological, append-at-bottom notes
    runs/           # large agent outputs
  projects/<repository-identity>/  # private CONTEXT.md and ADRs
  wayfinders/<map-id>/             # private decision maps
~/workspaces/<workspace-id>/
  <repo>/
  <workspace-id>.code-workspace    # generated from workspace.yaml
```

Private project knowledge belongs to one canonical repository identity. Multi-repository domain models are out of scope. For wayfinder or prototype work, read [references/wayfinders.md](references/wayfinders.md).

## Configuration and commands

Ensure configuration exists:

```bash
scripts/workspace.py init-config
```

Use `scripts/workspace.py config` for resolved configuration, or add `--show-config` to `status` when also orienting in a workspace. Do not copy defaults from `templates/config.yaml` into instructions.

Discover commands from the CLI instead of relying on a static inventory:

```bash
scripts/workspace.py --help
scripts/workspace.py <command> --help
```

Use the helper for deterministic file and git operations. If it lacks a behavior, inspect and edit the ledger directly rather than inventing another tracker.

## Interoperability

Within a managed workspace, these rules override conflicting publication or discovery behavior in cooperating skills:

- **`to-spec`:** update canonical `spec.md`, then set state to `scoped`; do not create an issue.
- **`to-tickets`:** create local implementation issues under `issues/`, preserving blockers; do not publish them elsewhere.
- **`implement`:** use the selected local issue as the execution contract and `spec.md` as broader context. A single-slice change without issues may use `spec.md` directly.
- **`code-review`:** use `spec.md` and local issues represented by the diff as Spec sources. For a task-branch review, use the full spec and all included issues. Private glossary entries and ADRs may inform Spec review; only repository-owned documentation defines hard Standards findings.
- **`wayfinder`:** keep maps private, inspect source repos without modifying worktrees by default, and require explicit approval before external publication. Read [references/wayfinders.md](references/wayfinders.md) for prototype and promotion rules.
- **`grilling`:** change no workspace artifacts before shared understanding is confirmed; capture the result through `to-spec`.
- **`grill-with-docs`:** update existing repository `CONTEXT.md` or ADR directories. If absent, write under `ledger_root/projects/<repository-identity>/`; creating missing repository artifacts requires explicit approval.

## Workflow

### Orient

Use `list` after time away and `status [id]` for one workspace. Both inspect local ledger and git state only; do not make Jira or GitHub calls during normal status. `list` defaults to active workspaces; use its help for state, archive, and structured-output filters.

Use `doctor <id>` when a workspace seems inconsistent. `--fix` may repair only derived/scaffold files: ledger directories/files, `.workspace-id`, and `.code-workspace`. It must not discard code changes or rewrite branches.

### Create and adopt

Create captures the user's raw request and defaults to `captured`; pass `--input` to preserve the actual instruction. With `--repo`, it fetches the configured base remote/branch and creates a linked worktree on a workspace-named branch. Do not push, explore the project, or groom the task during ordinary creation. Verify only config, source repo, branch/base availability, and resulting local status.

Use `adopt` for existing Cursor/manual worktrees. Mixed branch names are allowed; note a warning when they matter, but do not block adoption.

Creation and adoption generate a VS Code workspace containing the ledger and all recorded repos. Treat it as generated output; update the ledger through helpers instead of editing it.

### Switch session roots

Topology-changing commands (`create`, `adopt`, `repo add`, `repo adopt`, `repo remove`, `doctor --fix`) print the current `.code-workspace` JSON as the final stdout block. To switch the agent session:

1. Read `folders[].path` from that JSON.
2. Resolve each path against the directory containing the `.code-workspace` file.
3. Use the resolved directories as session roots, never the `.code-workspace` path.

Ignore a switching error if the correct roots became active. After `create`, switch automatically only when the agent is not already in a workspace; otherwise ask. After every other topology-changing command, switch immediately.

`open <id>` is a human convenience for launching the configured editor, not an agent root-switching mechanism. Use `open <id> --print` only to print the workspace JSON.

### Plan and implement

Write or groom `spec.md` only when asked to scope, plan, or start work. Put Jira input under `## Input` when available. Set `scoped` once an agent can implement without re-asking basics; set `in-progress` when implementation starts.

Local `issues/*.md` files are implementation contracts, not GitHub issues. Create them through `issue create`; keep id, title, status, blockers, and branch in YAML frontmatter, with acceptance criteria and body in Markdown. Use `issue ready` for dependency-ready work and `--json` for scripts. Mark completed issue branches `merged`; cleanup remains user-triggered.

Base commands may encounter Sandcastle-enriched workspace or issue metadata. Preserve it unchanged without treating it as drift.

### Manage repos

Use `repo add` for a linked worktree from a configured source repo, `repo adopt` for an existing checkout/worktree, and `repo refresh` after branch or remote changes. `repo remove --delete-worktree` requires explicit user intent and refuses dirty worktrees, non-linked checkouts, and paths outside the workspace root.

### Cleanup

Cleanup is HITL. Always run `cleanup-plan` first and show the result. Run cleanup only for workspace IDs the user explicitly approves.

`cleanup-plan` refreshes recorded `githubPrs` via `gh pr view` before treating snapshots as open, and persists updated state. Soft-fails (keeps the snapshot) when `gh` is missing or a view fails.

For bulk review, use `cleanup-plan --all --write`; it stores an auditable JSON plan under `ledger_root/runs/`. Then use `cleanup --from-plan` for the approved IDs. Cleanup revalidates the plan and refuses stale entries.

Normal workspace cleanup removes clean linked worktrees and the generated workspace root, retains the ledger, marks the workspace closed, and records `cleanupStatus: done`. It refuses active states, dirty repos, non-linked checkouts, outside-root worktrees, unexpected root files, open PR snapshots, and active Sandcastle locks.

Use `--force` only after explicit approval when `cleanup-plan` reports `force-eligible`. It may override dirty repos, extra files, open PR snapshots, or a non-done state; it retains task branches, records `cleanupAction: force-removed`, and notes every override. It cannot combine with `--from-plan` or `--item`.

Issue-level cleanup is exceptional. Plan it with `cleanup-plan <id> --issues`, then run `cleanup --item <id>:<issue-id>` only for approved items. It accepts only:

- `safe-to-delete`: branch is merged into the task branch and any child worktree is clean.
- `mark-done`: branch and child worktree are already absent.

Never delete the workspace task branch or clean failed, needs-fix, blocked-HITL, unmerged, or dirty issue work.

### GitHub and PR feedback

Whenever a branch is pushed or a PR is created, updated, merged, or observed merged, update before ending the session:

- `workspace.yaml` links and state as appropriate.
- `notes.md` with a short chronological entry.

Use `sync-github` only for explicit PR discovery. Use `pr <id> opened|feedback|merged` when lifecycle changes are observed directly; these select `pr-review`, `review-feedback`, and `dev-complete` and append a note.

When asked to inspect PR comments, requested changes, or reviewer/CI follow-up, read [references/pr-feedback.md](references/pr-feedback.md) and follow its intake workflow.

Whenever Jira is read or changed during workspace work, update `workspace.yaml` `links.jira` and append a useful note. Store links/status snapshots; do not continuously mirror Jira.

### Close

Closing sets state to `closed` and appends a note. It does not delete worktrees.

## State

State is explicit in `workspace.yaml`:

- `captured`: raw request recorded; not groomed.
- `needs-clarification`: critical input is missing or contradictory.
- `scoped`: ready to implement without re-asking basics.
- `in-progress`: implementation started.
- `user-review`: implementation awaits user inspection, not branch approval.
- `pr-review`: PR awaits reviewers, CI, or external review.
- `review-feedback`: actionable PR feedback requires work.
- `dev-complete`: implementation accepted or merged, but workspace remains open.
- `blocked`: progress requires external input or state change.
- `closed`: intentionally archived.

Transitions are loose. Preserve unfamiliar state strings unless the user asks to normalize them.
