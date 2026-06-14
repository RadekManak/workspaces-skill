# Add workflow self-checks for helper scripts

## Parent

[Workspace Skill V1 PRD](../prd/workspace-skill-v1.md)

## What to build

Add a small runnable self-check suite for the workspace helper. The checks should validate externally visible behavior using temporary folders and temporary git repositories.

These checks should be easy for an agent to run before changing the skill.

## Acceptance criteria

- [ ] A single command runs all self-checks locally.
- [ ] Checks cover config initialization without touching the real `~/workspaces` config.
- [ ] Checks cover ledger-only workspace creation.
- [ ] Checks cover workspace creation with a local git worktree.
- [ ] Checks cover adoption of an existing worktree.
- [ ] Checks cover status output from the ledger and local git state.
- [ ] Checks cover appending notes without rewriting existing notes.
- [ ] Checks cover closing a workspace without deleting worktrees.
- [ ] The checks use temporary paths and leave no runtime data behind.

## Blocked by

None - can start immediately.
