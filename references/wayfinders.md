# Wayfinders and prototype workspaces

Use these conventions for private decision maps and linked prototype workspaces.

## Private artifacts

```text
<ledger-root>/wayfinders/<map-id>/
  map.md      # mutable decision map
  tickets/    # decision tickets, not implementation tickets
  assets/     # durable prototype or research artifacts
  notes.md
```

Wayfinder maps may outlive and feed multiple implementation workspaces. Edit these files directly; do not invent helpers before repeated use establishes an automation need. Inspect configured source repositories without modifying their worktrees by default. Never publish maps or decision tickets externally without explicit approval; feed resolved decisions into workspace `spec.md` rather than mixing decision and implementation tickets.

## Prototype workspaces

A wayfinder prototype ticket may create a linked workspace with disposable code worktrees:

- Use workspace ID `prototype-<map-id>-<ticket-id>`.
- Record map and ticket links in `workspace.yaml` or `notes.md`.
- Do not push or merge the prototype by default.
- Copy or link durable results back to the map before cleanup.
- Treat promotion of prototype code into production work as a separate explicit decision.
