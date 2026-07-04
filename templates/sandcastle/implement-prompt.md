# Task

Implement issue `{{ISSUE_ID}}`: {{ISSUE_TITLE}}

Work only on branch `{{BRANCH}}`.

## Workspace

- Workspace: `{{WORKSPACE_ID}}`
- Workspace title: {{WORKSPACE_TITLE}}
- Workspace spec path: `{{WORKSPACE_SPEC_PATH}}`
- Issue path: `{{ISSUE_PATH}}`

Read the workspace spec and issue file before editing.

If this issue depends on other repos, their read-only worktrees are available under `related-repos/<repo-name>/`.

## Issue Body

{{ISSUE_BODY}}

## Review Feedback

{{REVIEW_FEEDBACK}}

If review feedback is present, focus on addressing it without widening scope.

## Expectations

- Make the smallest coherent change that satisfies the issue.
- Add or update tests when behavior changes.
- Run relevant validation commands.
- Commit your work on this branch.

When the issue is implemented, output:

```text
<promise>COMPLETE</promise>
```

