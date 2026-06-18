# Add workflow self-checks for helper scripts

## Parent

[Workspace Skill V1 PRD](../prd/workspace-skill-v1.md)

## What to build

Add a small runnable self-check suite for the workspace helper. The checks should validate externally visible behavior using temporary folders and temporary git repositories.

These checks should be easy for an agent to run before changing the skill.

## Acceptance criteria

- [x] A single command runs all self-checks locally.
- [x] Checks cover config initialization without touching the real `~/workspaces` config.
- [x] Checks cover ledger-only workspace creation.
- [x] Checks cover workspace creation with a local git worktree.
- [x] Checks cover adoption of an existing worktree.
- [x] Checks cover status output from the ledger and local git state.
- [x] Checks cover appending notes without rewriting existing notes.
- [x] Checks cover closing a workspace without deleting worktrees.
- [x] The checks use temporary paths and leave no runtime data behind.
- [x] Checks cover local issue dependency readiness and user-review transition.
- [x] Checks cover Sandcastle plan generation, runner scaffold, result reconciliation, and failed execution handling.
- [x] Checks cover HITL cleanup planning and explicitly approved cleanup execution.
- [x] Checks cover whole-workspace cleanup planning and approved workspace removal.
- [x] Checks cover generated VS Code workspace files for create, adopt, and cleanup.
- [x] Checks cover written cleanup plans and stale-plan refusal.
- [x] Checks cover open path printing and doctor repair for generated workspace files.
- [x] Checks cover repo add, adopt, refresh, remove, and PR lifecycle helpers.
- [x] Checks cover Sandcastle execution lock refusal.

## Blocked by

None - can start immediately.
