# PR feedback intake

Use this workflow when asked to inspect PR comments, requested changes, or CI/reviewer follow-up for a workspace.

## Orient and inspect

1. Run `status <workspace-id>`.
2. Run `sync-github <workspace-id>` when PR snapshots may be stale and `gh` is available.
3. Identify the repo, task branch, and PR URL/number from `workspace.yaml`.
4. Inspect feedback with available GitHub tools or `gh`. Prioritize unresolved review threads and requested-changes reviews, then actionable top-level comments.

## Classify and act

Classify every actionable item:

- `direct-fix`: small, obvious, single-context change. Implement it on the workspace task branch, run relevant checks, append a note, and keep `review-feedback` until the PR is updated.
- `workspace-issue`: non-trivial, multi-file, dependency-bearing, or independently reviewable follow-up. Create a local issue, preserving a link or short reference to the PR comment, then use `issue ready` to find executable work.
- `HITL`: product decision, reviewer negotiation, design judgment, credential/environment access, or ambiguous feedback. Do not guess; create or update a local issue when useful, retain `review-feedback`, and ask the user.

After intake, append a note naming the PR and recording actionable-item count, direct fixes, local issues, and HITL decisions. Keep `review-feedback` while action remains, use `pr-review` after fixes are pushed and review/CI is next, and use `dev-complete` only after acceptance or merge with no remaining implementation work.
