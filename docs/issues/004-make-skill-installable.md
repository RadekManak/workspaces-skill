# Make the workspace skill installable and discoverable

## Parent

[Workspace Skill V1 PRD](../prd/workspace-skill-v1.md)

## What to build

Make this repository usable as an actual Codex skill from the user's normal skill discovery path, without mixing runtime workspace data into the skill repo.

The result should let future Codex sessions trigger `$workspaces` naturally and use the helper scripts in this repo.

## Acceptance criteria

- [ ] Decide whether this repo should be symlinked, copied, or installed into the active Codex skills directory.
- [ ] Add the minimal installation mechanism needed for the chosen approach.
- [ ] Keep runtime data under `~/workspaces`, not in the skill repo.
- [ ] Verify the skill validates after installation.
- [ ] Verify the installed skill metadata exposes the `workspaces` skill name and default prompt.
- [ ] Record the install decision in the skill or issue notes without adding broad documentation clutter.

## Blocked by

None - can start immediately.
