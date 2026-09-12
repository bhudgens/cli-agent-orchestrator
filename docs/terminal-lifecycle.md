# Terminal Lifecycle

## Overview

Each terminal created by CAO (via `assign` or `handoff`) occupies a tmux window
and a database record. In long-running sessions, terminals accumulate and can
exhaust system resources. CAO provides automatic and manual cleanup paths.

## Deletion paths

| How deleted | Snapshot saved? |
|-------------|----------------|
| Handoff completes successfully (auto-delete) | Yes |
| `delete_terminal` MCP tool | Yes |
| `DELETE /terminals/{id}` API | Yes |
| `cao shutdown --session <name>` | Yes |
| `cao shutdown --all` | Yes |
| Process crash | No |

Individual deletion snapshots via `terminal_service.delete_terminal`.
Session-level shutdown (`delete_session`, which both `cao shutdown` modes reach
over `DELETE /sessions/{name}`) snapshots too: capturing each terminal's
scrollback is an explicit step of the teardown, and it deliberately runs
*before* the session kill, since scrollback only exists while the pane does. A
crash bypasses both paths, so nothing is captured.

Capture is best-effort everywhere, not just at session level: a snapshot whose
write fails is logged and teardown continues regardless of which path took it.
So "Yes" above means the path attempts a snapshot, not that one is guaranteed.

## Snapshot files

On deletion, two files are written to `~/.cao/logs/terminal/`:

- `<terminal_id>.scrollback` — plain-text capture of the full pane scrollback
- `<terminal_id>.snapshot.json` — metadata for restore

Snapshot JSON schema:

```json
{
  "terminal_id": "...",
  "session_name": "...",
  "window_name": "...",
  "agent_profile": "...",
  "provider": "...",
  "working_directory": "...",
  "allowed_tools": null
}
```

All three file types (`.log`, `.scrollback`, `.snapshot.json`) are purged after
`RETENTION_DAYS` (default: 7) by the cleanup service.

## Restore

```bash
cao terminal restore <terminal_id>
```

This creates a **plain shell window** in the original session at the original
working directory, replaying the saved scrollback via `cat ... ; exec $SHELL -l`.

Constraints:

- The original session must still exist. If the session was shut down, restore
  will fail. You can still read the scrollback directly:
  `cat ~/.cao/logs/terminal/<terminal_id>.scrollback`
- Restore creates a shell window, not a re-launched agent. The window shows
  the old output but is not connected to any provider.

## Restart recovery and adoption

The server keeps a versioned recovery manifest for each newly created terminal
under `~/.aws/cli-agent-orchestrator/terminal-recovery/<terminal_id>.recovery.json`.
For tmux, the identity fields are also mirrored into per-window
`@cao-recovery-*` user options. These stores carry the exact terminal id,
session/window, provider, profile, and working directory needed to reconnect a
live worker after a `cao-server` restart; they are separate from snapshots,
which describe terminals that are being deleted.

Restart resync first reconciles rows that still point at live windows. A
row-less window is automatically adopted only when exactly one complete
manifest or tmux metadata record names that exact session/window. A window
with no evidence, duplicate records, or disagreeing manifest/tmux values is
left alone and reported as unreconciled; CAO never guesses from names such as
`anchor-dev-new`, and resync never kills, renames, or restarts a tmux resource
or sends new recovery/probe input to it. After successful recovery, normal
pending-inbox delivery may resume messages that were already queued for the
terminal. Supported terminal deletion removes its recovery manifest.

For a legacy persistent worker that predates recovery metadata, an operator can
re-adopt the existing live window with explicit identity:

```bash
cao terminal adopt \
  --session-name cao-iac-sailpoint-supervisor \
  --window-name anchor-dev-new \
  --terminal-id c8397e50 \
  --provider codex \
  --agent-profile developer \
  --working-directory /Users/benjamin.hudgens/reverts/project-iac-sailpoint \
  --caller-id 5a88b9bb \
  --metadata-json '{"role":"anchor-dev","persistence":"human-driven"}'
```

The command calls the admin-only `POST /terminals/adopt` API. The service
checks that the named session and window already exist, refuses terminal-id or
window collisions, writes the normal terminal row, and restores provider
lookup, output/FIFO and status monitoring, working-directory lookup, and input
delivery. It does not create a tmux resource or initialize a new provider
process, so any provider-specific state that was lost independently of the
pane remains an operator-visible limitation.

## Assign vs handoff cleanup

- **Handoff** terminals are deleted automatically on success. No action needed.
- **Assign** terminals are not auto-deleted. Call `delete_terminal(terminal_id)`
  when you no longer need the terminal, or wait for the 10-terminal nudge.

## Terminal count nudge

When a session reaches 10 terminals, `assign` and `handoff` responses include:

> NOTE: This session has N terminals. Consider calling delete_terminal on
> terminals you no longer need.
