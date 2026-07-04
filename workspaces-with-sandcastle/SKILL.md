---
name: workspaces-with-sandcastle
description: Extend an existing /workspaces-managed workspace with Sandcastle, AFK batch execution, and runner-based automation. Use only when the user explicitly asks for Sandcastle, AFK batch execution, or runner-based execution — never merely because sandcastle metadata already exists on a workspace or issue.
---

# Workspaces with Sandcastle

This skill extends `/workspaces`; it does not replace it.

**Read `/workspaces` first** if you have not already read it in this session — use the **split-aware** base skill from this same repo (root `SKILL.md`), not an older monolithic copy that still documents `workspace.py sandcastle <id>` as a single verb. Inherit its ledger layout, workspace lifecycle, issue vocabulary, session-root switching rules, and manual/subagent workflow. Do not duplicate or contradict those rules here.

## Activation

Load this skill **only** on an explicit user request for Sandcastle, AFK batch execution, or runner-based execution.

The mere presence of a `sandcastle:` block on an issue, or a `sandcastle:` section in `workspace.yaml`, must **not** auto-activate this skill. When the user is doing ordinary manual or subagent work, stay on `/workspaces`. Per-issue `sandcastle:` metadata is preserved; however, ordinary repo or issue mutations through `/workspaces` (for example `repo add`/`repo remove`, or editing an issue that is part of the current plan's targeted set) can eagerly clear the `sandcastle.currentPlan` pointer in `workspace.yaml` and append a Sandcastle invalidation note to `notes.md`. That is intentional safety behavior — not a reason to load this skill.

## Relationship to `/workspaces`

This skill is an **upgrade path** for an existing `/workspaces` workspace, not a separate workspace type.

- **Mid-session upgrade:** You can load this skill against an already-active `/workspaces` workspace and reuse the same ledger, spec, issues, and worktrees.
- **No workspace yet:** Bootstrap through `/workspaces` first (`create`, `adopt`, spec grooming, issue splitting) before using Sandcastle commands.
- **Not ready for execution:** If the spec is missing or issues are not yet execution-ready, hand control back to `/workspaces` for scoping, planning, and issue creation. This skill does not absorb those duties.

## Selective mode

Choosing Sandcastle does **not** convert the whole workspace.

Only the issues and commands targeted by a given `sandcastle plan` / `sandcastle execute` run use Sandcastle execution. Keep using the manual or subagent workflow from `/workspaces` for issues not targeted in that run.

After a Sandcastle run, return to plain `/workspaces` guidance for further manual work in the same workspace.

## Layout

Sandcastle shares the same runtime layout as `/workspaces`:

- Skill/tooling repo: this skill directory is nested under the base `/workspaces` skill repo root (the directory that contains root `SKILL.md` and `scripts/`). There is no `scripts/` subdirectory here — Sandcastle scripts live at `../scripts/` relative to this file.
- Runtime config: `~/workspaces/config.yaml`
- Runtime ledger: `~/workspaces/_ledger/workspaces/<workspace-id>/`
- Code worktrees: `~/workspaces/<workspace-id>/<repo>/`
- Plan artifacts and run outputs: `~/workspaces/_ledger/workspaces/<workspace-id>/runs/`

Use `workspace_with_sandcastle.py` for Sandcastle commands. It is a full superset of `workspace.py` and shares the same underlying library. For non-Sandcastle ledger operations (excluding `issue *` commands, which gain Sandcastle-specific flags and output columns on the Sandcastle entrypoint), either entrypoint behaves the same; prefer `workspace.py` when Sandcastle is not in play.

## Issue schema

Base issue frontmatter stays generic (`id`, `title`, `status`, `blocked-by`, `branch`); acceptance criteria and body text live in the markdown body, not the frontmatter. See `/workspaces` for the base schema.

Sandcastle-specific metadata is added lazily to issues actually targeted for a Sandcastle run, not eagerly to the whole workspace. Execution `type` and `reviewStatus` live in a nested `sandcastle:` block. Result-reconciliation fields written during `execute` (for example `lastError`, `blockedByFailure`) are top-level issue frontmatter keys, not nested under `sandcastle:`.

Cross-repo dependencies use the same top-level `blocked-by` field as same-repo dependencies.

`workspace.yaml` may also carry a `sandcastle:` section (for example, a current-plan pointer). Base `/workspaces` commands preserve per-issue `sandcastle:` metadata without interpreting it; however, ordinary repo or issue mutations can eagerly clear the `sandcastle.currentPlan` pointer as described in Activation.

## Commands

Paths below are relative to this skill directory (`workspaces-with-sandcastle/`); scripts live at the repo root, one level up:

```bash
../scripts/workspace_with_sandcastle.py sandcastle plan <id> [--repo <repo>] [--limit N] [--json]
../scripts/workspace_with_sandcastle.py sandcastle init-runner <id> [--repo <repo>] [--force]
../scripts/workspace_with_sandcastle.py sandcastle execute <id> [--plan <path>] --command "<repo-local command>" [--json]
../scripts/workspace_with_sandcastle.py sandcastle reconcile-result <id> <result_path>
```

`workspace_with_sandcastle.py` also extends shared `issue` commands with Sandcastle-specific flags (`--type` AFK/HITL on create, `--review-status` on set-status). Use those only while explicitly in Sandcastle mode.

Inspect `--help` on any command for full flag detail.

## Workflow

### Prerequisites

Before planning or executing:

1. An active `/workspaces` workspace with the relevant repo worktrees.
2. A groomed `spec.md` and execution-ready local issues (dependency-ready AFK issues for automated runs).
3. A runner scaffold in each repo that will execute (`sandcastle init-runner`), unless the user already has one.

If any prerequisite is missing, defer to `/workspaces` instead of improvising here.

### Runner scaffold

`sandcastle init-runner` writes `.sandcastle/main.mts`, `implement-prompt.md`, and `review-prompt.md` into a target repo's worktree. Run it once per repo before the first execute.

The `--command` passed to `sandcastle execute` typically invokes this runner (for example, a package script that runs `main.mts`). The command runs with `cwd` set to that repo's worktree.

### Plan, then execute

Sandcastle execution is a required **two-step** flow:

1. **`sandcastle plan`** — compute and inspect the execution scope.
2. **`sandcastle execute`** — run against that plan.

Do not call `execute` without a current, valid plan.

#### `sandcastle plan`

Produces a dependency-ordered, multi-repo execution plan artifact under `runs/` and sets the workspace's current-plan pointer in `workspace.yaml`.

- Plans across **all repos** in the workspace by default. Use `--repo <repo>` to narrow to one repo.
- Use `--limit N` to cap the **seed** selection of targeted AFK issues. Dependency closure can add more issues to the locked execution scope (`targetedIssueIds` / execution waves) beyond `N`. Such closure-added issues may appear in a repo's `blocked` display bucket rather than `targeted` — check the JSON payload or `targetedIssueIds` for the true scope, not only the per-repo display buckets.
- Output groups issues per repo (targeted, ready-but-not-targeted, blocked with reasons) for inspection, and prints dependency-ordered **execution waves** (the actual execution driver, independent of repo boundaries).
- Appends a `notes.md` entry with repo(s), targeted issues, and the plan path.
- Use `--json` when another tool needs the plan payload directly.

Review the plan with the user when scope is non-obvious before executing.

#### `sandcastle execute`

Runs the previously computed plan **wave by wave**. Each wave is a dependency-ordered group of issues that may run in parallel across repos.

- Invokes the given `--command` once per repo that has ready issues in the current wave, in parallel within the wave, then waits for the entire wave to finish before starting the next wave.
- Reconciles each repo's result automatically after that repo's command exits.
- Resolves which plan to run: workspace current-plan pointer first, then the newest valid plan artifact, unless `--plan <path>` overrides for deterministic reruns or debugging.
- **Stale-plan guard:** refuses to run if workspace repos, the targeted issue set, `blocked-by` edges, or a targeted issue's branch or Sandcastle `type`/`reviewStatus` have changed since `plan` was run. Re-run `sandcastle plan` when stale.
- Clears the current-plan pointer when the run finishes (success or failure). Appends a `notes.md` summary when the run reaches a classified outcome; an unhandled mid-run exception still clears the pointer but may skip the summary note.
- Only **one execute run** may be active per workspace at a time (PID-liveness lock file at `runs/active-sandcastle-run.json`; a crashed holder's stale lock is detected and recovered automatically — recovery attempts are serialized via a separate `flock`-guarded marker file at `active-sandcastle-run.json.recovery-lock`).

If a dependency issue ends `failed` or `blocked-hitl`, transitive dependents are marked `blocked-hitl` rather than attempted.

### Cross-repo dependencies

Execution ordering follows `blocked-by` across repos, not a fixed per-repo sequence. Independent issues (no dependency edge between them) may run in parallel within a wave.

For issues that depend on work in another repo, the runner mounts read-only cross-repo dependency worktrees inside the sandbox at `related-repos/<repo-name>/`. Only repos an issue actually depends on are mounted — not every repo in the workspace. Because waves wait for dependency issues to complete and merge before dependents start, those mounts reflect already-merged code from blocker issues in other repos.

Express cross-repo dependencies with the same `blocked-by` field used for same-repo blockers.

### Reconcile result

`sandcastle reconcile-result` applies a Sandcastle result JSON file to workspace issues manually. `execute` normally reconciles automatically per repo per wave; use reconcile-result mainly for recovery or debugging when a result file exists but was not applied.

## State

Sandcastle runs set workspace state to `in-progress` during execution. Otherwise follow `/workspaces` state guidance for the workspace as a whole.

`user-review` still means "ready for the user to inspect," not "branch-level review already complete."

## Returning to manual work

When Sandcastle-targeted issues are done (or the user stops automated execution), continue with `/workspaces` for status inspection, PR bookkeeping, manual fixes, HITL issues, and issues not part of the Sandcastle run. Per-issue `sandcastle:` metadata is normal and is preserved. Further ordinary `/workspaces` repo or issue edits can still invalidate a live `sandcastle.currentPlan` pointer as described in Activation — that does not require staying in Sandcastle mode.
