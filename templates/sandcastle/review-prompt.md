# Task

Review issue `{{ISSUE_ID}}`: {{ISSUE_TITLE}}

Review branch `{{BRANCH}}` against the workspace spec and issue acceptance criteria.

## Workspace

- Workspace: `{{WORKSPACE_ID}}`
- Workspace title: {{WORKSPACE_TITLE}}
- Workspace spec path: `{{WORKSPACE_SPEC_PATH}}`
- Issue path: `{{ISSUE_PATH}}`

If this issue depends on other repos, their read-only worktrees are available under `related-repos/<repo-name>/`.

## Issue Body

{{ISSUE_BODY}}

## Instructions

Do not edit files. Review only.

Check:

- Does the implementation satisfy this issue?
- Does it stay inside the intended scope?
- Are relevant tests or validations present?
- Are there obvious regressions, unsafe assumptions, or missing edge cases?
- Is anything blocked on human judgment?

Output exactly one `<review>` block containing JSON:

```text
<review>
{"status":"approved","feedback":"Short explanation."}
</review>
```

Valid `status` values:

- `approved`: the branch can be merged.
- `needs-fix`: the implementer should address the feedback and rerun review.
- `blocked-hitl`: a human decision or action is needed before the issue can continue.

After the review block, output:

```text
<promise>COMPLETE</promise>
```

