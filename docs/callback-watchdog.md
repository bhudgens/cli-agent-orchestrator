# Callback watchdog MVP

Combined callback watchdog and restart recovery implementation.

The watchdog tracks whether a newly assigned local worker has sent a message
back to its caller. It stores the obligation in the CAO database and periodically
checks outstanding tasks. It does not determine whether the work succeeded or
whether the caller actually consumed the message.

## Behavior

- Local assignment creates a durable worker/caller record before provider initialization.
- An accepted worker-to-caller inbox message closes the matching obligation.
  Any message counts, including a progress update; there is no final-result protocol.
- Missing runtime evidence can mark a task not deliverable; provider error marks it failed.
  An expired deadline otherwise marks it overdue. Output timestamps are recorded as
  evidence, not interpreted as semantic progress or completion.
- An overdue worker may receive a reminder through the existing terminal input path
  when its session/window are present and its status is idle or completed.
  Explicit human/manual/persistent/anchor metadata suppresses reminders.
- The default is one durable nudge claim per task. A claim survives restart even if
  sending fails, so a failed or interrupted send can consume the reminder allowance.
- `GET /callback-tasks` and `cao agent callback-tasks` list active obligations.
  Use `?active_only=false` or CLI `--all` to include closed/failed obligations;
  CLI `--json` provides structured output.

Configuration: `CALLBACK_WATCHDOG_INTERVAL_SECONDS` (30 seconds),
`CALLBACK_TASK_TIMEOUT_SECONDS` (300 seconds), and
`CALLBACK_WATCHDOG_MAX_NUDGES` (1; set to 0 to disable reminders).

## Session supervision and recovery

This change incorporates the recovery implementation merged in
[PR #6](https://github.com/bhudgens/cli-agent-orchestrator/pull/6), with exact-only
recovery, retained unmatched rows, observational session GETs, and global identity
collision checks. Startup restores in-process adapters for exact existing rows;
rowless windows require explicit operator recovery. Retention preserves rows and
logs for old workers whose tmux session still exists or cannot be checked reliably.

The periodic watchdog also scans all live CAO windows and all registry identities.
It reports missing/unmanaged windows and registration errors. For an exact tracked,
unprotected worker, it restores runtime registration and checks the current screen.
When idle with pending mail and no unsubmitted composer text, it retries ordinary
inbox delivery once per message. It never fabricates a mail-reading tool: the queued
message itself is the actionable nudge. Unknown status does not authorize input.

`GET /watchdog` exposes persisted observations; the dashboard displays failures.
Visible MCP startup/transport errors are reported, but absent errors do not prove
tool connectivity: MCP health remains unknown without protocol evidence.

Bulk recovery:

```bash
cao terminal recover --all --dry-run --format json
cao terminal recover --apply TOKEN --exact-only --hold-inbox
```

Admin APIs are `POST /terminals/recovery/plan` and
`POST /terminals/recovery/apply` with `{"token":"TOKEN"}`. Tokens expire after five
minutes and a server restart invalidates them. Evidence is rechecked before apply;
changed panes/policy or duplicate identity claims prevent unsafe adoption. Repeating
an applied token returns the cached result. Partial failures require a fresh plan.
No recovery path prunes, kills, renames, or relaunches live windows.

Recovered rows carry `metadata.cao_recovery_hold=true`. Inbox delivery and watchdog
nudges respect this durable hold and human/manual/persistent/anchor metadata. An
operator can release the recovery hold through the existing terminal metadata
update API/tool, preserving other metadata; protected anchors remain protected.

This watchdog does not repair inbox transport. In particular, the existing inbox
marks a message delivered before writing to tmux. A crash in between can strand
an unsent message outside the pending-message retry sweep. Moving the write after
the send instead creates a duplicate-delivery window. A separate delivery protocol
fix should define durable in-flight claims, restart recovery, and how ambiguous
delivery is reported or retried; exactly-once tmux delivery cannot be assumed.

## Validation limits

Tests cover real SQLite/manifest recovery, policy preservation, observational GETs,
duplicate ids, replaced panes, registration recovery, idle mail delivery, protected
inboxes, API/CLI wiring, callback assignment registration and database reopening.
External tmux/model clients remain test boundaries. A status check and tmux write
cannot be one atomic operation; a worker can change state between them. Callback
closure and reminder send are serialized within the server process. Durable nudge
claims prevent duplicates across restart but may consume an unsent reminder on crash.
Registration and callback recording remain best-effort on database failure.

Validation uses a disposable `CAO_HOME_DIR` and stubbed backend, including
any lifecycle tests; the earlier legacy lifecycle run lacked explicit home isolation.
Release validation separately checks packaged frontend assets and live API/runtime
state after a server-only restart; project sessions are preserved.
