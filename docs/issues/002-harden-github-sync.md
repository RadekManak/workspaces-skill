# Harden explicit GitHub PR sync

## Parent

[Workspace Skill V1 PRD](../prd/workspace-skill-v1.md)

## What to build

Improve the existing explicit GitHub sync so it reliably discovers PRs for workspace branches across the user's upstream/fork remote setup and records useful last-seen snapshots in the ledger.

The command should remain explicit. Normal workspace status must not make network calls.

## Acceptance criteria

- [ ] `sync-github <workspace-id>` handles branches pushed from the user's fork to PRs targeting the upstream repo.
- [ ] PR snapshots include repo, PR number, URL, branch, state, merged flag, title, and last-seen timestamp.
- [ ] Existing PR snapshots are updated instead of duplicated.
- [ ] Missing `gh`, missing auth, missing remotes, or missing pushed branches fail gracefully with notes that explain what was skipped.
- [ ] `status` displays stored PR snapshots without querying GitHub.
- [ ] The skill instructions remain clear that agents must update the ledger after GitHub-visible actions.

## Blocked by

None - can start immediately.
