# Hand off a completed workspace to Codex

Use this procedure only after a successful `create` or `adopt` and an explicit user request for a new task or continuation. Other workspace commands, cleanup, and retries do not trigger handoff. A handoff failure leaves the completed workspace operation intact.

The agent host owns `mcp__codex_app` calls. `scripts/codex_handoff.py` is a separate adapter for workspace input, desktop links, and the installed app-server. It cannot invoke host tools from Python. It accepts host observations and returns tool arguments where a host call is required. The core `scripts/workspace.py` remains unchanged.

## Prepare the input

1. Capture the completed workspace command's stdout and exit code. Keep the final JSON block unchanged. Do not run the topology command again to obtain another handoff attempt.
2. Use the absolute `.code-workspace` path recorded in `workspace.yaml`.
3. Pass `--handoff` to authorize launching a task. Add `--task "Implement X"` only for work the new task should perform. Keep the original request in the workspace's `spec.md` input; never replay workspace creation or task-launch instructions as task work.
4. Call the exposed desktop `list_projects` tool. Supply its complete `projects` array in `--projects-json`. Fetch all pages if the host paginates results.

Use private temporary files outside the ledger for captured stdout and host responses. Remove those files when finished. Do not write project IDs, task IDs, responses, or credentials into workspace metadata. The adapter reads the ledger but never writes it.

For example, after the topology command has finished, the common arguments are:

```bash
common=(
  --operation create
  --workspace-exit-code "$workspace_exit_code"
  --workspace-output "$temporary_directory/workspace.stdout"
  --workspace-file "/absolute/workspace/workspace.code-workspace"
  --handoff
)
```

Use `adopt` for an adoption. Omitting `--task` means orientation only. These arguments attest to a completed operation and an explicit request, not permission to rerun either operation.

The helper validates every recorded worktree and checks that the generated folders match all repositories plus the canonical ledger. Repository order comes from the ledger list, not directory enumeration or folder display order. The first recorded repository is the default primary. Use `--primary-repository NAME` to select another recorded repository, and keep that selection throughout the handoff. The adapter remains read-only and does not persist the override.

Normal handoff bootstraps or matches the primary repository root. It prefers an exact single-root project and also accepts primary-first projects containing only workspace folders. Equally preferred matches are rejected as ambiguous. The prompt includes every repository name, absolute path, recorded branch, ledger path, and spec path. It states that the workspace already exists and that the spec's original orchestration request is historical input.

Secondary roots are optional. Ask the user to attach them only after a concrete access failure, an explicit sidebar request, or a feature requirement for first-class project roots. Root attachment is not a guaranteed fix for filesystem permissions. The task must check access before editing and report the exact failed operation without creating a replacement checkout.

For strict full-root attachment, add `--require-all-roots`. The required order is the primary, remaining repositories in ledger order, then the ledger. A provider exposing only `path` proves only one attached root. If the complete ordered topology is not observed, follow the returned GUI instructions and obtain a fresh observation before starting a task. The adapter does not mutate desktop roots.

## Prefer the exposed task tool

When `list_projects` and `create_thread` are exposed, prepare a request:

```bash
python3 scripts/codex_handoff.py prepare "${common[@]}" \
  --projects-json "$temporary_directory/projects.json"
```

For `host-action-required`, call the returned `tool` once with its `arguments` from the agent host. The target uses the verified desktop `projectId` and a `local` environment, not a new Codex worktree. The title is the workspace title. No model or reasoning override is generated.

Save the tool response in a private temporary file. For a ready `threadId`, obtain a fresh `list_threads` observation and run:

```bash
python3 scripts/codex_handoff.py observe "${common[@]}" \
  --projects-json "$temporary_directory/projects.json" \
  --task-response-json "$temporary_directory/task-response.json" \
  --threads-json "$temporary_directory/threads.json"
```

If the listing has not indexed the ready ID, call the host's `read_thread` with that ID. Supply the returned record through `--thread-json`; a wrapper containing `thread` is also accepted. Host `wait_threads` can also establish liveness, but do not fabricate a thread record from its status. Use a direct read for the adapter's observation input.

`handoff-created` means creation returned a ready task ID. `thread_observed` records independent existence evidence. Only an observation containing the expected `projectId` sets `desktop_project_association_verified`. `submission_target_project_id` records where the create request was directed, not an independently observed association. Missing association never authorizes resubmission.

`handoff-pending` means creation returned only a `clientThreadId`. This is not a ready `threadId`; omit thread observations until a ready ID exists. Never pass a pending client ID to tools requiring a thread ID. Observe rather than resubmit.

Report UI topology separately from launch success. `project_scope`, `primary_root`, `attached_roots`, `all_roots_attached`, and `unattached_paths` describe the project observation. `workspace_paths_supplied` means the submitted prompt contained the full topology. `workspace_access` remains `unknown`: neither a ready ID nor an attached root proves filesystem access. In normal mode, missing secondary roots are informational and do not set `manual_action_required`.

If the host call fails after being sent, treat submission as unknown. Inspect the app before any explicit retry. Do not fall back to another submission mechanism after an uncertain create call.

## Bootstrap a missing desktop project

Use this unsupported path only when automatic handoff was explicitly requested. The local Linux desktop session must be known to the caller. Generic environment variables do not establish that capability.

```bash
python3 scripts/codex_handoff.py bootstrap "${common[@]}" \
  --projects-json "$temporary_directory/projects.json" \
  --enable-experimental --desktop-session
```

The helper negotiates the experimental app-server capability and calls `project/list` before dispatching a path-only `codex://threads/new` link. It does not submit a prompt or create a server-only project.

