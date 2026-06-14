# Workspace Skill V1 PRD

## Problem Statement

The user already has a useful workspace workflow: they keep source repositories checked out locally, then create task-specific worktrees on a branch named after the work item. This makes code work easy to start and clean up, especially across multiple repositories.

The missing part is durable workflow memory. Once work starts, there is no lightweight local system that records the workspace metadata, current spec, Jira links, GitHub PR links, notes, and agent run results. This makes it harder for a later agent session to understand what exists, what was planned, what changed manually, whether PRs exist, and whether the workspace is ready for review, dev complete, or closed.

The user does not want a second project management product such as Linear in V1. Jira remains the company-visible tracker, GitHub remains the code review system, and the local workspace ledger becomes the agent-friendly source of truth for personal workflow state.

## Solution

Build a Codex skill centered on the user's existing workspace concept. A workspace represents the task, its metadata, specs, links, notes, and one or more local repo worktrees. The skill should extend the existing workspace workflow instead of introducing separate "task" or "repo job" concepts.

The runtime workspace data should live under the user's workspace storage area, with a plain-file ledger as the canonical metadata store. The actual code worktrees remain separate from the ledger. The skill/tooling repo contains the skill instructions, templates, and small helper scripts, but not the runtime workspace data.

The skill should support creating new workspaces, adopting existing worktrees created by other tools, grooming a mutable spec with an agent, running implementation or review agents against the workspace, reporting status from local files and local git state, explicitly syncing Jira/GitHub snapshots, and closing the workspace by marking the ledger closed.

The system should stay local-first. It should not require Git for the runtime ledger in V1, should not require Linear, should not require Sandcastle, and should not continuously mirror Jira or GitHub. Agents must update the ledger after they perform or observe GitHub/Jira actions during a session.

## User Stories

1. As a developer, I want to create a workspace from a Jira key, so that I can start company-tracked work with local metadata.
2. As a developer, I want to create a workspace from a local idea without Jira, so that I can plan before the company-visible ticket exists.
3. As a developer, I want workspace IDs to be human-supplied, so that the folder, branch, and task identity are easy to recognize.
4. As a developer, I want a Jira key to be used as the workspace ID when available, so that local work naturally maps to company work.
5. As a developer, I want workspace creation to optionally create repo worktrees, so that I can start coding immediately when I know the repos.
6. As a developer, I want workspace creation to also support ledger-only creation, so that imported or untriaged work does not create unnecessary code folders.
7. As a developer, I want created worktrees to use my existing local source repos as their base, so that the skill fits my current git workflow.
8. As a developer, I want worktrees to branch from the upstream main branch by default, so that workspace code starts from the expected integration base.
9. As a developer, I want the branch name to default to the workspace ID, so that branches are easy to associate with work.
10. As a developer, I want the system to support mixed branch names in one workspace, so that adopted real-world worktrees are not rejected.
11. As a developer, I want to adopt a worktree created by Cursor or another tool, so that the ledger can start tracking work even when the workspace was not created by the skill.
12. As a developer, I want adoption to infer repo name, remote, branch, base, PR, and local git status where possible, so that I do not manually enter obvious metadata.
13. As a developer, I want adoption to create or attach to a workspace, so that existing work can become trackable after the fact.
14. As a developer, I want the workspace ledger to be canonical, so that agents have one place to read and update workflow state.
15. As a developer, I want code workspaces to contain only a tiny pointer to their workspace identity, so that metadata does not drift between folders.
16. As a developer, I want a mutable spec file per workspace, so that I can groom the plan with agents without managing spec versions.
17. As a developer, I want the spec to include imported Jira input, so that the same document can become the implementable plan.
18. As a developer, I want a light spec template, so that agents have enough structure without turning planning into paperwork.
19. As a developer, I want a chronological notes log, so that human changes, agent findings, review comments, and important decisions are preserved.
20. As a developer, I want small agent run summaries in notes and large outputs in run files, so that the ledger stays readable.
21. As a developer, I want workspace state stored explicitly in metadata, so that commands can report and update state without parsing prose.
22. As a developer, I want loose state transitions, so that the workflow can evolve without a rigid state machine.
23. As a developer, I want status to read the ledger and inspect local git worktrees, so that I can see useful local progress without network calls.
24. As a developer, I want status to avoid silently mutating metadata, so that inspection does not unexpectedly change the ledger.
25. As a developer, I want explicit GitHub sync, so that PR snapshots are updated only when I ask or when an agent performs GitHub actions.
26. As a developer, I want agents to update the ledger after pushing branches or creating PRs, so that the workspace remains coherent after GitHub-visible work.
27. As a developer, I want the ledger to store PR links plus last-seen status, so that status remains useful without live GitHub queries.
28. As a developer, I want Jira fetched once during creation from a Jira key when credentials exist, so that the initial spec can include source context.
29. As a developer, I want ongoing Jira sync to be explicit, so that the local workflow does not pretend to continuously mirror Jira.
30. As a developer, I want the ledger to store Jira links plus last-seen status, so that I know what the agent last observed.
31. As a developer, I want PR creation to remain outside V1 automation, so that the workflow does not add unnecessary publishing machinery.
32. As a developer, I want manual code changes to require no extra ceremony, so that quick fixes during review stay quick.
33. As a developer, I want review feedback to be captured in notes when useful, so that important context is not lost.
34. As a developer, I want to close a workspace without deleting worktrees, so that cleanup remains a separate deliberate operation.
35. As an agent, I want clear instructions for where metadata lives, so that I can resume a workspace in a later session.
36. As an agent, I want helper scripts for repetitive git and ledger inspection, so that I avoid brittle ad hoc commands.
37. As an agent, I want to know that Jira and GitHub are integrations rather than the source of truth, so that I update the local ledger first.
38. As an agent, I want a required ledger update rule after GitHub and Jira actions, so that I do not leave the workspace state stale.
39. As a future maintainer, I want runtime data separate from skill code, so that the skill can be versioned without including workspaces or code repos.
40. As a future maintainer, I want the ledger to remain plain files in V1, so that sync/history mechanisms can be chosen later without changing the workflow model.

