# Add Jira fetch-on-create and explicit sync

## Parent

[Workspace Skill V1 PRD](../prd/workspace-skill-v1.md)

## What to build

Add Jira integration to the workspace helper so a workspace created from a Jira key can import useful Jira context into the local ledger and `spec.md`, and so an explicit sync command can refresh last-seen Jira status later.

This should keep the local ledger as the source of truth. Jira is an external source/snapshot, not a continuous mirror.

## Acceptance criteria

- [ ] Config supports the Jira base URL and credential source needed by the user's local environment.
- [ ] Creating a workspace whose ID looks like a Jira key attempts a Jira fetch when Jira config is present.
- [ ] Jira title/body/status/link are stored in the ledger and/or inserted into the `## Input` section of `spec.md`.
- [ ] If Jira config is missing or fetch fails, workspace creation still succeeds and records a clear note.
- [ ] A `sync-jira <workspace-id>` command refreshes last-seen Jira status and appends a concise `notes.md` entry.
- [ ] The skill instructions describe that Jira sync is explicit and lightweight.

## Blocked by

None - can start immediately.