For `desktop-observation-required`, call the host's `list_projects` again and replace the projects snapshot with that fresh response. This pause is intentional: a subprocess cannot call the host observer. A successful desktop launch is not a project observation. If no project is visible, stop and inspect the app rather than repeat bootstrap indefinitely.

Once the project is observed, use the supported task tool if available. Otherwise use the automatic path below. If `list_projects` is unavailable, stop with `unavailable`; do not invent a project observation from a working directory or a server response.

## Submit through app-server

When the supported task tool is absent, use the freshly observed single-root desktop project:

```bash
python3 scripts/codex_handoff.py submit "${common[@]}" \
  --projects-json "$temporary_directory/projects.json" \
  --enable-experimental
```

The helper independently matches the exact repository root through app-server `project/list`. Desktop and server IDs stay separate. It starts a thread with the server ID, checks the returned assignment, submits the prompt, and waits for the matching `turn/completed` notification plus a nonempty `item/completed` assistant message. A successful acknowledgement is not completion. Assistant-result verification proves a response was observed, not that requested implementation work was correct.

Orientation-only tasks use a read-only sandbox. `--task` uses `workspace-write`; approvals are disabled so this path cannot pause for interaction. The app-server project and working directory use the primary repository even for multi-repository workspaces. Supplying other paths in the prompt does not grant write permission. Work needing additional access must stop rather than silently widen permissions.

`auto-submitted-desktop-association-unknown` means the turn completed with verified server assignment, but desktop placement is not observed. This is execution success, not proof that the task appears under the desktop project. `manual_action_required` is false because no composer submission is needed. If desktop-observable placement is mandatory, treat that requirement as unresolved.

To refine placement, pass the automatic result as `--task-response-json` to `observe`, with fresh projects and threads observations. It returns `auto-submitted` only if the thread carries the expected desktop project ID. A null ID leaves placement unknown. Do not resubmit to fix placement.

### Process and retry contract

- Linux only. Discovery checks the installed ChatGPT directories under `/usr/lib` and `/opt`. Supply absolute `--binary` and `--desktop-binary` paths for another installation. The helper never selects `codex` or an arbitrary GUI binary from `PATH`.
- Every app-server process negotiates `experimentalApi: true`. Unsupported methods and response shapes fail closed. Compatibility targets the installed experimental protocol, not a public desktop API.
- Processes use argv, never shell command strings. The default total app-server deadline is 120 seconds, bounded by `--timeout` to 1 through 600 seconds.
- Stdout uses JSONL framing with correlated request IDs and a one-MiB line limit. Malformed lines stop the operation. Stderr is drained and discarded to avoid retaining credentials.
- Authentication, protocol, or process errors before submission return `unavailable`. Once writing `turn/start` begins, an interruption returns `submission-unknown`. A failed or interrupted completed turn returns `unavailable` with its observed IDs and completion facts.
- There are no automatic retries. After a crash with no result, assume submission is unknown. Inspect the app, then obtain explicit approval before a new submission. Repeating `submit` can create another thread.
- The helper terminates its own app-server process group after success or failure. It never terminates the desktop app or unrelated processes. An interrupted turn may not continue after the helper exits.

## Use the manual composer only with permission

When automatic submission is unavailable before any submission attempt, or the user wants a reviewable draft, use a freshly verified desktop project:

```bash
python3 scripts/codex_handoff.py composer "${common[@]}" \
  --projects-json "$temporary_directory/projects.json" \
  --desktop-session --allow-manual
```

The URL-encoded link carries the desktop `projectId`, repository `path`, and full prompt. Direct ChatGPT dispatch is preferred. `--allow-protocol-opener` permits `xdg-open` only when no direct executable was found; the result marks that dispatch as less reliable. An invalid explicit desktop binary is terminal.

`dispatch-only` always means `task_created: false`, `project_created: false`, and `verification_required: true`. Tell the user to review and submit. Observe the resulting task through the host afterward. Dispatch never presses Submit or synthesizes keyboard input.

## Keep server-only projects separate

Only for an explicit request to ensure a server-only project:

```bash
python3 scripts/codex_handoff.py server-only "${common[@]}" --enable-experimental
```

This operation lists exact single-root matches, creates a missing server project with a deterministic project idempotency key, then lists again. It does not create a task or bootstrap a desktop project. The result carries `server_only: true` and `desktop_project_menu: "absent"`. Never pass its server ID to a desktop tool or deep link. No cleanup operation is provided.

## Observation input contract

Snapshots are inputs from the agent host, not an alternative project registry. Obtain them immediately before each phase, and always after bootstrap or a GUI root change. The helper cannot independently establish their freshness.

Desktop projects use the exposed shape:

```json
{"projects":[{"projectId":"desktop-id","projectKind":"local","path":"/absolute/repository"}]}
```

A complete multi-root provider may replace `path` with ordered `roots`, each an absolute path string or an object containing `path`. Preserve `projectKind: "local"` only for projects the host identifies as local. Remote or unknown project kinds are not eligible.

Thread observations use:

```json
{"threads":[{"threadId":"ready-thread-id","projectId":"desktop-id"}]}
```

Thread records may use `id` instead of `threadId`. A bare projects or threads array is also accepted. Unwrap tool transport envelopes at the host boundary without changing the records. Never construct a desktop record from app-server data.

Run `make self-check` for the offline two-repository smoke test. It creates a real managed workspace and exercises preparation and observation with fixture host responses. It does not launch a live Codex task. The multi-repository primary-root handoff passed a live Codex desktop smoke test on 2026-09-14: the task launched with one attached primary root and read both repositories plus the ledger. The host did not independently expose the task's desktop project association.