## Implementation Decisions

- The product is a Codex skill first, not a standalone application.
- Helper scripts are allowed where they remove friction from repeated filesystem, YAML, Git, GitHub, or Jira inspection.
- The central domain object is `Workspace`. It replaces separate first-class task and repo-job concepts in V1.
- A workspace groups metadata, specs, notes, links, and one or more repo worktrees.
- The runtime ledger is plain local files in V1. It is not a Git repo in V1.
- The skill/tooling repository is separate from runtime workspace data.
- The actual code worktrees are separate from the ledger and can be deleted without deleting the ledger.
- The ledger is canonical for workspace metadata.
- Code workspace folders may contain a small workspace identity pointer, but not duplicated metadata.
- Workspace IDs are human-supplied. Jira keys are preferred when available. Local slugs are acceptable before Jira exists.
- Workspaces may start with zero repos. This supports imported or untriaged work.
- Workspaces may also be created with one or more repos, creating local worktrees immediately.
- The default git model uses local source repositories, an upstream remote for the base branch, and a fork remote for later pushing.
- Worktree creation should not push branches.
- PR creation and branch publishing are outside V1 automation unless performed by the user or an agent as part of normal work.
- If an agent pushes, opens a PR, updates a PR, merges a PR, or observes that a PR was merged, it must update the ledger before ending the session.
- If an agent creates, reads, or updates Jira as part of workspace work, it must update the ledger before ending the session.
- GitHub and Jira synchronization are explicit operations in V1.
- Normal status should not perform network calls.
- Normal status should inspect local git worktrees for branch, uncommitted changes, ahead/behind information, and locally visible repo state.
- Status may suggest adoption of discovered repos but should not silently mutate the ledger.
- Adoption is a first-class workflow. It should support one repo path at a time, with optional recursive discovery later.
- Adopted workspaces may have mixed branch names. The system may warn but should not block.
- The workspace metadata file should remain small and machine-readable.
- The mutable spec file is the current working plan. There is no spec versioning in V1.
- The notes file is append-only-ish and chronological.
- Small run summaries belong in notes. Large or full run outputs belong in run artifacts.
- Workspace state is explicitly stored as metadata and is not derived from the spec.
- State transitions are loose in V1. Commands can warn on unfamiliar states but should not block them.
- Closing a workspace only marks it closed. It does not delete code worktrees.
- Linear is out of V1.
- Sandcastle is not foundational. A future runner can use Sandcastle, but the workspace model should not depend on it.
- Multi-repo work is handled naturally because a workspace can contain multiple worktrees.

## Testing Decisions

- Tests should exercise externally visible behavior: created files, updated metadata, appended notes, reported status, and git inspection output.
- Tests should not assert internal implementation details of helper scripts.
- The highest useful seam is the command/script interface used by agents.
- Workspace creation should be tested by running the create command in a temporary workspace root and checking the ledger and optional worktree records.
- Workspace adoption should be tested against a temporary git repository with known remotes and branch state.
- Workspace status should be tested against ledger fixtures plus temporary git repositories, without network access.
- GitHub sync should be tested behind a small adapter boundary so that `gh` output can be faked.
- Jira fetch/sync should be tested behind a small adapter boundary so that Jira API responses can be faked.
- Notes updates should be tested by checking that new entries append without rewriting existing entries.
- State updates should be tested by checking metadata changes and notes entries.
- Close behavior should be tested by verifying that metadata changes to closed while code worktrees remain untouched.
- The initial skill instructions should be reviewed manually with a dry-run prompt to ensure an agent can create, adopt, inspect, and update a workspace using only the documented workflow.

## Out of Scope

- Linear integration.
- Runtime ledger Git syncing or remote synchronization.
- A background GitHub crawler.
- Continuous Jira mirroring.
- A required Sandcastle dependency.
- First-class repo jobs.
- Strict workflow state machines.
- Automated PR publishing as a dedicated V1 command.
- Deleting worktrees as part of closing a workspace.
- Complex conflict handling for multiple machines editing the same ledger.
- Full database-backed task tracking.
- Spec versioning or multiple spec files per workspace.

## Further Notes

- The design intentionally extends the user's current workspace workflow instead of replacing it.
- The runtime ledger can later become a Git repo, Syncthing folder, database, or hosted system if multi-machine sync becomes important.
- The skill should encode the critical behavioral rules, especially ledger updates after GitHub/Jira actions.
- Scripts should stay small and boring. The agent-facing workflow is more important than building a large application in V1.
- The PRD was written directly into the local repository rather than published to an issue tracker because no issue tracker configuration was provided and the user explicitly requested a repo-local PRD.
